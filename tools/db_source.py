"""数据源可替换层：本地 PostgreSQL（兜底） / 远端 PostgreSQL（主数据源）。

设计约束
--------
1. 本文件不 import 任何 `app.*` 模块，也不修改应用代码。
   应用通过 `DATABASE_URL` 环境变量拿到最终地址（`app/core/config.py` 里
   `load_dotenv()` 默认 override=False，所以启动器注入的环境变量优先级高于 .env）。
   这样后续从仓库拉取新版本代码并覆盖 `app/` 时，数据源切换逻辑不会被冲掉。

2. 只有两个"插槽"：
     - local ：本机 PG，当前是 legal_demo_migrated（63GB），作为兜底
     - remote：另一台机器上的全量数据源，准备好后填 URL 即可接管

3. 远端未配置 / 探测失败时，只要 `DB_SOURCE_FALLBACK_ENABLED=true` 就回落本地，
   永不因为数据源不可用而让服务起不来。

CLI
---
  python tools/db_source.py status                  # 人看的全景状态
  python tools/db_source.py resolve --json          # 机器读的解析结果
  python tools/db_source.py probe local|remote      # 单独探测
  python tools/db_source.py switch local|remote     # 改 .env 里的 DB_SOURCE_MODE
  python tools/db_source.py set-remote <url>        # 写入远端连接串并探测
  python tools/db_source.py verify                  # 校验远端是否真的可用（含维度/模型一致性）
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env"
RUNTIME_DIR = ROOT / "runtime"
STATE_FILE = RUNTIME_DIR / "db_source.json"
LOG_FILE = ROOT / "logs" / "db_source.log"

MARK_BEGIN = "# ===== 数据源切换 BEGIN（由 tools/db_source.py 管理，请勿改动段落标记）====="
MARK_END = "# ===== 数据源切换 END ====="

DEFAULT_REQUIRED_TABLES = [
    "users",
    "legal_cases",
    "rag_chunks",
    "rag_chunk_embeddings",
    "legal_rules",
    "item_keywords",
]

# 远端接管时必须强制打开的开关：
# 远端数据源里向量索引已经建好，绝不能让应用启动时再建/再灌。
REMOTE_MODE_OVERRIDES = {
    "RAG_VECTOR_DEFER_INDEXES": "true",
    "STARTUP_CANADA_SYNC_MODE": "disabled",
    "ARCHIVE_BOOTSTRAP_ENABLED": "false",
    "DEMO_HISTORY_SEED_ENABLED": "false",
    # [local-patch:corpus-remote] 远端 legal_corpus 直查模式：
    # public.rag_chunks / rag_chunk_embeddings 是兼容视图，检索层跳过本地
    # ensure/DDL/HNSW 状态检查，向量通道走远端原生 HNSW（见 vector_store_service）。
    "RAG_CORPUS_REMOTE": "true",
    # 远端无本地 IVFFlat+汉明索引，关闭两阶段汉明补丁，改用原生 HNSW。
    "RAG_ANN_COMPAT": "",
}


# --------------------------------------------------------------------------- #
# .env 读写
# --------------------------------------------------------------------------- #
def read_env(path: Path = ENV_FILE) -> Dict[str, str]:
    """极简 .env 解析（只取 KEY=VALUE，不处理插值），不 import dotenv。"""
    data: Dict[str, str] = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key:
            data[key] = value
    return data


def _cfg(env: Dict[str, str], key: str, default: str = "") -> str:
    """环境变量优先于 .env（与 dotenv override=False 保持一致的语义）。"""
    return (os.environ.get(key) or env.get(key) or default).strip()


def _cfg_bool(env: Dict[str, str], key: str, default: bool) -> bool:
    raw = _cfg(env, key, "true" if default else "false").lower()
    return raw in {"1", "true", "yes", "on"}


def _cfg_int(env: Dict[str, str], key: str, default: int) -> int:
    try:
        return int(_cfg(env, key, str(default)))
    except ValueError:
        return default


def build_env_block(env: Dict[str, str]) -> str:
    """生成 .env 中的数据源段落（值取自当前 .env，缺失给默认值）。"""
    mode = _cfg(env, "DB_SOURCE_MODE", "local") or "local"
    local_url = _cfg(env, "DB_SOURCE_LOCAL_URL") or _cfg(env, "DATABASE_URL")
    remote_url = _cfg(env, "DB_SOURCE_REMOTE_URL")
    timeout = _cfg(env, "DB_SOURCE_PROBE_TIMEOUT", "5")
    fallback = _cfg(env, "DB_SOURCE_FALLBACK_ENABLED", "true")
    strict = _cfg(env, "DB_SOURCE_STRICT_EMBEDDING_MATCH", "true")
    return "\n".join(
        [
            MARK_BEGIN,
            "# 数据源只有一个插槽：local=本机 PG（兜底），remote=另一台机器的全量数据源。",
            "# MODE=remote 且远端探测不通过时，若 FALLBACK=true 会自动回落本地，服务照常启动。",
            f"DB_SOURCE_MODE={mode}",
            f"DB_SOURCE_LOCAL_URL={local_url}",
            f"DB_SOURCE_REMOTE_URL={remote_url}",
            f"DB_SOURCE_PROBE_TIMEOUT={timeout}",
            f"DB_SOURCE_FALLBACK_ENABLED={fallback}",
            f"DB_SOURCE_STRICT_EMBEDDING_MATCH={strict}",
            MARK_END,
        ]
    )


def ensure_env_block(path: Path = ENV_FILE) -> str:
    """幂等地写入/刷新 .env 数据源段落。返回值：created|replaced|unchanged。"""
    text = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    env = read_env(path)
    block = build_env_block(env)
    if MARK_BEGIN in text and MARK_END in text:
        pattern = re.compile(
            re.escape(MARK_BEGIN) + r".*?" + re.escape(MARK_END), re.S
        )
        new_text, n = pattern.subn(block, text)
        if n == 0:
            new_text = text.rstrip() + "\n\n" + block + "\n"
            path.write_text(new_text, encoding="utf-8")
            return "created"
        if new_text == text:
            return "unchanged"
        path.write_text(new_text, encoding="utf-8")
        return "replaced"
    new_text = text.rstrip() + ("\n\n" if text.strip() else "") + block + "\n"
    path.write_text(new_text, encoding="utf-8")
    return "created"


def set_env_value(key: str, value: str, path: Path = ENV_FILE) -> None:
    """改写 .env 里单个 KEY（存在则替换，不存在则追加到数据源段之前）。"""
    text = path.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.M)
    if pattern.search(text):
        text = pattern.sub(f"{key}={value}", text, count=1)
    else:
        text = text.rstrip() + f"\n{key}={value}\n"
    path.write_text(text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# 连接串工具
# --------------------------------------------------------------------------- #
def to_libpq(dsn: str) -> str:
    """SQLAlchemy 连接串 → psycopg2 可直接吃的 libpq DSN。"""
    dsn = (dsn or "").strip()
    for prefix in ("postgresql+psycopg2://", "postgresql+psycopg://", "postgres+psycopg2://"):
        if dsn.startswith(prefix):
            return "postgresql://" + dsn[len(prefix) :]
    if dsn.startswith("postgres://"):
        return "postgresql://" + dsn[len("postgres://") :]
    return dsn


def mask_url(dsn: str) -> str:
    """脱敏：密码替换为 ***，用于日志与状态文件。"""
    dsn = (dsn or "").strip()
    if not dsn:
        return ""
    try:
        parts = urlsplit(dsn)
        if parts.password:
            netloc = f"{parts.username}:***@{parts.hostname}"
            if parts.port:
                netloc += f":{parts.port}"
            return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        pass
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:***@", dsn)


def describe_url(dsn: str) -> str:
    """host:port/db 摘要。"""
    try:
        parts = urlsplit(dsn)
        host = parts.hostname or "?"
        port = parts.port or 5432
        db = (parts.path or "/").lstrip("/") or "?"
        return f"{host}:{port}/{db}"
    except ValueError:
        return "?"


# --------------------------------------------------------------------------- #
# 探测
# --------------------------------------------------------------------------- #
def probe(
    dsn: str,
    timeout: int = 5,
    *,
    deep: bool = True,
    expected_dim: Optional[int] = None,
    expected_provider: str = "",
    expected_model: str = "",
    required_tables: Optional[List[str]] = None,
    strict_embedding: bool = True,
) -> Dict[str, Any]:
    """探测一个数据源是否可用。

    返回：{ok, latency_ms, error, fatal[], warnings[], details{}}
    fatal 非空 => 不可用。
    """
    result: Dict[str, Any] = {
        "target": describe_url(dsn),
        "ok": False,
        "latency_ms": None,
        "error": "",
        "fatal": [],
        "warnings": [],
        "details": {},
    }
    if not dsn:
        result["error"] = "连接串为空"
        result["fatal"].append("empty_dsn")
        return result
    if dsn.startswith("sqlite"):
        result["error"] = "当前数据源切换层只支持 PostgreSQL"
        result["fatal"].append("unsupported_scheme")
        return result

    try:
        import psycopg2  # 延迟导入：本文件在无 psycopg2 环境也能做纯解析
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"psycopg2 不可用，无法深度探测: {exc}"
        result["fatal"].append("psycopg2_missing")
        return result

    started = time.time()
    conn = None
    try:
        conn = psycopg2.connect(to_libpq(dsn), connect_timeout=max(1, int(timeout)))
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {max(1, int(timeout)) * 1000}")
        cur.execute("SELECT 1")
        cur.fetchone()
        result["latency_ms"] = int((time.time() - started) * 1000)
        if not deep:
            result["ok"] = True
            return result

        # 扩展
        cur.execute("SELECT extversion FROM pg_extension WHERE extname = 'vector'")
        row = cur.fetchone()
        result["details"]["pgvector"] = row[0] if row else None
        if not row:
            result["fatal"].append("pgvector 扩展缺失")

        # 表清单（含视图：远端接管模式下 rag_chunks / rag_chunk_embeddings /
        # source_items 是 legal_corpus.* 的兼容视图，不在 pg_tables 中）
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' "
            "  AND table_type IN ('BASE TABLE', 'VIEW')"
        )
        tables = {r[0] for r in cur.fetchall()}
        want = required_tables or DEFAULT_REQUIRED_TABLES
        missing = [t for t in want if t not in tables]
        result["details"]["table_count"] = len(tables)
        result["details"]["missing_tables"] = missing
        if missing:
            result["fatal"].append(f"缺少核心表: {', '.join(missing)}")

        # 向量列维度
        if "rag_chunk_embeddings" in tables:
            cur.execute(
                "SELECT atttypmod FROM pg_attribute "
                "WHERE attrelid = 'rag_chunk_embeddings'::regclass "
                "  AND attname = 'embedding_vector' AND NOT attisdropped"
            )
            row = cur.fetchone()
            dim = row[0] if row and row[0] and row[0] > 0 else None
            result["details"]["vector_dim"] = dim
            if expected_dim and dim and int(dim) != int(expected_dim):
                result["fatal"].append(
                    f"向量维度不一致：数据源 {dim}，本地配置 EMBEDDING_DIMENSION={expected_dim}"
                )

            # 向量归属的 provider/model（本地 658 万行分组约 1s，超时降级为跳过）
            try:
                cur.execute(
                    "SELECT embedding_provider, embedding_model, count(*) "
                    "FROM rag_chunk_embeddings "
                    "GROUP BY 1, 2 ORDER BY 3 DESC LIMIT 5"
                )
                rows = cur.fetchall()
                result["details"]["embedding_sets"] = [
                    {"provider": r[0], "model": r[1], "rows": r[2]} for r in rows
                ]
                if rows and (expected_provider or expected_model):
                    top = rows[0]
                    bad = []
                    if expected_provider and (top[0] or "") != expected_provider:
                        bad.append(f"provider {top[0]}≠{expected_provider}")
                    if expected_model and (top[1] or "") != expected_model:
                        bad.append(f"model {top[1]}≠{expected_model}")
                    if bad:
                        msg = "向量归属与本地配置不一致: " + "; ".join(bad)
                        (result["fatal"] if strict_embedding else result["warnings"]).append(msg)
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                result["warnings"].append(f"向量归属检查跳过: {type(exc).__name__}")

            # ANN 索引（只告警：远端可能用了别的索引方案）
            try:
                cur.execute(
                    "SELECT indexname FROM pg_indexes WHERE tablename = 'rag_chunk_embeddings'"
                )
                idx = [r[0] for r in cur.fetchall()]
                ann = [i for i in idx if any(k in i.lower() for k in ("ivfflat", "hnsw"))]
                result["details"]["ann_indexes"] = ann
                if not ann:
                    result["warnings"].append(
                        "rag_chunk_embeddings 上没有 ivfflat/hnsw 索引，检索可能退化为全表扫描"
                    )
            except Exception as exc:  # noqa: BLE001
                conn.rollback()
                result["warnings"].append(f"索引检查跳过: {type(exc).__name__}")

        result["ok"] = not result["fatal"]
        return result
    except Exception as exc:  # noqa: BLE001
        result["latency_ms"] = int((time.time() - started) * 1000)
        result["error"] = f"{type(exc).__name__}: {str(exc).strip()[:300]}"
        result["fatal"].append("connect_or_query_failed")
        return result
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# 解析（核心决策）
# --------------------------------------------------------------------------- #
def resolve(
    env: Optional[Dict[str, str]] = None,
    *,
    deep: bool = True,
    write_state: bool = True,
) -> Dict[str, Any]:
    """决定本次该连哪个数据源，并给出需要注入的环境变量覆盖项。"""
    env = env if env is not None else read_env()

    mode = (_cfg(env, "DB_SOURCE_MODE", "local") or "local").lower()
    local_url = _cfg(env, "DB_SOURCE_LOCAL_URL") or _cfg(env, "DATABASE_URL")
    remote_url = _cfg(env, "DB_SOURCE_REMOTE_URL")
    fallback = _cfg_bool(env, "DB_SOURCE_FALLBACK_ENABLED", True)
    timeout = _cfg_int(env, "DB_SOURCE_PROBE_TIMEOUT", 5)
    strict = _cfg_bool(env, "DB_SOURCE_STRICT_EMBEDDING_MATCH", True)
    expected_dim = _cfg_int(env, "EMBEDDING_DIMENSION", 1024)
    expected_provider = _cfg(env, "EMBEDDING_PROVIDER")
    expected_model = _cfg(env, "EMBEDDING_MODEL")
    required = [t.strip() for t in _cfg(env, "DB_SOURCE_REQUIRED_TABLES").split(",") if t.strip()]

    out: Dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "active": "local",
        "url": local_url,
        "reason": "",
        "fallback_used": False,
        "probes": {},
        "overrides": {},
    }

    if mode not in {"local", "remote"}:
        out["mode"] = "local"
        out["reason"] = f"DB_SOURCE_MODE={mode} 非法，按 local 处理"
    elif mode == "local":
        out["reason"] = "本地模式（未启用远端数据源）"
    elif not remote_url:
        out["reason"] = "远端未配置（DB_SOURCE_REMOTE_URL 为空）"
        if not fallback:
            raise RuntimeError("DB_SOURCE_MODE=remote 但 DB_SOURCE_REMOTE_URL 为空")
    else:
        remote_probe = probe(
            remote_url,
            timeout=timeout,
            deep=deep,
            expected_dim=expected_dim,
            expected_provider=expected_provider,
            expected_model=expected_model,
            required_tables=required or None,
            strict_embedding=strict,
        )
        out["probes"]["remote"] = remote_probe
        if remote_probe["ok"]:
            out["active"] = "remote"
            out["url"] = remote_url
            out["reason"] = "远端数据源探测通过，接管"
            out["overrides"] = dict(REMOTE_MODE_OVERRIDES)
        else:
            problem = "; ".join(remote_probe["fatal"]) or remote_probe["error"]
            if not fallback:
                raise RuntimeError(f"远端数据源不可用且未开启兜底: {problem}")
            out["fallback_used"] = True
            out["reason"] = f"远端不可用已回落本地: {problem}"

    # 本地兜底也必须至少能连上；连不上只告警，交给应用自己报错（避免启动器吞掉真实原因）
    if out["active"] == "local":
        local_probe = probe(local_url, timeout=timeout, deep=False)
        out["probes"]["local"] = local_probe
        if not local_probe["ok"]:
            out["reason"] += f"；警告：本地数据源探测失败 {local_probe['error']}"

    if write_state:
        _write_state(out)
    return out


def _write_state(state: Dict[str, Any]) -> None:
    try:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.loads(json.dumps(state))
        payload["url"] = mask_url(payload.get("url", ""))
        for item in payload.get("probes", {}).values():
            if isinstance(item, dict) and "target" in item:
                pass  # target 已经是 host:port/db，无密码
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = (
            f"{state['generated_at']}\tmode={state['mode']}\tactive={state['active']}"
            f"\tfallback={state['fallback_used']}\ttarget={describe_url(state['url'])}\t{state['reason']}\n"
        )
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:  # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _print_probe(name: str, info: Dict[str, Any]) -> None:
    if not info:
        print(f"  {name:<7} -")
        return
    mark = "OK  " if info.get("ok") else "FAIL"
    print(f"  {name:<7} {mark} {info.get('target','')}  {info.get('latency_ms','-')}ms")
    if info.get("error"):
        print(f"          error: {info['error']}")
    for item in info.get("fatal", []):
        print(f"          fatal: {item}")
    for item in info.get("warnings", []):
        print(f"          warn : {item}")
    details = info.get("details") or {}
    if details:
        brief = {k: details[k] for k in ("pgvector", "table_count", "vector_dim", "ann_indexes") if k in details}
        if brief:
            print(f"          detail: {brief}")


def cmd_status(_: argparse.Namespace) -> int:
    env = read_env()
    state = resolve(env, deep=True, write_state=True)
    print("==")
    print(f"模式       : {state['mode']}  (local=本机兜底 / remote=远端全量)")
    print(f"当前生效   : {state['active']}")
    print(f"连接目标   : {describe_url(state['url'])}")
    print(f"决策原因   : {state['reason']}")
    print(f"兜底触发   : {'是' if state['fallback_used'] else '否'}")
    print("本机数据源 :", describe_url(_cfg(env, "DB_SOURCE_LOCAL_URL") or _cfg(env, "DATABASE_URL")))
    remote = _cfg(env, "DB_SOURCE_REMOTE_URL")
    print("远端数据源 :", describe_url(remote) if remote else "(未配置)")
    print("探测结果:")
    _print_probe("remote", state["probes"].get("remote"))
    _print_probe("local", state["probes"].get("local"))
    if state["overrides"]:
        print("已注入覆盖:", state["overrides"])
    print(f"状态文件   : {STATE_FILE}")
    print(f"日志       : {LOG_FILE}")
    return 0


def cmd_resolve(args: argparse.Namespace) -> int:
    state = resolve(deep=not args.shallow, write_state=True)
    if args.json:
        print(json.dumps(state, ensure_ascii=False))
    else:
        print(state["url"])
    return 0


def cmd_probe(args: argparse.Namespace) -> int:
    env = read_env()
    if args.target == "local":
        dsn = _cfg(env, "DB_SOURCE_LOCAL_URL") or _cfg(env, "DATABASE_URL")
    elif args.target == "remote":
        dsn = _cfg(env, "DB_SOURCE_REMOTE_URL")
    else:
        dsn = args.target
    if not dsn:
        print(f"未配置 {args.target} 的连接串", file=sys.stderr)
        return 2
    info = probe(
        dsn,
        timeout=_cfg_int(env, "DB_SOURCE_PROBE_TIMEOUT", 5),
        expected_dim=_cfg_int(env, "EMBEDDING_DIMENSION", 1024),
        expected_provider=_cfg(env, "EMBEDDING_PROVIDER"),
        expected_model=_cfg(env, "EMBEDDING_MODEL"),
        strict_embedding=_cfg_bool(env, "DB_SOURCE_STRICT_EMBEDDING_MATCH", True),
    )
    _print_probe(args.target, info)
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info["ok"] else 1


def cmd_switch(args: argparse.Namespace) -> int:
    ensure_env_block()
    if args.mode == "remote":
        env = read_env()
        if not _cfg(env, "DB_SOURCE_REMOTE_URL"):
            print(
                "拒绝切换：DB_SOURCE_REMOTE_URL 为空。\n"
                "先执行： python tools/db_source.py set-remote 'postgresql+psycopg2://user:pass@host:5432/db'",
                file=sys.stderr,
            )
            return 2
    set_env_value("DB_SOURCE_MODE", args.mode)
    print(f"已切换 DB_SOURCE_MODE={args.mode}（重启服务后生效：service_ctl.bat restart）")
    return 0


def cmd_set_remote(args: argparse.Namespace) -> int:
    ensure_env_block()
    probe_info = probe(args.url, timeout=_cfg_int(read_env(), "DB_SOURCE_PROBE_TIMEOUT", 5))
    _print_probe("候选", probe_info)
    if not probe_info["ok"]:
        print("\n探测未通过，未写入 .env。修好远端或加 --force 强制写入。", file=sys.stderr)
        if not args.force:
            return 1
    set_env_value("DB_SOURCE_REMOTE_URL", args.url)
    print(f"已写入 DB_SOURCE_REMOTE_URL={mask_url(args.url)}")
    if args.activate:
        set_env_value("DB_SOURCE_MODE", "remote")
        print("已同时设置 DB_SOURCE_MODE=remote")
    print("重启服务生效：service/service_ctl.bat restart")
    return 0


def cmd_verify(_: argparse.Namespace) -> int:
    env = read_env()
    remote = _cfg(env, "DB_SOURCE_REMOTE_URL") or _cfg(env, "REMOTE_CORPUS_URL")
    if not remote:
        print("远端未配置，无可校验对象。", file=sys.stderr)
        return 2
    info = probe(
        remote,
        timeout=_cfg_int(env, "DB_SOURCE_PROBE_TIMEOUT", 5),
        expected_dim=_cfg_int(env, "EMBEDDING_DIMENSION", 1024),
        expected_provider=_cfg(env, "EMBEDDING_PROVIDER"),
        expected_model=_cfg(env, "EMBEDDING_MODEL"),
        strict_embedding=_cfg_bool(env, "DB_SOURCE_STRICT_EMBEDDING_MATCH", True),
    )
    _print_probe("remote", info)
    ok = info["ok"]
    print("\n结论:", "远端可作为主数据源接管" if ok else "远端不可接管（见上方 fatal）")
    if ok and info.get("warnings"):
        print("但存在告警，建议接管前确认。")
    return 0 if ok else 1


CORPUS_REQUIRED_TABLES = [
    "legal_document",
    "legal_document_version",
    "legal_chunk",
    "legal_chunk_embedding",
    "legal_provision",
]

# 远端语料库（schema legal_corpus）与当前应用期望（public.rag_chunks 等）不是同一套 schema。
APP_EXPECTED_TABLES = ["rag_chunks", "rag_chunk_embeddings", "users", "legal_cases"]


def probe_corpus(
    dsn: str,
    timeout: int = 8,
    *,
    schema: str = "legal_corpus",
    expected_dim: int = 1024,
) -> Dict[str, Any]:
    """探测另一台机器的语料库（legal_corpus schema）。

    它和当前应用使用的 schema 不同，所以不能直接用 `probe()`；
    这里只回答「语料是否齐、向量维度对不对、HNSW 有没有」。
    """
    result: Dict[str, Any] = {
        "target": describe_url(dsn),
        "schema": schema,
        "ok": False,
        "error": "",
        "fatal": [],
        "warnings": [],
        "details": {},
    }
    if not dsn:
        result["error"] = "连接串为空"
        result["fatal"].append("empty_dsn")
        return result
    try:
        import psycopg2
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"psycopg2 不可用: {exc}"
        result["fatal"].append("psycopg2_missing")
        return result

    started = time.time()
    conn = None
    try:
        conn = psycopg2.connect(to_libpq(dsn), connect_timeout=max(1, int(timeout)))
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute(f"SET statement_timeout = {max(1, int(timeout)) * 1000}")

        cur.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,)
        )
        tables = {r[0] for r in cur.fetchall()}
        result["details"]["table_count"] = len(tables)
        missing = [t for t in CORPUS_REQUIRED_TABLES if t not in tables]
        result["details"]["missing_tables"] = missing
        if missing:
            result["fatal"].append(f"schema {schema} 缺少: {', '.join(missing)}")

        # 应用当前期望的 public 表是否存在（用于判断能否直接切过去）
        cur.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
        public_tables = {r[0] for r in cur.fetchall()}
        app_ready = [t for t in APP_EXPECTED_TABLES if t in public_tables]
        result["details"]["public_tables_total"] = len(public_tables)
        result["details"]["app_compat_tables"] = app_ready
        if len(app_ready) < len(APP_EXPECTED_TABLES):
            result["warnings"].append(
                "该库没有应用期望的 public 表（"
                + ", ".join(t for t in APP_EXPECTED_TABLES if t not in public_tables)
                + "），不能直接把 DATABASE_URL 指过来，需要做 schema 适配"
            )

        # 规模（用统计信息，避免 count(*) 扫几百万行）
        for table in ("legal_chunk", "legal_chunk_embedding", "legal_document"):
            if table in tables:
                cur.execute(
                    "SELECT reltuples::bigint FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = %s AND c.relname = %s",
                    (schema, table),
                )
                row = cur.fetchone()
                result["details"][f"{table}_rows"] = int(row[0]) if row else None

        # 向量列维度 + HNSW 索引
        cur.execute(
            """
            SELECT a.attname, a.atttypmod, t.typname
            FROM pg_attribute a
            JOIN pg_class c ON c.oid = a.attrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_type t ON t.oid = a.atttypid
            WHERE n.nspname = %s AND c.relname = 'legal_chunk_embedding'
              AND a.attnum > 0 AND NOT a.attisdropped
              AND t.typname IN ('vector', 'halfvec')
            """,
            (schema,),
        )
        vcols = cur.fetchall()
        if not vcols:
            result["fatal"].append("legal_chunk_embedding 上找不到 vector/halfvec 列")
        else:
            name, typmod, typname = vcols[0]
            result["details"]["vector_column"] = name
            result["details"]["vector_type"] = typname
            result["details"]["vector_dim"] = typmod if typmod and typmod > 0 else None
            if typmod and expected_dim and int(typmod) != int(expected_dim):
                result["fatal"].append(f"向量维度 {typmod} 与本地配置 {expected_dim} 不一致")

        cur.execute(
            """
            SELECT c.relname, am.amname, pg_get_indexdef(c.oid)
            FROM pg_class c
            JOIN pg_index i ON i.indexrelid = c.oid
            JOIN pg_am am ON am.oid = c.relam
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = %s AND am.amname IN ('hnsw', 'ivfflat')
            """,
            (schema,),
        )
        ann = [{"name": r[0], "method": r[1]} for r in cur.fetchall()]
        result["details"]["ann_indexes"] = ann
        if not ann:
            result["warnings"].append("语料库没有 hnsw/ivfflat 索引，检索会退化")

        # 许可字段（交付文档要求应用层按 access_class 等过滤）
        cur.execute(
            """
            SELECT column_name FROM information_schema.columns
            WHERE table_schema = %s AND table_name = 'legal_document'
              AND column_name IN ('access_class', 'embedding_decision', 'training_decision')
            """,
            (schema,),
        )
        lic = [r[0] for r in cur.fetchall()]
        result["details"]["license_columns"] = lic
        if len(lic) < 3:
            result["warnings"].append("legal_document 缺少许可字段，展示过滤无法落实")

        result["ok"] = not result["fatal"]
        result["latency_ms"] = int((time.time() - started) * 1000)
        return result
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {str(exc).strip()[:300]}"
        result["fatal"].append("connect_or_query_failed")
        return result
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def _http_check(url: str, timeout: int = 6, payload: Any = None) -> Dict[str, Any]:
    """轻量 HTTP 探测；只判断「能不能用」，不解析业务返回。"""
    import urllib.error
    import urllib.request

    out: Dict[str, Any] = {"url": url, "ok": False, "status": None, "latency_ms": None, "detail": ""}
    if not url:
        out["detail"] = "未配置"
        return out
    started = time.time()
    try:
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(url, data=body, method="POST",
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(600).decode("utf-8", "replace")
            out["status"] = resp.status
            out["ok"] = 200 <= int(resp.status) < 300
            out["detail"] = raw[:300]
    except urllib.error.HTTPError as exc:
        out["status"] = exc.code
        out["detail"] = exc.read(300).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        out["detail"] = f"{type(exc).__name__}: {exc}"
    out["latency_ms"] = int((time.time() - started) * 1000)
    return out


def check_services(env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """探测另一台机器的 LLM / BGE-M3 / reranker（经 frp 暴露的三端口）。"""
    env = env if env is not None else read_env()
    timeout = _cfg_int(env, "REMOTE_SERVICE_TIMEOUT", 8)
    llm_base = _cfg(env, "REMOTE_LLM_BASE_URL").rstrip("/")
    embed_url = _cfg(env, "REMOTE_EMBED_URL")
    rerank_url = _cfg(env, "REMOTE_RERANK_URL")

    checks: Dict[str, Any] = {}
    if llm_base:
        checks["llm"] = _http_check(f"{llm_base}/models", timeout=timeout)
        checks["llm"]["endpoint"] = f"{llm_base}/chat/completions"
    if embed_url:
        base = embed_url.rsplit("/", 1)[0]
        checks["embed"] = _http_check(f"{base}/health", timeout=timeout)
        checks["embed"]["endpoint"] = embed_url
    if rerank_url:
        base = rerank_url.rsplit("/", 1)[0]
        checks["rerank"] = _http_check(f"{base}/health", timeout=timeout)
        checks["rerank"]["endpoint"] = rerank_url

    # 功能级探测：真正发一次最小请求（健康检查通过不代表接口签名对得上）
    if embed_url and checks.get("embed", {}).get("ok"):
        checks["embed"]["functional"] = _http_check(
            embed_url, timeout=timeout, payload={"texts": ["probe"], "batch_size": 1}
        )
    if rerank_url and checks.get("rerank", {}).get("ok"):
        checks["rerank"]["functional"] = _http_check(
            rerank_url, timeout=timeout,
            payload={"query": "probe", "passages": ["a", "b"], "top_k": 2},
        )
    return checks


def cmd_services(_: argparse.Namespace) -> int:
    checks = check_services()
    if not checks:
        print("未配置 REMOTE_LLM_BASE_URL / REMOTE_EMBED_URL / REMOTE_RERANK_URL", file=sys.stderr)
        return 2
    print("== 远端服务探测 ==")
    for name, info in checks.items():
        mark = "OK  " if info.get("ok") else "FAIL"
        print(f"  {name:<7} {mark} {info.get('status')} {info.get('latency_ms')}ms  {info.get('endpoint') or info.get('url')}")
        if not info.get("ok"):
            print(f"          {info.get('detail')[:200]}")
        fn = info.get("functional")
        if fn is not None:
            print(f"          功能调用: {'OK' if fn.get('ok') else 'FAIL'} {str(fn.get('detail'))[:160]}")
    return 0 if all(i.get("ok") for i in checks.values()) else 1


def cmd_corpus(args: argparse.Namespace) -> int:
    env = read_env()
    dsn = args.url or _cfg(env, "REMOTE_CORPUS_URL") or _cfg(env, "DB_SOURCE_REMOTE_URL")
    if not dsn:
        print("未配置 REMOTE_CORPUS_URL", file=sys.stderr)
        return 2
    info = probe_corpus(
        dsn,
        timeout=_cfg_int(env, "DB_SOURCE_PROBE_TIMEOUT", 8),
        schema=_cfg(env, "REMOTE_CORPUS_SCHEMA", "legal_corpus"),
        expected_dim=_cfg_int(env, "EMBEDDING_DIMENSION", 1024),
    )
    print("== 远端语料库探测 ==")
    _print_probe("corpus", info)
    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0 if info["ok"] else 1


def cmd_readiness(_: argparse.Namespace) -> int:
    """一键验收：三个模型服务 + 语料库 + 本地兜底，给出能否切换的结论。"""
    env = read_env()
    print("== 远端就绪度 ==")
    services = check_services(env)
    for name, info in services.items():
        mark = "OK  " if info.get("ok") else "FAIL"
        print(f"  服务 {name:<7} {mark} {info.get('status')} {info.get('latency_ms')}ms")
        if not info.get("ok"):
            print(f"       {str(info.get('detail'))[:180]}")

    dsn = _cfg(env, "REMOTE_CORPUS_URL") or _cfg(env, "DB_SOURCE_REMOTE_URL")
    corpus = probe_corpus(dsn, schema=_cfg(env, "REMOTE_CORPUS_SCHEMA", "legal_corpus")) if dsn else None
    if corpus:
        mark = "OK  " if corpus.get("ok") else "FAIL"
        print(f"  语料库      {mark} {corpus.get('target')} {corpus.get('latency_ms')}ms")
        for item in corpus.get("fatal", []):
            print(f"       fatal: {item}")
        for item in corpus.get("warnings", []):
            print(f"       warn : {item}")
        d = corpus.get("details", {})
        if d.get("table_count"):
            print(f"       表数 {d.get('table_count')} / 向量 {d.get('vector_type')}({d.get('vector_dim')}) / ANN {d.get('ann_indexes')}")
            print(f"       规模 legal_chunk≈{d.get('legal_chunk_rows')}  embedding≈{d.get('legal_chunk_embedding_rows')}")
    else:
        print("  语料库      -    未配置 REMOTE_CORPUS_URL")

    local = probe(_cfg(env, "DB_SOURCE_LOCAL_URL") or _cfg(env, "DATABASE_URL"), deep=False)
    print(f"  本地兜底    {'OK  ' if local['ok'] else 'FAIL'} {local.get('target')} {local.get('latency_ms')}ms")

    services_ok = bool(services) and all(i.get("ok") and (i.get("functional") is None or i["functional"].get("ok")) for i in services.values())
    corpus_ok = bool(corpus and corpus.get("ok"))
    print("\n结论:")
    print(f"  模型服务：{'全部可用' if services_ok else '存在不可用项'}")
    print(f"  语料库：{'可连接' if corpus_ok else '不可用或未配置'}")
    if corpus_ok:
        compat = (corpus.get("details") or {}).get("app_compat_tables") or []
        if len(compat) < len(APP_EXPECTED_TABLES):
            print("  ⚠ 语料库 schema 与应用期望不一致，不能直接切 DATABASE_URL（需视图/迁移/改代码三选一）")
    return 0 if (services_ok and corpus_ok) else 1


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="数据源可替换层（本地兜底 / 远端接管）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("status", help="全景状态")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("resolve", help="解析当前应连的数据源")
    p.add_argument("--json", action="store_true")
    p.add_argument("--shallow", action="store_true", help="只做连通性探测")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("probe", help="探测某个数据源")
    p.add_argument("target", help="local | remote | 直接给连接串")
    p.set_defaults(func=cmd_probe)

    p = sub.add_parser("switch", help="切换模式")
    p.add_argument("mode", choices=["local", "remote"])
    p.set_defaults(func=cmd_switch)

    p = sub.add_parser("set-remote", help="写入远端连接串")
    p.add_argument("url")
    p.add_argument("--activate", action="store_true", help="同时切到 remote 模式")
    p.add_argument("--force", action="store_true", help="探测失败也写入")
    p.set_defaults(func=cmd_set_remote)

    p = sub.add_parser("verify", help="校验远端是否可接管")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("services", help="探测远端 LLM / BGE-M3 / reranker")
    p.set_defaults(func=cmd_services)

    p = sub.add_parser("corpus", help="探测远端语料库（legal_corpus schema）")
    p.add_argument("--url", default="", help="覆盖 REMOTE_CORPUS_URL")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_corpus)

    p = sub.add_parser("readiness", help="一键验收：模型服务 + 语料库 + 本地兜底")
    p.set_defaults(func=cmd_readiness)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
