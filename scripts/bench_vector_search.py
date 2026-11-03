"""向量检索延迟基准测试。

用真实 query 测量不同 source_kind 下的检索延迟分布，用于判断当前索引是否够用。

用法:
  python scripts/bench_vector_search.py                    # 默认 20 次
  python scripts/bench_vector_search.py --runs 50 --kind case
  python scripts/bench_vector_search.py --show-hits        # 打印召回结果
"""
from __future__ import annotations

import argparse
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.core.database import engine  # noqa: E402
from app.service.embedding_service import create_embeddings, embedding_metadata  # noqa: E402
from app.service.vector_index_management_service import get_hnsw_index_state  # noqa: E402

# 覆盖不同法律领域，避免单一主题偏差
QUERIES = [
    "dismissal without cause and reasonable notice period",
    "Charter breach and stay of proceedings for delay",
    "grounds of inadmissibility for organized criminality",
    "employer obligations when hiring temporary foreign workers",
    "unconscionable transaction and undue influence in contract",
    "adverse inference and failure to disclose evidence",
]


def pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    idx = min(int(len(values) * p), len(values) - 1)
    return values[idx]


def bench(kind: str | None, runs: int, show_hits: bool) -> None:
    meta = embedding_metadata()
    kind_clause = "AND rc.source_kind = :kind" if kind else ""
    sql = (
        "select rc.title, rc.citation, rc.metadata_json->>'heading_path' hp, "
        "rc.metadata_json->>'section_number' sn, "
        "1-(rce.embedding_vector <=> CAST(:v AS vector)) sim "
        "from rag_chunk_embeddings rce join rag_chunks rc on rc.id = rce.chunk_id "
        "where rce.embedding_vector is not null "
        "and rce.embedding_provider = :provider and rce.embedding_model = :model "
        "and rce.content_hash = rc.content_hash "
        f"{kind_clause} "
        "order by rce.embedding_vector <=> CAST(:v AS vector) limit 5"
    )

    # 先编码所有 query，隔离掉 embed 时间，只测检索
    print(f"预热：编码 {len(QUERIES)} 条 query ...")
    vectors = create_embeddings(QUERIES)

    latencies: list[float] = []
    print(f"测量 {runs} 轮 × {len(QUERIES)} query (kind={kind or 'all'})")
    for i in range(runs):
        for q, vec in zip(QUERIES, vectors):
            params = {
                "v": "[" + ",".join(str(round(x, 6)) for x in vec) + "]",
                "provider": meta["provider"], "model": meta["model"],
            }
            if kind:
                params["kind"] = kind
            t0 = time.perf_counter()
            with engine.connect() as conn:
                rows = conn.execute(text(sql), params).mappings().all()
            latencies.append((time.perf_counter() - t0) * 1000)
        if show_hits and i == 0:
            print("\n首轮召回样例:")
            for q, vec in zip(QUERIES, vectors):
                params = {
                    "v": "[" + ",".join(str(round(x, 6)) for x in vec) + "]",
                    "provider": meta["provider"], "model": meta["model"],
                }
                if kind:
                    params["kind"] = kind
                with engine.connect() as conn:
                    rows = conn.execute(text(sql), params).mappings().all()
                print(f"  [{q[:50]}]")
                for r in rows[:3]:
                    tag = r["hp"] or (f"§{r['sn']}" if r["sn"] else "")
                    print(f"     {r['sim']:.4f}  {(r['citation'] or '')[:20]:20s} {tag[:40]}")
            print()

    n = len(latencies)
    print(f"\n样本数 {n}")
    print(f"  P50    {pct(latencies, 0.50):8.1f} ms")
    print(f"  P90    {pct(latencies, 0.90):8.1f} ms")
    print(f"  P95    {pct(latencies, 0.95):8.1f} ms")
    print(f"  P99    {pct(latencies, 0.99):8.1f} ms")
    print(f"  平均   {statistics.mean(latencies):8.1f} ms")
    print(f"  最大   {max(latencies):8.1f} ms")
    print(f"  最小   {min(latencies):8.1f} ms")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kind", default="", help="case / law / 空=全部")
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--show-hits", action="store_true")
    args = ap.parse_args()

    state = get_hnsw_index_state()
    print("当前 HNSW 索引:", state)
    if not state.get("valid") or not state.get("ready"):
        raise RuntimeError("HNSW index is not valid and ready; benchmark refused to run a full-table scan")

    bench(args.kind or None, args.runs, args.show_hits)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
