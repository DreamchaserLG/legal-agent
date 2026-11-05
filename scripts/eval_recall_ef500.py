"""验证 ef_search=500 修复后的召回与过滤分区填充率。

输出 outputs/acceptance/recall_after_ef500.json
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.service.embedding_service import create_embeddings, embedding_metadata  # noqa: E402
from app.service.vector_store_service import (  # noqa: E402
    _build_pgvector_ann_query,
    _vector_text,
    vector_search,
)

QUERIES = [
    ("ontario-eviction", "安大略房东以自住为由驱逐租客，善意要求与 s.57 救济"),
    ("criminal-sentencing", "加拿大刑事量刑中醉酒、过失与既往记录的加重减轻"),
    ("contract", "显失公平交易与不当影响合同撤销"),
    ("summary-judgment", "Ontario summary judgment threshold no genuine issue requiring trial"),
]
K = 10


def exact_top(vec, provider, model):
    sql = (
        "select chunk_id from rag_chunk_embeddings where embedding_provider=:p "
        "and embedding_model=:m and embedding_vector is not null "
        "order by embedding_vector <=> cast(:v as vector) limit :k"
    )
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET LOCAL statement_timeout = 0"))
            conn.execute(text("SET LOCAL enable_indexscan = off"))
            conn.execute(text("SET LOCAL enable_bitmapscan = off"))
            t0 = time.perf_counter()
            ids = conn.execute(
                text(sql), {"v": _vector_text(vec), "p": provider, "m": model, "k": K}
            ).scalars().all()
            ms = round((time.perf_counter() - t0) * 1000, 1)
        finally:
            trans.rollback()
    return list(ids), ms


def ann_ids(vec, meta, ef: int, ann_limit: int = 96):
    sql, params = _build_pgvector_ann_query(
        vec, meta=meta, module="canada", source_filter="all", filters=None, ann_limit=ann_limit
    )
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET LOCAL statement_timeout = 0"))
            conn.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
            conn.execute(text("SET LOCAL hnsw.max_scan_tuples = 20000"))
            conn.execute(text(f"SET LOCAL hnsw.ef_search = {ef}"))
            t0 = time.perf_counter()
            rows = conn.execute(text(sql), params).mappings().all()
            ms = round((time.perf_counter() - t0) * 1000, 2)
        finally:
            trans.rollback()
    return [r["id"] for r in rows], ms


def main() -> int:
    meta = embedding_metadata()
    vectors = create_embeddings([q for _, q in QUERIES])
    rows = []

    for (qid, query), vec in zip(QUERIES, vectors):
        exact, exact_ms = exact_top(vec, meta["provider"], meta["model"])
        rec = {"id": qid, "exact_ms": exact_ms, "ef": {}}
        for ef in (80, 200, 500):
            ids, ms = ann_ids(vec, meta, ef)
            rec["ef"][str(ef)] = {
                "recall": round(len(set(ids[:K]) & set(exact)) / K, 2),
                "ms": ms,
                "returned": len(ids),
            }
        rows.append(rec)
        print(qid, "exact %.0fs" % (exact_ms / 1000), {k: (v["recall"], v["ms"]) for k, v in rec["ef"].items()}, flush=True)

    summary = {}
    for ef in ("80", "200", "500"):
        summary[ef] = {
            "recall_mean": round(statistics.fmean(r["ef"][ef]["recall"] for r in rows), 3),
            "recall_min": min(r["ef"][ef]["recall"] for r in rows),
            "p50_ms": round(statistics.median([r["ef"][ef]["ms"] for r in rows]), 1),
        }

    # 过滤分区填充率（生产默认 ef）
    fill = []
    for qid, query in QUERIES[:3]:
        for sf in ("all", "law", "case"):
            t0 = time.perf_counter()
            r = vector_search(query, module="canada", source_filter=sf, limit=24)
            fill.append(
                {
                    "id": qid,
                    "source_filter": sf,
                    "items": r["total"],
                    "requested": 24,
                    "fill_rate": round(r["total"] / 24, 3),
                    "ms": round((time.perf_counter() - t0) * 1000, 1),
                    "stats": r.get("retrieval_stats"),
                }
            )
            print("fill", qid, sf, r["total"], "/24", fill[-1]["ms"], "ms", flush=True)

    out = {"k": K, "queries": rows, "summary": summary, "filtered_fill": fill}
    os.makedirs("outputs/acceptance", exist_ok=True)
    with open("outputs/acceptance/recall_after_ef500.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
