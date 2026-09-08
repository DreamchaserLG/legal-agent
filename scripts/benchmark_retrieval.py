from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.rag_service import rag_search
from app.service.vector_store_service import vector_search


DEFAULT_QUERIES = [
    "contract good faith appeal",
    "tenant eviction unpaid rent repairs Ontario",
    "federal regulation administrative appeal",
]


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _time_call(fn: Callable[[], dict], repeat: int) -> dict:
    durations = []
    last_result = {}
    for _ in range(max(1, repeat)):
        started = time.perf_counter()
        last_result = fn()
        durations.append((time.perf_counter() - started) * 1000)
    return {
        "最小毫秒": round(min(durations), 3),
        "平均毫秒": round(statistics.mean(durations), 3),
        "中位毫秒": round(statistics.median(durations), 3),
        "最大毫秒": round(max(durations), 3),
        "返回数量": int(last_result.get("total") or len(last_result.get("items") or [])),
        "状态": last_result.get("status", ""),
        "样例标题": [item.get("title", "") for item in (last_result.get("items") or [])[:3]],
    }


def _explain_vector_query(query: str, limit: int) -> list[str]:
    if is_sqlite():
        return ["SQLite 不支持 pgvector EXPLAIN。"]
    vector_payload = vector_search(query, module="canada", source_filter="canada", limit=limit)
    items = vector_payload.get("items") or []
    if not items:
        return ["向量检索无返回结果，跳过 EXPLAIN。"]
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT embedding_vector::text AS vector_text
                FROM rag_chunk_embeddings
                WHERE chunk_id = :chunk_id
                LIMIT 1
                """
            ),
            {"chunk_id": int(items[0]["chunk_id"])},
        ).mappings().first()
        if not row or not row.get("vector_text"):
            return ["未找到可用于 EXPLAIN 的向量。"]
        rows = conn.execute(
            text(
                """
                EXPLAIN ANALYZE
                SELECT rc.id
                FROM rag_chunks rc
                JOIN rag_chunk_embeddings rce ON rce.chunk_id = rc.id
                WHERE rc.source_code = ANY(:source_codes)
                  AND rce.embedding_vector IS NOT NULL
                ORDER BY rce.embedding_vector <=> CAST(:query_vector AS vector)
                LIMIT :limit
                """
            ),
            {
                "source_codes": [
                    "a2aj_case",
                    "a2aj_law",
                    "a2aj_regulation",
                    "canlii",
                    "ca_federal_act",
                    "ca_federal_regulation",
                    "laws_lois_xml",
                    "on_statute",
                    "on_regulation",
                ],
                "query_vector": row["vector_text"],
                "limit": int(limit),
            },
        ).all()
    return [str(item[0]) for item in rows]


def run_benchmark(queries: list[str], repeat: int, limit: int, include_explain: bool) -> dict:
    payload = {
        "状态": "完成",
        "重复次数": repeat,
        "limit": limit,
        "查询": [],
    }
    for query in queries:
        row = {
            "query": query,
            "关键词检索": _time_call(lambda q=query: rag_search(q, module="canada", source_filter="canada", limit=limit), repeat),
            "向量检索": _time_call(lambda q=query: vector_search(q, module="canada", source_filter="canada", limit=limit), repeat),
            "混合检索": _time_call(lambda q=query: hybrid_search(q, module="canada", source_filter="canada", limit=limit), repeat),
        }
        if include_explain:
            row["EXPLAIN_ANALYZE"] = _explain_vector_query(query, limit)
        payload["查询"].append(row)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="对本地 RAG/pgvector/hybrid 检索做基础延迟基准。")
    parser.add_argument("--query", action="append", default=[], help="自定义查询，可重复传入。")
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--explain", action="store_true", help="附带 PostgreSQL EXPLAIN ANALYZE。")
    parser.add_argument("--output", default="", help="可选 JSON 输出路径。")
    args = parser.parse_args(argv)
    payload = run_benchmark(args.query or DEFAULT_QUERIES, repeat=args.repeat, limit=args.limit, include_explain=args.explain)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        payload["输出文件"] = str(path)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
