"""Evaluate the source-local filtered vector path introduced after c606579.

Read-only: this script does not rebuild indexes or write business tables.  It
stores only a JSON report under outputs/acceptance/.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.service.embedding_service import create_embedding, embedding_metadata  # noqa: E402
from app.service.hybrid_retrieval_service import hybrid_search  # noqa: E402
from app.service.vector_store_service import (  # noqa: E402
    _build_pgvector_source_rerank_query,
    vector_search,
)


QUERIES = [
    ("ontario-eviction", "安大略房东以自住为由驱逐租客，善意要求与 s.57 救济"),
    ("criminal-sentencing", "加拿大刑事量刑中醉酒、过失与既往记录的加重减轻"),
    ("contract", "显失公平交易与不当影响合同撤销"),
]


def _plan_nodes(value):
    nodes = []
    if isinstance(value, dict):
        if value.get("Node Type"):
            nodes.append(value)
        for child in value.values():
            nodes.extend(_plan_nodes(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(_plan_nodes(child))
    return nodes


def _timed_vector(query: str) -> dict:
    started = time.perf_counter()
    result = vector_search(query, module="canada", source_filter="law", limit=24)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    return {
        "total_ms": elapsed,
        "items": result.get("total", 0),
        "status": result.get("status"),
        "stats": result.get("retrieval_stats") or {},
        "top5": [
            {
                "title": item.get("title"),
                "citation": item.get("citation"),
                "source_code": item.get("source_code"),
                "score": item.get("score"),
            }
            for item in (result.get("items") or [])[:5]
        ],
    }


def _timed_hybrid(query: str) -> dict:
    started = time.perf_counter()
    result = hybrid_search(query, module="canada", source_filter="all", limit=8)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    return {
        "total_ms": elapsed,
        "retrieval_wall_ms": (result.get("channels") or {}).get("timing_ms", {}).get(
            "partition_retrieval_wall", 0
        ),
        "partitions": (result.get("channels") or {}).get("partitions") or [],
        "source_kinds": sorted(
            {str(item.get("source_kind") or "") for item in result.get("items") or []}
        ),
        "top5": [
            {
                "title": item.get("title"),
                "citation": item.get("citation"),
                "source_kind": item.get("source_kind"),
                "jurisdiction_match": (item.get("rerank_signals") or {}).get(
                    "jurisdiction_match", False
                ),
            }
            for item in (result.get("items") or [])[:5]
        ],
    }


def main() -> int:
    vector_runs = []
    for query_id, query in QUERIES:
        runs = [_timed_vector(query), _timed_vector(query)]
        vector_runs.append({"id": query_id, "query": query, "runs": runs})
        print(
            query_id,
            [(run["items"], run["total_ms"], run["stats"].get("strategy")) for run in runs],
            flush=True,
        )

    query = QUERIES[0][1]
    vector = create_embedding(query)
    sql, params = _build_pgvector_source_rerank_query(
        query,
        vector,
        meta=embedding_metadata(),
        module="canada",
        source_filter="law",
        filters=None,
        candidate_limit=500,
        outer_limit=1000,
    )
    with engine.connect() as conn:
        raw_plan = conn.execute(text("EXPLAIN (FORMAT JSON) " + sql), params).scalar()
    plan = raw_plan if isinstance(raw_plan, list) else json.loads(raw_plan)
    nodes = _plan_nodes(plan)
    plan_result = {
        "has_seq_scan": any(
            node.get("Node Type") in {"Seq Scan", "Parallel Seq Scan"} for node in nodes
        ),
        "indexes": sorted(
            {str(node.get("Index Name")) for node in nodes if node.get("Index Name")}
        ),
        "node_types": sorted({str(node.get("Node Type")) for node in nodes}),
    }

    hybrid_runs = [_timed_hybrid(query) for _ in range(3)]
    output = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "vector_runs": vector_runs,
        "plan": plan_result,
        "hybrid_ontario_runs": hybrid_runs,
    }
    output_path = Path("outputs/acceptance/filtered_rerank_v3.json")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False)[:2000], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
