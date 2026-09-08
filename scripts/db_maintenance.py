from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import text

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.database import engine, is_sqlite
from app.service.ingestion_task_service import ensure_ingestion_tables
from app.service.legal_data_service import ensure_legal_data_tables
from app.service.module_service import ensure_module_support_tables
from app.service.rag_service import ensure_rag_tables, get_rag_status
from app.service.risk_assessment_service import ensure_risk_assessment_tables
from app.service.user_service import ensure_user_tables
from app.service.vector_store_service import ensure_vector_tables, get_vector_status


RUNTIME_TABLES = [
    "risk_feedback_labels",
    "risk_assessment_samples",
    "agent_chat_logs",
    "agent_predictions",
    "agent_runs",
    "user_case_histories",
    "search_histories",
    "rag_chunk_embeddings",
    "rag_chunks",
    "rag_index_runs",
    "ingestion_tasks",
    "import_tasks",
    "sync_logs",
    "app_runtime_state",
]

CORPUS_TABLES = [
    "case_rule_relations",
    "canada_case_law_links",
    "case_votes",
    "legal_cases",
    "legal_rules",
    "canada_laws",
    "item_keywords",
    "source_items",
]

PRESERVED_TABLES = [
    "users",
]


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _timestamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _table_exists(conn, table_name: str) -> bool:
    if is_sqlite():
        return bool(
            conn.execute(
                text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
                {"name": table_name},
            ).scalar()
        )
    return bool(conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table_name}).scalar())


def _existing_tables(tables: list[str]) -> list[str]:
    with engine.connect() as conn:
        return [table for table in tables if _table_exists(conn, table)]


def initialize_database() -> dict:
    ensure_ingestion_tables()
    ensure_user_tables()
    ensure_module_support_tables()
    ensure_legal_data_tables()
    ensure_rag_tables()
    ensure_risk_assessment_tables()
    ensure_vector_tables()
    return {
        "status": "ok",
        "rag": get_rag_status(),
        "vector": get_vector_status(),
    }


def table_counts(tables: list[str] | None = None) -> dict[str, int]:
    target_tables = tables or (RUNTIME_TABLES + CORPUS_TABLES + PRESERVED_TABLES)
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in target_tables:
            if not _table_exists(conn, table):
                continue
            counts[table] = int(conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar() or 0)
    return counts


def backup_tables(tables: list[str], backup_dir: Path | None = None) -> dict:
    target_dir = backup_dir or Path("data") / "backups" / f"runtime-cleanup-{_timestamp()}"
    target_dir.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    with engine.connect() as conn:
        for table in _existing_tables(tables):
            rows = conn.execute(text(f"SELECT * FROM {table}")).mappings().all()
            path = target_dir / f"{table}.jsonl"
            with path.open("w", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps(dict(row), ensure_ascii=False, default=_json_default) + "\n")
            counts[table] = len(rows)
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "database_backend": "sqlite" if is_sqlite() else "postgresql",
        "tables": counts,
        "preserved_tables": PRESERVED_TABLES,
    }
    (target_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=_json_default),
        encoding="utf-8",
    )
    return {"status": "ok", "backup_dir": str(target_dir), "tables": counts}


def clear_tables(tables: list[str]) -> dict:
    existing = _existing_tables(tables)
    if not existing:
        return {"status": "ok", "cleared_tables": [], "message": "No matching tables exist."}
    if is_sqlite():
        with engine.begin() as conn:
            for table in existing:
                conn.execute(text(f"DELETE FROM {table}"))
                conn.execute(text("DELETE FROM sqlite_sequence WHERE name = :name"), {"name": table})
    else:
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE {', '.join(existing)} RESTART IDENTITY CASCADE"))
    return {"status": "ok", "cleared_tables": existing}


def command_status(args: argparse.Namespace) -> int:
    payload = {
        "status": "ok",
        "backend": "sqlite" if is_sqlite() else "postgresql",
        "counts": table_counts(),
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0


def command_init(args: argparse.Namespace) -> int:
    payload = initialize_database()
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0


def command_backup_clear(args: argparse.Namespace) -> int:
    target_tables = list(RUNTIME_TABLES)
    if args.include_corpus:
        target_tables.extend(CORPUS_TABLES)
    before = table_counts(target_tables + PRESERVED_TABLES)
    backup = backup_tables(target_tables, Path(args.backup_dir) if args.backup_dir else None)
    clear = clear_tables(target_tables)
    after = table_counts(target_tables + PRESERVED_TABLES)
    payload = {
        "status": "ok",
        "include_corpus": bool(args.include_corpus),
        "before": before,
        "backup": backup,
        "clear": clear,
        "after": after,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Initialize, inspect, backup, and clear the legal demo database.")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Print table counts.")
    status.set_defaults(func=command_status)

    init = sub.add_parser("init-vector", help="Initialize application tables, RAG tables, risk tables, and vector index.")
    init.set_defaults(func=command_init)

    backup_clear = sub.add_parser("backup-clear", help="Back up rows to JSONL, then clear active runtime tables.")
    backup_clear.add_argument("--include-corpus", action="store_true", help="Also clear source_items and derived legal corpus tables.")
    backup_clear.add_argument("--backup-dir", default="", help="Optional backup output directory.")
    backup_clear.set_defaults(func=command_backup_clear)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
