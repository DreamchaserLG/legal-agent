"""召回率与过采样倍数 (ann_limit) 的关系。

对每条真实查询先做一次精确 Top-100（全表扫描），再以不同 ann_limit 取 Top-10，
比较与精确 Top-10 的重合度。用于判断生产配置 ann_limit = 4 x limit 的召回余量。

用法: python scripts/eval_recall_vs_annlimit.py --queries 4
"""
from __future__ import annotations

import argparse
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
    _configure_hnsw_search,
    _hnsw_ef_search,
    _vector_text,
)

QUERIES = [
    ("ontario-eviction", "安大略房东以自住为由驱逐租客，善意要求与 s.57 救济"),
    ("criminal-sentencing", "加拿大刑事量刑中醉酒、过失与既往记录的加重减轻"),
    ("contract", "显失公平交易与不当影响合同撤销"),
    ("summary-judgment", "Ontario summary judgment threshold no genuine issue requiring trial"),
]
ANN_LIMITS = [10, 40, 96, 200]
K = 10
EXACT_N = 100


def exact_topn(vec, n: int, provider: str, model: str):
    sql = (
        "select chunk_id, (embedding_vector <=> cast(:v as vector)) d "
        "from rag_chunk_embeddings where embedding_provider=:p and embedding_model=:m "
        "and embedding_vector is not null order by embedding_vector <=> cast(:v as vector) limit :n"
    )
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET LOCAL statement_timeout = 0"))
            conn.execute(text("SET LOCAL enable_indexscan = off"))
            conn.execute(text("SET LOCAL enable_bitmapscan = off"))
            t0 = time.perf_counter()
            rows = conn.execute(
                text(sql), {"v": _vector_text(vec), "p": provider, "m": model, "n": n}
            ).all()
            ms = round((time.perf_counter() - t0) * 1000, 1)
        finally:
            trans.rollback()
    return [(r[0], float(r[1])) for r in rows], ms


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--queries", type=int, default=4)
    ap.add_argument("--out", default="outputs/acceptance/recall_vs_annlimit.json")
    args = ap.parse_args()

    meta = embedding_metadata()
    queries = QUERIES[: args.queries]
    vectors = create_embeddings([q for _, q in queries])

    rows = []
    for (qid, query), vec in zip(queries, vectors):
        exact, exact_ms = exact_topn(vec, EXACT_N, meta["provider"], meta["model"])
        exact_ids = {cid for cid, _ in exact[:K]}
        exact_thresh = exact[K - 1][1]

        rec = {"id": qid, "query": query, "exact_ms": exact_ms, "ann": {}}
        for ann_limit in ANN_LIMITS:
            sql, params = _build_pgvector_ann_query(
                vec, meta=meta, module="canada", source_filter="all",
                filters=None, ann_limit=ann_limit,
            )
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    conn.execute(text("SET LOCAL statement_timeout = 0"))
                    effective_ef = _configure_hnsw_search(conn, ann_limit=ann_limit)
                    t0 = time.perf_counter()
                    got = conn.execute(text(sql), params).mappings().all()
                    ms = round((time.perf_counter() - t0) * 1000, 2)
                finally:
                    trans.rollback()
            ids = [r["id"] for r in got]
            top = ids[:K]
            overlap = len(set(top) & exact_ids)
            within = sum(
                1 for r in got[:K] if (1 - float(r["score"])) <= exact_thresh * 1.01 + 1e-9
            )
            mean_d = statistics.fmean(1 - float(r["score"]) for r in got[:K]) if got else 1.0
            rec["ann"][str(ann_limit)] = {
                "ms": ms,
                "returned": len(ids),
                "recall_at_10": round(overlap / K, 4),
                "recall_at_10_tol1pct": round(within / K, 4),
                "mean_distance": round(mean_d, 5),
                "effective_ef_search": effective_ef,
            }
        rec["exact_mean_distance"] = round(statistics.fmean(d for _, d in exact[:K]), 5)
        rows.append(rec)
        print(
            qid,
            "exact %.0fms" % exact_ms,
            {k: (v["recall_at_10"], v["ms"]) for k, v in rec["ann"].items()},
            flush=True,
        )

    summary = {}
    for ann_limit in ANN_LIMITS:
        vals = [r["ann"][str(ann_limit)]["recall_at_10"] for r in rows]
        tols = [r["ann"][str(ann_limit)]["recall_at_10_tol1pct"] for r in rows]
        lats = [r["ann"][str(ann_limit)]["ms"] for r in rows]
        summary[str(ann_limit)] = {
            "recall_at_10_mean": round(statistics.fmean(vals), 4),
            "recall_at_10_min": round(min(vals), 4),
            "recall_at_10_tol1pct_mean": round(statistics.fmean(tols), 4),
            "p50_ms": round(statistics.median(lats), 2),
        }

    out = {
        "k": K,
        "exact_n": EXACT_N,
        "configured_ef_search": _hnsw_ef_search(),
        "queries": rows,
        "summary": summary,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print("saved:", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
