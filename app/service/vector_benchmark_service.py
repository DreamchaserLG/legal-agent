from __future__ import annotations

"""Metrics and preflight helpers for local exact-vs-ANN evaluation."""

from statistics import mean
from time import perf_counter
from typing import Iterable

from sqlalchemy import text

from app.core.database import engine
from app.service.embedding_service import create_embedding
from app.service.vector_store_service import _vector_text, embedding_metadata, get_vector_status, vector_search


LOCAL_SAMPLE_CONFIRM_TOKEN = "BUILD_LOCAL_VECTOR_SAMPLE"


def _sample_table_name(sample_size: int) -> str:
    size = max(1_000, min(int(sample_size), 500_000))
    return f"rag_vector_eval_{size}"


def prepare_local_vector_sample(
    *, confirmation_token: str, sample_size: int = 50_000, lists: int | None = None,
    build_pg_index: bool = True,
) -> dict:
    """Create an isolated deterministic sample and IVFFlat index, never touching source rows."""
    if confirmation_token != LOCAL_SAMPLE_CONFIRM_TOKEN:
        raise PermissionError("Explicit local sample confirmation token required")
    size = max(1_000, min(int(sample_size), 500_000))
    table_name = _sample_table_name(size)
    list_count = max(1, min(int(lists or round(size ** 0.5)), 4096))
    index_name = f"idx_{table_name}_ivfflat"
    started = perf_counter()
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = 0"))
        conn.execute(text(f"""
            CREATE TABLE IF NOT EXISTS {table_name} AS
            SELECT chunk_id, embedding_provider, embedding_model, embedding_vector
            FROM rag_chunk_embeddings
            WHERE embedding_vector IS NOT NULL
              AND mod(abs(hashint8(chunk_id)), 1000000) < 200000
            ORDER BY chunk_id
            LIMIT {size}
        """))
        conn.execute(text(f"ALTER TABLE {table_name} ADD PRIMARY KEY (chunk_id)")) if not conn.execute(
            text("SELECT EXISTS (SELECT 1 FROM pg_indexes WHERE indexname=:name)"),
            {"name": f"{table_name}_pkey"},
        ).scalar() else None
        if build_pg_index:
            conn.execute(text(f"""
                CREATE INDEX IF NOT EXISTS {index_name}
                ON {table_name} USING ivfflat (embedding_vector vector_cosine_ops)
                WITH (lists = {list_count})
            """))
        conn.execute(text(f"ANALYZE {table_name}"))
        count = int(conn.execute(text(f"SELECT count(*) FROM {table_name}")).scalar() or 0)
        size_bytes = int(conn.execute(text("SELECT pg_total_relation_size(CAST(:name AS regclass))"), {"name": table_name}).scalar() or 0)
    return {
        "status": "completed", "migration_started": False, "isolated_sample": True,
        "table": table_name, "rows": count, "lists": list_count if build_pg_index else None,
        "pg_index_built": bool(build_pg_index),
        "size_bytes": size_bytes, "duration_seconds": round(perf_counter() - started, 3),
    }


def benchmark_local_vector_sample(
    queries: list[str], *, sample_size: int = 50_000, k: int = 10, probes: int = 16,
) -> dict:
    table_name = _sample_table_name(sample_size)
    with engine.connect() as conn:
        if not conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table_name}).scalar():
            return {"status": "blocked", "reason": "local_sample_not_ready", "migration_started": False}
    rows: list[dict] = []
    for raw_query in queries[:100]:
        query = str(raw_query or "").strip()
        if not query:
            continue
        query_vector = _vector_text(create_embedding(query))
        params = {"query_vector": query_vector, "limit": max(1, int(k))}
        exact_started = perf_counter()
        with engine.begin() as conn:
            conn.execute(text("SET LOCAL enable_indexscan = off"))
            exact_ids = [int(row[0]) for row in conn.execute(text(f"""
                SELECT chunk_id FROM {table_name}
                ORDER BY embedding_vector <=> CAST(:query_vector AS vector)
                LIMIT :limit
            """), params).all()]
        exact_ms = (perf_counter() - exact_started) * 1000
        ann_started = perf_counter()
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL ivfflat.probes = {max(1, int(probes))}"))
            candidate_ids = [int(row[0]) for row in conn.execute(text(f"""
                SELECT chunk_id FROM {table_name}
                ORDER BY embedding_vector <=> CAST(:query_vector AS vector)
                LIMIT :limit
            """), params).all()]
        ann_ms = (perf_counter() - ann_started) * 1000
        rows.append({
            "query": query, "exact_ids": exact_ids, "candidate_ids": candidate_ids,
            "latency_ms": ann_ms, "exact_latency_ms": exact_ms,
        })
    return {
        "status": "completed", "migration_started": False, "isolated_sample": True,
        "table": table_name, "probes": probes, "summary": summarize_backend_trials(rows),
        "queries": rows,
    }


