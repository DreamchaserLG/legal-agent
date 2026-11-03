"""Plan, build, or inspect the one supported local pgvector HNSW index.

Examples:
  python scripts/build_hnsw_index.py --plan
  python scripts/build_hnsw_index.py --monitor
  python scripts/build_hnsw_index.py --build --confirm BUILD_LOCAL_HNSW
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.service.vector_index_management_service import (  # noqa: E402
    HNSW_CONFIRM_TOKEN,
    build_local_hnsw_index,
    get_hnsw_build_plan,
    get_hnsw_index_state,
)


def progress() -> list[dict]:
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(
            text(
                "SELECT pid, phase, blocks_done, blocks_total, tuples_done, tuples_total "
                "FROM pg_stat_progress_create_index"
            )
        ).mappings().all()]


def main() -> int:
    parser = argparse.ArgumentParser()
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--plan", action="store_true")
    action.add_argument("--monitor", action="store_true")
    action.add_argument("--build", action="store_true")
    parser.add_argument("--confirm", default="")
    parser.add_argument("--m", type=int, default=16)
    parser.add_argument("--ef-construction", type=int, default=64)
    parser.add_argument("--work-mem", default="1800MB")
    args = parser.parse_args()

    if args.build:
        result = build_local_hnsw_index(
            confirmation_token=args.confirm,
            m=args.m,
            ef_construction=args.ef_construction,
            maintenance_work_mem=args.work_mem,
        )
    elif args.plan:
        result = get_hnsw_build_plan(m=args.m, ef_construction=args.ef_construction)
    else:
        result = {"index": get_hnsw_index_state(), "progress": progress()}
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    if args.build and args.confirm != HNSW_CONFIRM_TOKEN:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
