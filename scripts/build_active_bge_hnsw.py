"""Build and verify the partial HNSW index used by live BGE-M3 cosine queries."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from app.core.database import engine


INDEX_NAME = "idx_rag_chunk_embeddings_active_hnsw"
PROVIDER = "sentence_transformers"
MODEL = "BAAI/bge-m3"


def _index_state(conn) -> dict | None:
    row = conn.execute(
        text(
            """
            SELECT c.relname AS index_name, i.indisvalid, i.indisready,
                   pg_relation_size(c.oid) AS bytes, pg_get_indexdef(c.oid) AS definition
            FROM pg_class c
            JOIN pg_index i ON i.indexrelid = c.oid
            WHERE c.oid = to_regclass(:index_name)
            """
        ),
        {"index_name": INDEX_NAME},
    ).mappings().first()
    return dict(row) if row else None


def main() -> int:
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
            state = _index_state(conn)
            if state and state["indisvalid"] and state["indisready"]:
                print(json.dumps({"status": "already_valid", "index": state}, ensure_ascii=False, default=str))
                print("[RESULT]: SUCCESS")
                return 0
            if state:
                conn.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX_NAME}"))

            provider = PROVIDER.replace("'", "''")
            model = MODEL.replace("'", "''")
            conn.execute(
                text(
                    f"""
                    CREATE INDEX CONCURRENTLY {INDEX_NAME}
                    ON rag_chunk_embeddings USING hnsw (embedding_vector vector_cosine_ops)
                    WITH (m = 16, ef_construction = 64)
                    WHERE embedding_vector IS NOT NULL
                      AND embedding_provider = '{provider}'
                      AND embedding_model = '{model}'
                    """
                )
            )
            state = _index_state(conn)
            if not state or not state["indisvalid"] or not state["indisready"]:
                raise RuntimeError(f"HNSW 索引构建后状态无效：{state}")
        print(json.dumps({"status": "built", "index": state}, ensure_ascii=False, default=str))
        print("[RESULT]: SUCCESS")
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        print("[RESULT]: FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
