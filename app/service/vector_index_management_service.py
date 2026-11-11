from __future__ import annotations

import os

import json
import re
from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, is_sqlite
from app.service.embedding_service import embedding_metadata


HNSW_CONFIRM_TOKEN = "BUILD_LOCAL_HNSW"
HNSW_INDEX_NAME = "idx_rag_chunk_embeddings_active_hnsw"


def _literal(value: str) -> str:
    return str(value or "").replace("'", "''")


def get_hnsw_build_plan(*, m: int | None = None, ef_construction: int | None = None) -> dict[str, Any]:
    meta = embedding_metadata()
    configured_m = max(2, min(int(m or settings.rag_hnsw_m), 100))
    configured_ef = max(4, min(int(ef_construction or settings.rag_hnsw_ef_construction), 1000))
    provider = _literal(meta["provider"])
    model = _literal(meta["model"])
    sql = (
        f"CREATE INDEX CONCURRENTLY {HNSW_INDEX_NAME} "
        "ON rag_chunk_embeddings USING hnsw (embedding_vector vector_cosine_ops) "
        f"WITH (m = {configured_m}, ef_construction = {configured_ef}) "
        "WHERE embedding_vector IS NOT NULL "
        f"AND embedding_provider = '{provider}' AND embedding_model = '{model}'"
    )
    return {
        "backend": "pgvector_hnsw",
        "index_name": HNSW_INDEX_NAME,
        "index_type": "hnsw",
        "provider": meta["provider"],
        "model": meta["model"],
        "dimension": int(meta["dimension"]),
        "m": configured_m,
        "ef_construction": configured_ef,
        "ef_search": int(settings.rag_hnsw_ef_search),
        "sql": sql,
        "will_execute": False,
        "migration_started": False,
        "confirmation_token": HNSW_CONFIRM_TOKEN,
    }


# [local-patch:ivfflat-compat]
def _compat_ann_index_state(conn) -> dict[str, Any] | None:
    """RAG_ANN_COMPAT=ivfflat_bq 时，把 bit 汉明量化索引当作可用 ANN。"""
    if os.getenv("RAG_ANN_COMPAT", "").strip().lower() != "ivfflat_bq":
        return None
    row = conn.execute(
        text(
            """
            SELECT c.relname AS index_name, i.indisvalid, i.indisready,
                   pg_get_indexdef(c.oid) AS definition
            FROM pg_class c
            JOIN pg_index i ON i.indexrelid = c.oid
            WHERE c.relname IN ('idx_rag_embeddings_ivfflat_bq', 'idx_rag_chunk_embeddings_active_hnsw')
            ORDER BY CASE WHEN c.relname LIKE '%ivfflat%' THEN 0 ELSE 1 END
            LIMIT 1
            """
        )
    ).mappings().first()
    if not row:
        return None
    state = dict(row)
    return {
        "available": True,
        "index_name": state["index_name"],
        "index_type": "ivfflat_bq",
        "compat": True,
        "valid": bool(state["indisvalid"]),
        "ready": bool(state["indisready"]),
        "definition": state["definition"],
    }


def get_hnsw_index_state() -> dict[str, Any]:
    if is_sqlite():
        return {"available": False, "reason": "pgvector HNSW requires PostgreSQL"}
    with engine.connect() as conn:
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
            {"index_name": HNSW_INDEX_NAME},
        ).mappings().first()
    if not row:
        # 原 with 块已关闭连接，这里新开一个
        with engine.connect() as compat_conn:
            compat_state = _compat_ann_index_state(compat_conn)
        if compat_state:
            return compat_state
        return {"available": False, "index_name": HNSW_INDEX_NAME, "valid": False, "ready": False}
    state = dict(row)
    state.update({
        "available": True,
        "valid": bool(state.pop("indisvalid")),
        "ready": bool(state.pop("indisready")),
    })
    return state


def build_local_hnsw_index(
    *,
    confirmation_token: str,
    m: int | None = None,
    ef_construction: int | None = None,
    maintenance_work_mem: str = "1800MB",
) -> dict[str, Any]:
    if is_sqlite():
        raise RuntimeError("pgvector HNSW requires PostgreSQL")
    if confirmation_token != HNSW_CONFIRM_TOKEN:
        raise PermissionError("Explicit HNSW confirmation token required")
    if not re.fullmatch(r"\d+(?:MB|GB)", maintenance_work_mem.upper()):
        raise ValueError("maintenance_work_mem must use an MB or GB value")
    existing = get_hnsw_index_state()
    if existing.get("valid") and existing.get("ready"):
        return {"status": "already_ready", "index": existing, "migration_started": False}
    plan = get_hnsw_build_plan(m=m, ef_construction=ef_construction)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("SET statement_timeout = 0"))
        conn.execute(text(f"SET maintenance_work_mem = '{maintenance_work_mem.upper()}'"))
        conn.execute(text("SET max_parallel_maintenance_workers = 4"))
        conn.execute(text(plan["sql"]))
    state = get_hnsw_index_state()
    if not state.get("valid") or not state.get("ready"):
        raise RuntimeError(f"HNSW index is not valid and ready: {state}")
    return {"status": "built", "index": state, "migration_started": False}


def _plan_nodes(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if "Node Type" in value:
            nodes.append(value)
        for child in value.values():
            nodes.extend(_plan_nodes(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_plan_nodes(child))
    return nodes


def verify_hnsw_query_plan(*, limit: int = 10) -> dict[str, Any]:
    state = get_hnsw_index_state()
    if not state.get("valid") or not state.get("ready"):
        return {"verified": False, "reason": "HNSW index is not valid and ready", "index": state}
    meta = embedding_metadata()
    # A unit vector avoids undefined cosine distance for an all-zero probe
    # while keeping this planner-only check independent of the embedder.
    probe = ["0"] * int(meta["dimension"])
    probe[0] = "1"
    query_vector = "[" + ",".join(probe) + "]"
    sql = """
        EXPLAIN (FORMAT JSON)
        SELECT chunk_id
        FROM rag_chunk_embeddings
        WHERE embedding_vector IS NOT NULL
          AND embedding_provider = :provider
          AND embedding_model = :model
        ORDER BY embedding_vector <=> CAST(:query_vector AS vector)
        LIMIT :limit
    """
    with engine.begin() as conn:
        conn.execute(text(f"SET LOCAL hnsw.ef_search = {int(settings.rag_hnsw_ef_search)}"))
        raw = conn.execute(
            text(sql),
            {
                "provider": meta["provider"], "model": meta["model"],
                "query_vector": query_vector, "limit": max(1, min(int(limit), 100)),
            },
        ).scalar()
    plan = raw if isinstance(raw, list) else json.loads(raw)
    nodes = _plan_nodes(plan)
    used_indexes = [str(node.get("Index Name")) for node in nodes if node.get("Index Name")]
    sequential_scan = any(node.get("Node Type") == "Seq Scan" for node in nodes)
    return {
        "verified": HNSW_INDEX_NAME in used_indexes and not sequential_scan,
        "used_indexes": used_indexes,
        "sequential_scan": sequential_scan,
        "index": state,
        "plan": plan,
    }
