from __future__ import annotations

import statistics
import time
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.embedding_service import create_embedding, embedding_metadata
from app.service.vector_index_management_service import get_hnsw_index_state, verify_hnsw_query_plan


SAMPLE_CONFIRM_TOKEN = "BUILD_HNSW_SAMPLE"
SAMPLE_TABLE = "rag_hnsw_eval_sample"
SAMPLE_INDEX = "idx_rag_hnsw_eval_sample_vector"


def recall_at_k(expected: list[int], actual: list[int], k: int) -> float:
    truth = set(expected[: max(1, int(k))])
    if not truth:
        return 0.0
    return len(truth & set(actual[: max(1, int(k))])) / len(truth)


def summarize_backend_trials(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [float(row.get("latency_ms") or 0) for row in rows]
    recalls = [
        float(row.get("recall_at_k"))
        if row.get("recall_at_k") is not None
        else recall_at_k(
            list(row.get("exact_ids") or []),
            list(row.get("ann_ids") or row.get("candidate_ids") or []),
            5,
        )
        for row in rows
    ]
    return {
        "queries": len(rows),
        "recall": {"recall@5": round(statistics.mean(recalls), 4) if recalls else 0.0},
        "latency_ms": {
            "mean": round(statistics.mean(latencies), 3) if latencies else 0.0,
            "max": round(max(latencies), 3) if latencies else 0.0,
        },
    }


def load_canada_queries(path: str) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("queries") or payload.get("cases") or []
    rows = [dict(item) for item in payload if isinstance(item, dict)]
    for item in rows:
        if not item.get("domain"):
            item_id = str(item.get("id") or "canadian_general")
            item["domain"] = item_id.split("_", 1)[0]
    return rows


def local_vector_experiment_manifest() -> dict[str, Any]:
    return {
        "migration_started": False,
        "active_backend": "pgvector_hnsw",
        "experiments": [
            {
                "id": "pgvector_hnsw_sample",
                "purpose": "isolated_real_ann_quality_and_latency_gate",
                "parameters": {"m": [8, 16], "ef_construction": [64, 128], "ef_search": [40, 80, 160]},
            },
            {"id": "exact_cosine", "purpose": "ground_truth_only"},
        ],
        "acceptance": {
            "planner_uses_hnsw": True,
            "sequential_scan_for_ann": False,
            "minimum_recall_at_10": 0.9,
            "stale_embedding_hits": 0,
        },
    }


def prepare_local_vector_sample(
    *, confirmation_token: str, sample_size: int = 50_000,
    m: int = 16, ef_construction: int = 64,
) -> dict[str, Any]:
    if is_sqlite():
        raise RuntimeError("A real HNSW sample requires PostgreSQL with pgvector")
    if confirmation_token != SAMPLE_CONFIRM_TOKEN:
        raise PermissionError("Explicit HNSW sample confirmation token required")
    size = max(1_000, min(int(sample_size), 500_000))
    m = max(2, min(int(m), 100))
    ef_construction = max(4, min(int(ef_construction), 1000))
    meta = embedding_metadata()
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(f"DROP TABLE IF EXISTS {SAMPLE_TABLE}"))
        conn.execute(
            text(
                f"""
                CREATE UNLOGGED TABLE {SAMPLE_TABLE} AS
                SELECT chunk_id, embedding_vector
                FROM rag_chunk_embeddings TABLESAMPLE SYSTEM (10) REPEATABLE (20261103)
                WHERE embedding_vector IS NOT NULL
                  AND embedding_provider = :provider
                  AND embedding_model = :model
                LIMIT :sample_size
                """
            ),
            {"provider": meta["provider"], "model": meta["model"], "sample_size": size},
        )
        count = int(conn.execute(text(f"SELECT COUNT(*) FROM {SAMPLE_TABLE}")).scalar() or 0)
        if count < 1_000:
            raise RuntimeError(f"HNSW sample is too small: {count}")
        conn.execute(text(f"ALTER TABLE {SAMPLE_TABLE} ADD PRIMARY KEY (chunk_id)"))
        conn.execute(
            text(
                f"CREATE INDEX {SAMPLE_INDEX} ON {SAMPLE_TABLE} USING hnsw "
                f"(embedding_vector vector_cosine_ops) WITH (m = {m}, ef_construction = {ef_construction})"
            )
        )
        conn.execute(text(f"ANALYZE {SAMPLE_TABLE}"))
    return {
        "status": "ready", "table": SAMPLE_TABLE, "index": SAMPLE_INDEX,
        "sample_size": count, "m": m, "ef_construction": ef_construction,
        "source_rows_modified": 0, "migration_started": False,
    }


