from __future__ import annotations

"""Explicit ANN index management.

Nothing in this module builds an index during application startup.  A caller
must inspect the plan and pass the confirmation token, which keeps local
experiments separate from corpus/context migrations.
"""

import math
from time import perf_counter

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text
from app.service.vector_store_service import embedding_metadata, get_vector_status


IVFFLAT_CONFIRM_TOKEN = "BUILD_LOCAL_IVFFLAT"


def _ident(value: str) -> str:
    clean = repair_text(value)
    if not clean.replace("_", "").isalnum() or not clean[0].isalpha():
        raise ValueError("Unsafe SQL identifier")
    return clean


def get_ivfflat_build_plan(*, lists: int | None = None, half_precision: bool = False) -> dict:
    status = get_vector_status()
    total = int(status.get("total_embeddings") or 0)
    configured_lists = max(1, min(int(lists or settings.rag_ivfflat_lists), 32768))
    recommended_lists = max(1, int(math.sqrt(total))) if total else configured_lists
    meta = embedding_metadata()
    dimension = int(meta.get("dimension") or settings.embedding_dimension)
    index_name = _ident(
        f"idx_rag_embeddings_{'half_' if half_precision else ''}ivfflat_{configured_lists}"
    )
    provider = repair_text(meta.get("provider")).replace("'", "''")
    model = repair_text(meta.get("model")).replace("'", "''")
    expression = (
        f"((embedding_vector::halfvec({dimension})) halfvec_cosine_ops)"
        if half_precision
        else "(embedding_vector vector_cosine_ops)"
    )
    sql = (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {index_name} "
        f"ON rag_chunk_embeddings USING ivfflat {expression} "
        f"WITH (lists = {configured_lists}) "
        "WHERE embedding_vector IS NOT NULL "
        f"AND embedding_provider = '{provider}' AND embedding_model = '{model}'"
    )
    return {
        "status": "plan_only",
        "will_execute": False,
        "migration_started": False,
        "index_name": index_name,
        "index_type": "ivfflat_halfvec" if half_precision else "ivfflat_float32",
        "rows": total,
        "dimension": dimension,
        "lists": configured_lists,
        "probes": int(settings.rag_ivfflat_probes),
        "recommended_lists_sqrt_rows": recommended_lists,
        "sql": sql,
        "confirmation_token": IVFFLAT_CONFIRM_TOKEN,
        "warnings": [
            "Build on a local evaluation copy first.",
            "CREATE INDEX is independent of contextual-prefix migration.",
            "Measure exact-search recall before activating this index.",
        ],
    }


def build_local_ivfflat_index(
    *, confirmation_token: str, lists: int | None = None, half_precision: bool = False
) -> dict:
    if is_sqlite():
        raise RuntimeError("IVFFlat requires PostgreSQL with pgvector")
    if confirmation_token != IVFFLAT_CONFIRM_TOKEN:
        raise PermissionError("Explicit IVFFlat confirmation token required")
    plan = get_ivfflat_build_plan(lists=lists, half_precision=half_precision)
    started = perf_counter()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SET statement_timeout = 0"))
        conn.execute(text(plan["sql"]))
        index_def = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname=:name"),
            {"name": plan["index_name"]},
        ).scalar()
    return {
        **plan,
        "status": "completed",
        "will_execute": True,
        "index_definition": repair_text(index_def),
        "duration_seconds": round(perf_counter() - started, 3),
    }

