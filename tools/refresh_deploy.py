"""部署刷新：把「最新版代码」刷进本机部署目录，失败自动回滚。

定位
----
本目录是**运行目录**，不是仓库：`.env`、`data/`、`data_archive/`、`logs/`、`service/`
都是本机资产，绝不能跟着代码一起换。所以刷新只替换代码文件，其余原地保留。

典型用法
--------
  # 从 git 源目录刷新（推荐，配合 tools/pull_and_refresh.cmd）
  python tools/refresh_deploy.py --source C:\\legal\\legal-src --restart

  # 从压缩包刷新（源机丢一个 zip 过来时）
  python tools/refresh_deploy.py --source C:\\legal\\最新代码.zip --restart

  # 先看会动哪些文件
  python tools/refresh_deploy.py --source C:\\legal\\legal-src --dry-run

保护范围
--------
不动的：.env*（真实配置）、data/、data_archive/、logs/、reports/、runtime/、
backup/、backups/、service/、tools/、tmp/、.workbuddy/、根目录的 *.md 笔记
替换的：app/、requirements.txt、Dockerfile、docker-compose.yml、README.md、
        .dockerignore、.env.example、.env.clean.example、.env.sqlite.example

流程
----
备份 → 替换 → 语法自检 → 自愈本地运维件（.env 数据源段 / WinSW 启动参数）
→ 重启服务 → /health 轮询 → 通过则保留备份，失败则回滚并重启
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent
BACKUP_ROOT = ROOT / "backups"
RUNTIME_DIR = ROOT / "runtime"
SERVICE_NAME = "LegalDemoAPI"
SERVICE_EXE = ROOT / "service" / "LegalDemoService.exe"
SERVICE_XML = ROOT / "service" / "LegalDemoService.xml"

CODE_DIRS = ["app"]
CODE_FILES = [
    "requirements.txt",
    "Dockerfile",
    "docker-compose.yml",
    "README.md",
    ".dockerignore",
    ".env.example",
    ".env.clean.example",
    ".env.sqlite.example",
]


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


# --------------------------------------------------------------------------- #
# 源准备
# --------------------------------------------------------------------------- #
def _looks_like_source(path: Path) -> bool:
    return (path / "app" / "main.py").exists()


def prepare_source(source: Path, workdir: Path) -> Tuple[Path, str]:
    """返回 (代码根目录, 来源描述)。支持目录 / zip / tar.gz。"""
    if source.is_dir():
        if not _looks_like_source(source):
            # 允许传仓库上层目录
            for child in source.iterdir():
                if child.is_dir() and _looks_like_source(child):
                    return child, f"目录 {child}"
            raise SystemExit(f"源目录不含 app/main.py：{source}")
        return source, f"目录 {source}"

    if not source.exists():
        raise SystemExit(f"源不存在：{source}")

    name = source.name.lower()
    extract_dir = workdir / "src"
    extract_dir.mkdir(parents=True, exist_ok=True)
    log(f"解压 {source.name} ...")
    if name.endswith(".zip"):
        with zipfile.ZipFile(source) as zf:
            zf.extractall(extract_dir)
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        with tarfile.open(source) as tf:
            tf.extractall(extract_dir)
    else:
        raise SystemExit(f"不支持的包格式：{source.name}（只支持 .zip / .tar.gz / .tgz）")

    if _looks_like_source(extract_dir):
        return extract_dir, f"包 {source.name}"
    for child in extract_dir.iterdir():
        if child.is_dir() and _looks_like_source(child):
            return child, f"包 {source.name} (子目录 {child.name})"
    raise SystemExit("压缩包内未找到 app/main.py")


def source_version(src: Path) -> Dict[str, Any]:
    info: Dict[str, Any] = {"path": str(src)}
    for git_dir in (src, src.parent, src.parent.parent):
        if (git_dir / ".git").exists():
            try:
                info["git_commit"] = subprocess.run(
                    ["git", "-C", str(git_dir), "rev-parse", "--short", "HEAD"],
                    capture_output=True, text=True, errors="replace", timeout=15,
                ).stdout.strip()
                info["git_subject"] = subprocess.run(
                    ["git", "-C", str(git_dir), "log", "-1", "--format=%s"],
                    capture_output=True, text=True, errors="replace", timeout=15,
                ).stdout.strip()
                info["git_branch"] = subprocess.run(
                    ["git", "-C", str(git_dir), "rev-parse", "--abbrev-ref", "HEAD"],
                    capture_output=True, text=True, errors="replace", timeout=15,
                ).stdout.strip()
                info["git_dirty"] = bool(subprocess.run(
                    ["git", "-C", str(git_dir), "status", "--porcelain"],
                    capture_output=True, text=True, errors="replace", timeout=15,
                ).stdout.strip())
            except Exception as exc:  # noqa: BLE001
                info["git_error"] = str(exc)
            break
    return info


# --------------------------------------------------------------------------- #
# 备份 / 还原
# --------------------------------------------------------------------------- #
def backup_current(tag: str) -> Path:
    dest = BACKUP_ROOT / f"deploy_{tag}"
    dest.mkdir(parents=True, exist_ok=True)
    items: List[str] = []
    for name in CODE_DIRS:
        src = ROOT / name
        if src.exists():
            shutil.copytree(src, dest / name, ignore=shutil.ignore_patterns("__pycache__"))
            items.append(name + "/")
    for name in CODE_FILES:
        src = ROOT / name
        if src.exists():
            shutil.copy2(src, dest / name)
            items.append(name)
    (dest / "_manifest.json").write_text(
        json.dumps({"created_at": datetime.now().isoformat(timespec="seconds"), "items": items},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return dest


def restore_from(backup: Path) -> None:
    for name in CODE_DIRS:
        src = backup / name
        dst = ROOT / name
        if src.exists():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    for name in CODE_FILES:
        src = backup / name
        if src.exists():
            shutil.copy2(src, ROOT / name)


# --------------------------------------------------------------------------- #
# 替换
# --------------------------------------------------------------------------- #
def apply_code(src: Path, dry_run: bool) -> Dict[str, List[str]]:
    report: Dict[str, List[str]] = {"dirs": [], "files": [], "skipped": []}
    for name in CODE_DIRS:
        src_dir = src / name
        if not src_dir.exists():
            report["skipped"].append(name + "/")
            continue
        report["dirs"].append(name + "/")
        if dry_run:
            continue
        dst = ROOT / name
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src_dir, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for name in CODE_FILES:
        src_file = src / name
        if not src_file.exists():
            report["skipped"].append(name)
            continue
        report["files"].append(name)
        if not dry_run:
            shutil.copy2(src_file, ROOT / name)
    return report


def self_heal() -> None:
    """把本机运维件修回可切换状态（仓库里的版本可能覆盖掉了）。"""
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))
    try:
        import db_source

        action = db_source.ensure_env_block()
        log(f".env 数据源配置段: {action}")
    except Exception as exc:  # noqa: BLE001
        log(f"警告：.env 数据源段自愈失败: {exc}")

    # 重放本地兼容补丁：上游只认原生 HNSW，本机库是 bit 汉明量化索引
    patch_script = TOOLS_DIR / "patches" / "apply_local_patches.py"
    compat_on = any(
        line.strip().upper().startswith("RAG_ANN_COMPAT=") and "ivfflat" in line.lower()
        for line in (ROOT / ".env").read_text(encoding="utf-8", errors="replace").splitlines()
    )
    if patch_script.exists() and compat_on:
        proc = subprocess.run(
            [sys.executable, str(patch_script)],
            capture_output=True, text=True, errors="replace", timeout=120,
        )
        for line in (proc.stdout or "").strip().splitlines():
            log(line)
        if proc.returncode != 0:
            raise RuntimeError(f"本地兼容补丁重放失败: {(proc.stderr or '')[-500:]}")
    elif compat_on:
        log("警告：RAG_ANN_COMPAT 已开启但补丁脚本缺失")

    # WinSW 必须走启动器，否则数据源切换在服务重启后失效
    if SERVICE_XML.exists():
        text = SERVICE_XML.read_text(encoding="utf-8", errors="replace")
        if "launch_api.py" not in text:
            if "-m uvicorn app.main:app" in text:
                text = text.replace(
                    "-m uvicorn app.main:app --host 0.0.0.0 --port 7000 --workers 1 --proxy-headers",
                    '"C:\\legal\\legal-demo-deploy\\tools\\launch_api.py" --host 0.0.0.0 --port 7000',
                )
            text = text.replace(
                "<arguments>",
                "<arguments>",
            )
            SERVICE_XML.write_text(text, encoding="utf-8")
            log("已把 WinSW 启动参数修回 tools/launch_api.py")
        else:
            log("WinSW 启动参数已指向启动器，无需修改")


def compile_check() -> Tuple[bool, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "compileall", "-q", "app"],
        cwd=str(ROOT), capture_output=True, text=True, errors="replace", timeout=600,
    )
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "").strip()[-2000:]
    return True, ""


# --------------------------------------------------------------------------- #
# 服务重启 / 健康检查
# --------------------------------------------------------------------------- #
def _port_owner(port: int) -> Optional[int]:
    try:
        proc = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, errors="replace", timeout=30)
    except Exception:  # noqa: BLE001
        return None
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local, state, pid = parts[1], parts[3], parts[4]
        if state.upper() == "LISTENING" and local.endswith(f":{port}"):
            try:
                return int(pid)
            except ValueError:
                return None
    return None


def _service_is_running() -> bool:
    """用 WinSW 自带的 status 判断，避免依赖 sc.exe（部分环境被安全策略禁用）。"""
    if not SERVICE_EXE.exists():
        return False
    try:
        proc = subprocess.run(
            [str(SERVICE_EXE), "status"], capture_output=True, text=True,
            errors="replace", timeout=60,
        )
    except Exception:  # noqa: BLE001
        return False
    return "Started" in (proc.stdout or "")


def _kill_stray(port: int) -> None:
    """服务外的手工进程会占端口，导致服务起不来。只杀 python 进程。"""
    pid = _port_owner(port)
    if not pid or pid == 0:
        return
    if _service_is_running():
        log(f"服务在运行、端口 {port} 由其子进程 PID {pid} 持有，交由 restart 处理")
        return
    proc = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True, errors="replace", timeout=30)
    if "python" not in (proc.stdout or "").lower():
        log(f"端口 {port} 被非 python 进程占用 (PID {pid})，不处理")
        return
    log(f"端口 {port} 被手工 python 进程 PID {pid} 占用，先停止")
    subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, text=True, errors="replace", timeout=30)


def restart_service(port: int) -> bool:
    try:
        _kill_stray(port)
    except Exception as exc:  # noqa: BLE001
        log(f"清理占端口进程时出错（忽略）: {exc}")
    try:
        if SERVICE_EXE.exists():
            subprocess.run([str(SERVICE_EXE), "restart"], capture_output=True, text=True, errors="replace", timeout=180)
        else:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"Restart-Service {SERVICE_NAME} -Force"],
                capture_output=True, text=True, errors="replace", timeout=180,
            )
    except Exception as exc:  # noqa: BLE001
        log(f"重启服务异常: {exc}")
        return False
    if _port_owner(port) is None:
        # 服务可能是 Stopped，尝试启动
        try:
            subprocess.run([str(SERVICE_EXE), "start"], capture_output=True, text=True, errors="replace", timeout=120)
        except Exception:  # noqa: BLE001
            pass
    return True


def wait_health(port: int, timeout: int = 120) -> Tuple[bool, str]:
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                body = resp.read().decode("utf-8", "replace")
                if resp.status == 200:
                    return True, body
                last = f"HTTP {resp.status}: {body[:200]}"
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(2)
    return False, last


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="用最新版代码刷新本机部署")
    parser.add_argument("--source", required=True, help="git 源目录，或 .zip/.tar.gz 代码包")
    parser.add_argument("--restart", action="store_true", help="刷新后重启服务并校验 /health")
    parser.add_argument("--port", type=int, default=7000)
    parser.add_argument("--dry-run", action="store_true", help="只显示将要变更的文件")
    parser.add_argument("--install-deps", action="store_true", help="刷新后 pip install -r requirements.txt")
    parser.add_argument("--keep-backups", type=int, default=10, help="保留最近 N 份部署备份")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    os.environ.setdefault("PYTHONUTF8", "1")
    source = Path(args.source).expanduser()

    with tempfile.TemporaryDirectory(prefix="refresh_") as tmp:
        src, desc = prepare_source(source, Path(tmp))
        if src.resolve() == ROOT.resolve() or ROOT.resolve() in src.resolve().parents:
            raise SystemExit("源目录就是部署目录本身，拒绝执行（会把 app/ 删掉再复制自己）")
        version = source_version(src)
        log(f"代码来源: {desc}")
        if version.get("git_commit"):
            log(
                f"版本: {version.get('git_branch')}@{version['git_commit']}"
                f"{' (有未提交改动)' if version.get('git_dirty') else ''}  {version.get('git_subject','')}"
            )
        else:
            log("版本: 非 git 来源（无 commit 信息）")

        if args.dry_run:
            report = apply_code(src, dry_run=True)
            log(f"将替换目录: {report['dirs']}")
            log(f"将替换文件: {report['files']}")
            if report["skipped"]:
                log(f"源中缺失、保持原样: {report['skipped']}")
            log("dry-run 结束，未做任何修改")
            return 0

        # 先备份再覆盖，否则回滚只能回到"新代码"
        tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup = backup_current(tag)
        log(f"已备份当前代码到 {backup}")

        report = apply_code(src, dry_run=False)
        log(f"已替换目录: {report['dirs']}")
        log(f"已替换文件: {report['files']}")
        if report["skipped"]:
            log(f"源中缺失、保持原样: {report['skipped']}")

        ok, err = compile_check()
        if not ok:
            log(f"语法自检失败，回滚。错误：{err}")
            restore_from(backup)
            return 1
        log("语法自检通过 (compileall app)")

        try:
            self_heal()
        except Exception as exc:  # noqa: BLE001
            log(f"自愈失败（{exc}），回滚")
            restore_from(backup)
            return 1

        if args.install_deps:
            log("安装依赖 requirements.txt ...")
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
                cwd=str(ROOT), capture_output=True, text=True, errors="replace",
            )
            if proc.returncode != 0:
                log(f"依赖安装失败，回滚。输出：{(proc.stderr or '')[-1500:]}")
                restore_from(backup)
                return 1
            log("依赖安装完成")

        health = {"ok": None, "body": ""}
        if args.restart:
            log("重启服务 ...")
            try:
                restart_service(args.port)
                ok, body = wait_health(args.port)
            except Exception as exc:  # noqa: BLE001
                ok, body = False, f"{type(exc).__name__}: {exc}"
            health = {"ok": ok, "body": str(body)[:300]}
            if not ok:
                log(f"健康检查失败（{body}），开始回滚 ...")
                restore_from(backup)
                restart_service(args.port)
                ok2, body2 = wait_health(args.port, timeout=120)
                log(f"回滚后健康检查: {'通过' if ok2 else '仍失败'}  {body2[:200]}")
                _write_record(tag, version, report, backup, health, rolled_back=True)
                return 1
            log(f"健康检查通过: {body[:120]}")

    _write_record(tag, version, report, backup, health, rolled_back=False)
    _prune_backups(args.keep_backups)
    log("刷新完成")
    return 0


def _write_record(tag: str, version: Dict[str, Any], report: Dict[str, List[str]],
                  backup: Path, health: Dict[str, Any], *, rolled_back: bool) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "at": datetime.now().isoformat(timespec="seconds"),
        "tag": tag,
        "source": version,
        "replaced": report,
        "backup": str(backup),
        "health": health,
        "rolled_back": rolled_back,
    }
    (RUNTIME_DIR / "last_refresh.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _prune_backups(keep: int) -> None:
    if keep <= 0 or not BACKUP_ROOT.exists():
        return
    dirs = sorted([d for d in BACKUP_ROOT.iterdir() if d.is_dir() and d.name.startswith("deploy_")])
    for old in dirs[:-keep]:
        shutil.rmtree(old, ignore_errors=True)
        log(f"清理旧备份 {old.name}")


if __name__ == "__main__":
    sys.exit(main())