def recall_at_k(exact_ids: Iterable[int], candidate_ids: Iterable[int], k: int) -> float:
    exact = list(exact_ids)[: max(1, int(k))]
    candidate = set(list(candidate_ids)[: max(1, int(k))])
    if not exact:
        return 1.0 if not candidate else 0.0
    return len(set(exact) & candidate) / len(set(exact))


def reciprocal_rank(expected_ids: Iterable[int], candidate_ids: Iterable[int]) -> float:
    expected = set(expected_ids)
    for rank, item_id in enumerate(candidate_ids, start=1):
        if item_id in expected:
            return 1.0 / rank
    return 0.0


def summarize_backend_trials(rows: list[dict], *, k_values: tuple[int, ...] = (5, 10, 20)) -> dict:
    if not rows:
        return {"queries": 0, "recall": {}, "mrr": 0.0, "latency_ms": {}}
    latency = sorted(float(row.get("latency_ms") or 0.0) for row in rows)
    percentile = lambda p: latency[min(len(latency) - 1, int((len(latency) - 1) * p))]
    return {
        "queries": len(rows),
        "recall": {
            f"recall@{k}": round(mean(recall_at_k(row.get("exact_ids") or [], row.get("candidate_ids") or [], k) for row in rows), 4)
            for k in k_values
        },
        "mrr": round(mean(reciprocal_rank(row.get("exact_ids") or [], row.get("candidate_ids") or []) for row in rows), 4),
        "latency_ms": {
            "mean": round(mean(latency), 3), "p50": round(percentile(0.50), 3),
            "p95": round(percentile(0.95), 3), "max": round(max(latency), 3),
        },
    }


def local_vector_experiment_manifest() -> dict:
    """Return the ordered experiment matrix; this function performs no writes."""
    return {
        "migration_started": False,
        "experiments": [
            {"id": "exact_fp32", "purpose": "ground_truth", "quantization": "none"},
            {"id": "ivfflat_fp32", "purpose": "low_memory_baseline", "lists": [1024, 2048, 3072], "probes": [16, 32, 48, 64, 96]},
            {"id": "ivfflat_halfvec", "purpose": "half_precision_ablation", "rerank": "fp32"},
            {"id": "binary_candidate", "purpose": "pgvector_binary_ablation", "rerank": "fp32"},
            {"id": "faiss_ivfpq", "purpose": "true_product_quantization", "configs": ["IVF2048,PQ64x8", "IVF4096,PQ64x8"], "rerank": "fp32"},
            {"id": "remote_hnsw", "purpose": "optional_high_recall_backend"},
        ],
        "quality_gates": {
            "recall@10_min": 0.90, "mrr_regression_max": 0.03,
            "jurisdiction_error_rate_max": 0.02, "p95_latency_ms_max": 1500,
        },
    }


def run_local_vector_benchmark(
    queries: list[str], *, k: int = 10, exact_timeout_ms: int = 90_000,
) -> dict:
    """Compare configured ANN with exact search; refuses when ANN is absent."""
    status = get_vector_status()
    if not status.get("ann_index_ready"):
        return {
            "status": "blocked", "reason": "ann_index_not_ready",
            "migration_started": False, "vector_status": status,
        }
    meta = embedding_metadata()
    rows: list[dict] = []
    for raw_query in queries[:100]:
        query = str(raw_query or "").strip()
        if not query:
            continue
        vector = create_embedding(query)
        params = {
            "query_vector": _vector_text(vector), "provider": meta["provider"],
            "model": meta["model"], "limit": max(1, int(k)),
        }
        exact_started = perf_counter()
        with engine.begin() as conn:
            conn.execute(text(f"SET LOCAL statement_timeout = {max(1000, int(exact_timeout_ms))}"))
            conn.execute(text("SET LOCAL enable_indexscan = off"))
            exact_ids = [int(row[0]) for row in conn.execute(text("""
                SELECT chunk_id FROM rag_chunk_embeddings
                WHERE embedding_provider=:provider AND embedding_model=:model
                  AND embedding_vector IS NOT NULL
                ORDER BY embedding_vector <=> CAST(:query_vector AS vector)
                LIMIT :limit
            """), params).all()]
        exact_ms = (perf_counter() - exact_started) * 1000
        ann_started = perf_counter()
        ann = vector_search(query, module="canada", source_filter="all", limit=max(1, int(k)))
        ann_ms = (perf_counter() - ann_started) * 1000
        candidate_ids = [int(item.get("chunk_id") or 0) for item in ann.get("items") or []]
        rows.append({
            "query": query, "exact_ids": exact_ids, "candidate_ids": candidate_ids,
            "latency_ms": ann_ms, "exact_latency_ms": exact_ms,
        })
    return {
        "status": "completed", "migration_started": False,
        "index": status, "summary": summarize_backend_trials(rows), "queries": rows,
    }
