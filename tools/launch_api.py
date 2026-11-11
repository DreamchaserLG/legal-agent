"""统一启动入口：先解析数据源，再启动 uvicorn。

为什么需要它
------------
应用读 `DATABASE_URL` 的时机是 `app/core/config.py` 的模块导入期，且 `load_dotenv()`
默认不覆盖已存在的环境变量。所以只要在 **导入 app 之前** 把环境变量设好，
就能在不改动任何应用代码的前提下切换数据源 —— 后续从仓库拉取新版本覆盖 `app/`，
这层逻辑依然有效。

启动流程
--------
1. tools/db_source.py 决定连 local 还是 remote（含探测与自动兜底）
2. 注入 DATABASE_URL + 远端模式下的安全覆盖项（禁止启动时建索引/灌数据）
3. 在同一进程内 uvicorn.run("app.main:app")

用法
----
  python tools/launch_api.py                      # 默认 0.0.0.0:7000
  python tools/launch_api.py --port 8000
  python tools/launch_api.py --no-resolve         # 跳过解析，直接用 .env 的 DATABASE_URL
  python tools/launch_api.py --check              # 只打印决策，不启动
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = Path(__file__).resolve().parent


def _log(message: str) -> None:
    line = f"[{datetime.now().astimezone().isoformat(timespec='seconds')}] {message}"
    print(line, flush=True)
    try:
        log_file = ROOT / "logs" / "db_source.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(f"{line}\n")
    except Exception:  # noqa: BLE001
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="数据源感知的 API 启动器")
    parser.add_argument("--host", default=os.getenv("APP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("APP_PORT", "7000")))
    parser.add_argument("--no-resolve", action="store_true", help="跳过数据源解析，直接用 .env")
    parser.add_argument("--shallow", action="store_true", help="只做连通性探测，不查 schema")
    parser.add_argument("--check", action="store_true", help="只输出决策与注入变量，不启动服务")
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))

    injected: dict[str, str] = {}
    if args.no_resolve:
        _log("数据源解析已跳过（--no-resolve），直接使用 .env 中的 DATABASE_URL")
    else:
        import db_source  # 同目录模块

        try:
            state = db_source.resolve(deep=not args.shallow, write_state=True)
        except Exception as exc:  # noqa: BLE001
            # 仅在「远端不可用且显式关闭兜底」时才会走到这里：按配置要求拒绝启动
            _log(f"FATAL 数据源解析失败，拒绝启动: {type(exc).__name__}: {exc}")
            return 3
        injected["DATABASE_URL"] = state["url"]
        injected["DB_SOURCE_ACTIVE"] = state["active"]
        for key, value in state["overrides"].items():
            injected[key] = value
        _log(
            "数据源决策: mode={mode} active={active} fallback={fb} target={target} reason={reason}".format(
                mode=state["mode"],
                active=state["active"],
                fb=state["fallback_used"],
                target=db_source.describe_url(state["url"]),
                reason=state["reason"],
            )
        )

    for key, value in injected.items():
        os.environ[key] = value

    if injected:
        _log(f"已注入环境变量: {json.dumps(injected, ensure_ascii=False)}")

    if args.check:
        print(json.dumps({"host": args.host, "port": args.port, "env": injected}, ensure_ascii=False, indent=2))
        return 0

    import uvicorn

    _log(f"启动 uvicorn  app.main:app  {args.host}:{args.port}  workers=1")
    uvicorn.run(
        "app.main:app",
        host=args.host,
        port=args.port,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
