"""Verify the live ANN-first retrieval shape without writing business data."""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.service.embedding_service import create_embedding, embedding_metadata  # noqa: E402
from app.service.vector_index_management_service import HNSW_INDEX_NAME  # noqa: E402
from app.service.vector_store_service import (  # noqa: E402
    _build_pgvector_ann_query,
    _configure_hnsw_search,
)


DEFAULT_QUERY = "Ontario sentencing principles prior criminal record intoxication"


def _nodes(value):
    output = []
    if isinstance(value, dict):
        if value.get("Node Type"):
            output.append(value)
        for child in value.values():
            output.extend(_nodes(child))
    elif isinstance(value, list):
        for child in value:
            output.extend(_nodes(child))
    return output


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = min(round((len(ordered) - 1) * fraction), len(ordered) - 1)
    return ordered[index]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--query", default=DEFAULT_QUERY)
    parser.add_argument("--limit", type=int, default=24)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--random-page-cost", type=float)
    args = parser.parse_args(argv)

    result_limit = max(1, min(args.limit, 100))
    ann_limit = result_limit * 4
    query_vector = create_embedding(args.query)
    sql, params = _build_pgvector_ann_query(
        query_vector,
        meta=embedding_metadata(),
        module="canada",
        source_filter="all",
        filters=None,
        ann_limit=ann_limit,
    )

    with engine.connect() as conn:
        _configure_hnsw_search(conn, ann_limit=ann_limit)
        if args.random_page_cost is not None:
            conn.execute(text(f"SET LOCAL random_page_cost = {float(args.random_page_cost)}"))
        raw_plan = conn.execute(text("EXPLAIN (FORMAT JSON) " + sql), params).scalar()
        plan = raw_plan if isinstance(raw_plan, list) else json.loads(raw_plan)
        plan_nodes = _nodes(plan)
        indexes = {str(node.get("Index Name")) for node in plan_nodes if node.get("Index Name")}
        node_types = {str(node.get("Node Type")) for node in plan_nodes}
        uses_hnsw = HNSW_INDEX_NAME in indexes
        has_seq_scan = bool({"Seq Scan", "Parallel Seq Scan"} & node_types)

        for _ in range(2):
            conn.execute(text(sql), params).all()
        samples = []
        rows = 0
        for _ in range(max(1, args.runs)):
            started = time.perf_counter()
            result = conn.execute(text(sql), params).all()
            samples.append((time.perf_counter() - started) * 1000)
            rows = len(result)

    p50 = _percentile(samples, 0.50)
    p95 = _percentile(samples, 0.95)
    print(f"plan_uses_hnsw={str(uses_hnsw).lower()}")
    print(f"plan_has_seq_scan={str(has_seq_scan).lower()}")
    print(f"p50_ms={p50:.3f} p95_ms={p95:.3f} rows={rows}")
    passed = uses_hnsw and not has_seq_scan and p50 < 50 and p95 < 150 and rows >= result_limit
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