def _search_sample(vector_text: str, *, k: int, exact: bool, ef_search: int) -> tuple[list[int], float, list[Any]]:
    explain = f"""
        EXPLAIN (FORMAT JSON)
        SELECT chunk_id FROM {SAMPLE_TABLE}
        ORDER BY embedding_vector <=> CAST(:query_vector AS vector)
        LIMIT :k
    """
    query = f"""
        SELECT chunk_id FROM {SAMPLE_TABLE}
        ORDER BY embedding_vector <=> CAST(:query_vector AS vector)
        LIMIT :k
    """
    started = time.perf_counter()
    with engine.begin() as conn:
        if exact:
            conn.execute(text("SET LOCAL enable_indexscan = off"))
            conn.execute(text("SET LOCAL enable_bitmapscan = off"))
        else:
            conn.execute(text(f"SET LOCAL hnsw.ef_search = {max(1, min(int(ef_search), 1000))}"))
        plan = conn.execute(text(explain), {"query_vector": vector_text, "k": k}).scalar()
        rows = conn.execute(text(query), {"query_vector": vector_text, "k": k}).scalars().all()
    return [int(value) for value in rows], (time.perf_counter() - started) * 1000, plan


def benchmark_local_vector_sample(
    queries: list[str], *, sample_size: int = 50_000, k: int = 10, ef_search: int = 80,
) -> dict[str, Any]:
    if is_sqlite():
        raise RuntimeError("A real HNSW benchmark requires PostgreSQL")
    with engine.connect() as conn:
        exists = bool(conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": SAMPLE_INDEX}).scalar())
        count = int(conn.execute(text(f"SELECT COUNT(*) FROM {SAMPLE_TABLE}" if exists else "SELECT 0")).scalar() or 0)
    if not exists or count < min(1_000, int(sample_size)):
        raise RuntimeError("Prepare the isolated HNSW sample before benchmarking")
    rows: list[dict[str, Any]] = []
    for query in queries:
        vector = create_embedding(str(query))
        vector_text = "[" + ",".join(str(float(value)) for value in vector) + "]"
        exact_ids, exact_ms, _ = _search_sample(vector_text, k=k, exact=True, ef_search=ef_search)
        ann_ids, ann_ms, plan = _search_sample(vector_text, k=k, exact=False, ef_search=ef_search)
        plan_text = str(plan)
        rows.append({
            "query": str(query), "exact_ids": exact_ids, "ann_ids": ann_ids,
            "recall_at_k": recall_at_k(exact_ids, ann_ids, k),
            "exact_latency_ms": round(exact_ms, 3), "latency_ms": round(ann_ms, 3),
            "planner_uses_hnsw": SAMPLE_INDEX in plan_text,
            "sequential_scan": "Seq Scan" in plan_text,
        })
    summary = summarize_backend_trials(rows)
    summary.update({
        "recall_at_k": round(statistics.mean([row["recall_at_k"] for row in rows]), 4) if rows else 0.0,
        "planner_uses_hnsw": all(row["planner_uses_hnsw"] for row in rows),
        "sequential_scan": any(row["sequential_scan"] for row in rows),
    })
    return {
        "backend": "pgvector_hnsw", "sample_size": count, "k": int(k),
        "ef_search": int(ef_search), "trials": rows, "summary": summary,
        "passed": bool(rows and summary["planner_uses_hnsw"] and not summary["sequential_scan"] and summary["recall_at_k"] >= 0.9),
        "migration_started": False,
    }


def run_local_vector_benchmark(
    queries: list[str], *, k: int = 10, exact_timeout_ms: int = 90_000,
) -> dict[str, Any]:
    del exact_timeout_ms
    state = get_hnsw_index_state()
    if not state.get("valid") or not state.get("ready"):
        return {
            "status": "blocked", "reason": "A valid local HNSW index is required",
            "ann_index_ready": False, "migration_started": False,
        }
    return {
        "status": "ready", "ann_index_ready": True,
        "planner": verify_hnsw_query_plan(limit=k),
        "queries": [str(value) for value in queries], "migration_started": False,
    }
