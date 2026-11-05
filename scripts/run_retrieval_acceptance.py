"""向量检索修复后的完整验收测试。

覆盖六层：
  A 环境与配置基线
  B 查询计划矩阵（source_filter × filters × limit）
  C 延迟与并发性能
  D 语义质量（真实中文/英文查询 vs 精确扫描）
  E 端到端链路状态链（vector_search / hybrid_search）
  F 用例判定汇总

只读：不写入业务表，不重建索引。

用法:
  python scripts/run_retrieval_acceptance.py
  python scripts/run_retrieval_acceptance.py --section A,B
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.core.database import engine  # noqa: E402
from app.service.embedding_service import (  # noqa: E402
    create_embedding,
    create_embeddings,
    embedding_metadata,
)
from app.service.hybrid_retrieval_service import hybrid_search  # noqa: E402
from app.service.vector_store_service import (  # noqa: E402
    _build_pgvector_ann_query,
    _configure_hnsw_search,
    _dedupe_vector_items,
    _vector_text,
    vector_search,
)

OUT = "outputs/acceptance"

# 覆盖 8 个加拿大法律领域，中英混合，验证翻译通道
QUERIES = [
    ("ontario-eviction", "安大略房东以自住为由驱逐租客，善意要求与 s.57 救济"),
    ("wrongful-dismissal", "Ontario wrongful dismissal reasonable notice period calculation"),
    ("criminal-sentencing", "加拿大刑事量刑中醉酒、过失与既往记录的加重减轻"),
    ("charter-delay", "Charter s.11(b) unreasonable delay stay of proceedings"),
    ("immigration", "inadmissibility for organized criminality and appeal rights"),
    ("contract", "显失公平交易与不当影响合同撤销"),
    ("summary-judgment", "Ontario summary judgment threshold no genuine issue requiring trial"),
    ("appeal-standard", "上诉标准 palpable and overriding error standard of review"),
]


def pctl(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(int(len(ordered) * p), len(ordered) - 1)], 2)


def explain(sql: str, params: dict) -> dict:
    with engine.connect() as conn:
        conn.execute(text("SET statement_timeout = 0"))
        raw = conn.execute(text("EXPLAIN (FORMAT JSON) " + sql), params).scalar()
        conn.commit()
    plan = json.loads(raw) if isinstance(raw, str) else raw
    blob = json.dumps(plan)
    nodes: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if "Node Type" in node:
                nodes.append(node["Node Type"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(plan)
    return {
        "uses_hnsw": "idx_rag_chunk_embeddings_active_hnsw" in blob,
        "node_types": nodes,
        "has_seq_scan": any(n in ("Seq Scan", "Parallel Seq Scan") for n in nodes),
        "has_hash_join": "Parallel Hash Join" in blob or "Hash Join" in nodes,
        "total_cost": plan[0].get("Plan", {}).get("Total Cost"),
        "plan_rows": plan[0].get("Plan", {}).get("Plan Rows"),
    }


# ---------------------------------------------------------------- A 环境基线
def section_a() -> dict:
    out: dict = {}
    with engine.connect() as conn:
        conn.execute(text("SET statement_timeout = 0"))
        conn.commit()
        out["index"] = dict(
            conn.execute(
                text(
                    "select c.relname name, i.indisvalid valid, i.indisready ready, i.indislive live, "
                    "pg_size_pretty(pg_relation_size(c.oid)) size, pg_get_indexdef(c.oid) ddl "
                    "from pg_index i join pg_class c on c.oid=i.indexrelid "
                    "where c.relname='idx_rag_chunk_embeddings_active_hnsw'"
                )
            ).mappings().first()
        )
        out["gucs"] = {
            r["name"]: {"value": r["setting"], "unit": r["unit"], "pending": r["pending_restart"]}
            for r in conn.execute(
                text(
                    "select name, setting, unit, pending_restart from pg_settings "
                    "where name in ('shared_buffers','effective_cache_size','random_page_cost',"
                    "'work_mem','maintenance_work_mem','hnsw.ef_search','hnsw.iterative_scan',"
                    "'hnsw.max_scan_tuples','statement_timeout')"
                )
            ).mappings()
        }
        out["counts"] = dict(
            conn.execute(
                text(
                    "select (select count(*) from rag_chunks) chunks, "
                    "(select count(*) from rag_chunk_embeddings) embeddings, "
                    "(select count(*) from rag_chunk_embeddings e join rag_chunks c on c.id=e.chunk_id "
                    " where e.content_hash <> c.content_hash) stale"
                )
            ).mappings().first()
        )
    out["app"] = {
        "embedding_provider": settings.embedding_provider,
        "embedding_model": settings.embedding_model,
        "embedding_dimension": settings.embedding_dimension,
        "rag_hnsw_ef_search": settings.rag_hnsw_ef_search,
        "rag_vector_candidate_limit": settings.rag_vector_candidate_limit,
        "statement_timeout_ms": settings.database_statement_timeout_ms,
        "legal_research_max_rounds": settings.legal_research_max_rounds,
    }
    return out


# ------------------------------------------------------- B 查询计划矩阵
def section_b() -> dict:
    vec = create_embedding(QUERIES[0][1])
    meta = embedding_metadata()
    combos = []
    for source in ("all", "law", "case", "canada"):
        for filters in (None, {"jurisdiction": "Ontario"}, {"document_type": "statute"}):
            for limit in (8, 24, 40, 100):
                combos.append((source, filters, limit))

    rows = []
    for source, filters, limit in combos:
        ann_limit = max(1, min(limit, 100)) * 4
        sql, params = _build_pgvector_ann_query(
            vec, meta=meta, module="canada", source_filter=source,
            filters=filters, ann_limit=ann_limit,
        )
        info = explain(sql, params)
        rows.append(
            {
                "source_filter": source,
                "filters": filters,
                "limit": limit,
                "ann_limit": ann_limit,
                **info,
            }
        )
    hit = sum(1 for r in rows if r["uses_hnsw"] and not r["has_seq_scan"] and not r["has_hash_join"])
    return {
        "total_combinations": len(rows),
        "hnsw_hit": hit,
        "hit_rate": round(hit / len(rows), 4),
        "rows": rows,
    }


# --------------------------------------------------------------- C 性能
def _timed_search(query: str, limit: int) -> tuple[dict, float]:
    t0 = time.perf_counter()
    res = vector_search(query, module="canada", source_filter="all", limit=limit)
    return res, round((time.perf_counter() - t0) * 1000, 2)


def section_c() -> dict:
    q = QUERIES[1][1]
    create_embedding(q)  # 预热模型

    out: dict = {"limit_sweep": {}, "cold_first": None, "concurrency": {}}

    # 进程内首查：包含计划缓存与索引页初次加载，作为"冷"参考
    res, ms = _timed_search(QUERIES[0][1], 24)
    out["cold_first"] = {"ms": ms, "items": res.get("total"),
                         "note": "进程内首查，含索引页首次加载"}

    for limit in (8, 24, 40, 100):
        lat, totals = [], []
        for _ in range(8):
            res, ms = _timed_search(q, limit)
            lat.append(ms)
            totals.append(res.get("total", 0))
        out["limit_sweep"][str(limit)] = {
            "p50_ms": pctl(lat, 0.5),
            "p95_ms": pctl(lat, 0.95),
            "p99_ms": pctl(lat, 0.99),
            "min_ms": min(lat),
            "max_ms": max(lat),
            "avg_items": round(statistics.fmean(totals), 1),
            "requested": limit,
        }

    for workers in (1, 4, 8):
        n = 24
        lat = []
        errors = []

        def worker(_):
            try:
                _, ms = _timed_search(QUERIES[2][1], 24)
                return ms, None
            except Exception as exc:  # noqa: BLE001
                return None, str(exc)[:120]

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for ms, err in pool.map(worker, range(n)):
                if err:
                    errors.append(err)
                else:
                    lat.append(ms)
        elapsed = time.perf_counter() - t0
        out["concurrency"][str(workers)] = {
            "requests": n,
            "errors": len(errors),
            "qps": round(n / elapsed, 2),
            "p50_ms": pctl(lat, 0.5),
            "p95_ms": pctl(lat, 0.95),
            "error_sample": errors[:2],
        }

    # C2：绕开 ensure_vector_tables 的 DDL，只测 SQL 层
    vec = create_embedding(q)
    meta = embedding_metadata()
    sql, params = _build_pgvector_ann_query(
        vec, meta=meta, module="canada", source_filter="all", filters=None, ann_limit=96
    )

    def db_only(_):
        t0 = time.perf_counter()
        try:
            with engine.connect() as conn:
                _configure_hnsw_search(conn, ann_limit=96)
                n = len(conn.execute(text(sql), params).all())
                conn.commit()
            return (time.perf_counter() - t0) * 1000, n, None
        except Exception as exc:  # noqa: BLE001
            return None, 0, str(exc)[:120]

    single = [db_only(i)[0] for i in range(10)]
    out["db_only"] = {
        "p50_ms": pctl(single, 0.5),
        "p95_ms": pctl(single, 0.95),
        "note": "仅 SQL 往返，不含 embedding 与 ensure_vector_tables",
    }
    out["concurrency_sql"] = {}
    for workers in (1, 4, 8):
        n = 24
        lat, errs = [], []
        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for ms, _, err in pool.map(db_only, range(n)):
                if err:
                    errs.append(err)
                else:
                    lat.append(ms)
        elapsed = time.perf_counter() - t0
        out["concurrency_sql"][str(workers)] = {
            "requests": n, "errors": len(errs), "qps": round(n / elapsed, 2),
            "p50_ms": pctl(lat, 0.5), "p95_ms": pctl(lat, 0.95),
            "error_sample": errs[:2],
        }
    return out


# ------------------------------------------------------------ D 语义质量
def exact_topk(vec, k: int, provider: str, model: str) -> tuple[list, float]:
    """精确 Top-K。使用 SET LOCAL + 事务回滚，避免污染连接池。"""
    sql = (
        "select chunk_id, (embedding_vector <=> cast(:v as vector)) d "
        "from rag_chunk_embeddings where embedding_provider=:p and embedding_model=:m "
        "and embedding_vector is not null order by embedding_vector <=> cast(:v as vector) limit :k"
    )
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            conn.execute(text("SET LOCAL statement_timeout = 0"))
            conn.execute(text("SET LOCAL enable_indexscan = off"))
            conn.execute(text("SET LOCAL enable_bitmapscan = off"))
            t0 = time.perf_counter()
            rows = conn.execute(
                text(sql), {"v": _vector_text(vec), "p": provider, "m": model, "k": k}
            ).all()
            ms = round((time.perf_counter() - t0) * 1000, 2)
        finally:
            trans.rollback()
    return [(r[0], float(r[1])) for r in rows], ms


def section_d() -> dict:
    vectors = create_embeddings([q for _, q in QUERIES])
    meta = embedding_metadata()
    per_query = []
    k = 10

    for (qid, query), vec in zip(QUERIES, vectors):
        sql, params = _build_pgvector_ann_query(
            vec, meta=meta, module="canada", source_filter="all",
            filters=None, ann_limit=k,
        )
        plan = explain(sql, params)
        t0 = time.perf_counter()
        with engine.connect() as conn:
            trans = conn.begin()
            try:
                _configure_hnsw_search(conn, ann_limit=k)
                ann_rows = conn.execute(text(sql), params).mappings().all()
            finally:
                trans.rollback()
        ann_ms = round((time.perf_counter() - t0) * 1000, 2)
        ann_ids = [r["id"] for r in ann_rows]

        exact, exact_ms = exact_topk(vec, k, meta["provider"], meta["model"])
        exact_ids = [x[0] for x in exact]
        exact_thresh = max((x[1] for x in exact), default=1.0)

        dists = {r["id"]: float(r["score"]) for r in ann_rows}
        overlap = len(set(ann_ids) & set(exact_ids))
        rank = next((i + 1 for i, iid in enumerate(ann_ids) if iid in exact_ids), None)
        within = sum(1 for r in ann_rows if (1 - float(r["score"])) <= exact_thresh * 1.000001 + 1e-9)

        per_query.append(
            {
                "id": qid,
                "query": query,
                "ann_ms": ann_ms,
                "exact_ms": exact_ms,
                "returned": len(ann_ids),
                "plan_uses_hnsw": plan["uses_hnsw"],
                "plan_has_seq_scan": plan["has_seq_scan"],
                "recall_at_10": round(overlap / k, 4),
                "recall_dist_10": round(within / k, 4),
                "mrr": round(1 / rank, 4) if rank else 0.0,
                "top1_in_exact": bool(ann_ids and ann_ids[0] in exact_ids),
                "top1_score": round(dists.get(ann_ids[0], 0), 4) if ann_ids else 0,
                "latency_ratio": round(exact_ms / max(ann_ms, 0.01), 1),
            }
        )

    return {
        "k": k,
        "queries": per_query,
        "recall_at_10_mean": round(statistics.fmean(q["recall_at_10"] for q in per_query), 4),
        "recall_dist_10_mean": round(statistics.fmean(q["recall_dist_10"] for q in per_query), 4),
        "mrr_mean": round(statistics.fmean(q["mrr"] for q in per_query), 4),
        "plan_all_hnsw": all(q["plan_uses_hnsw"] and not q["plan_has_seq_scan"] for q in per_query),
        "ann_p50_ms": pctl([q["ann_ms"] for q in per_query], 0.5),
        "exact_p50_ms": pctl([q["exact_ms"] for q in per_query], 0.5),
        "speedup_p50": round(
            pctl([q["exact_ms"] for q in per_query], 0.5)
            / max(pctl([q["ann_ms"] for q in per_query], 0.5), 0.01),
            1,
        ),
    }


# --------------------------------------------------------- E 状态链追踪
def trace_vector(query: str, limit: int = 24) -> dict:
    steps = []
    t_all = time.perf_counter()

    steps.append({"stage": "01_input", "state": "received", "detail": {"query": query, "limit": limit}})

    vec = create_embedding(query)
    norm = round(sum(v * v for v in vec) ** 0.5, 4)
    steps.append(
        {"stage": "02_embedding", "state": "encoded",
         "detail": {"model": settings.embedding_model, "dim": len(vec), "l2_norm": norm}}
    )

    res, ms = _timed_search(query, limit)
    steps.append(
        {"stage": "03_vector_search", "state": res.get("status"),
         "detail": {"ms": ms, "pgvector_enabled": res.get("pgvector_enabled"),
                    "index_type": res.get("index_type"), "items": res.get("total"),
                    "retrieval_stats": res.get("retrieval_stats") or {}}}
    )

    meta = embedding_metadata()
    ann_limit = max(1, limit) * 4
    sql, params = _build_pgvector_ann_query(
        vec, meta=meta, module="canada", source_filter="all", filters=None, ann_limit=ann_limit
    )
    ann_count_sql = (
        "WITH ann AS MATERIALIZED ("
        " SELECT chunk_id, embedding_vector <=> CAST(:query_vector AS vector) AS distance"
        " FROM rag_chunk_embeddings"
        " WHERE embedding_provider = :embedding_provider"
        "   AND embedding_model = :embedding_model"
        "   AND embedding_vector IS NOT NULL"
        " ORDER BY embedding_vector <=> CAST(:query_vector AS vector)"
        " LIMIT :ann_limit"
        ") SELECT count(*) FROM ann"
    )
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = 0"))
        _configure_hnsw_search(conn, ann_limit=ann_limit)
        cand = conn.execute(text(ann_count_sql), params).scalar()
    plan = explain(sql, params)
    steps.append(
        {"stage": "04_ann_candidates", "state": "bounded",
         "detail": {"ann_limit": ann_limit, "candidates": cand, "uses_hnsw": plan["uses_hnsw"],
                    "has_seq_scan": plan["has_seq_scan"], "plan_cost": plan["total_cost"]}}
    )

    after_filter = res.get("total", 0)
    kept = len(_dedupe_vector_items(res.get("items") or [], limit))
    steps.append(
        {"stage": "05_after_join_freshness", "state": "filtered",
         "detail": {"rows": after_filter, "note": "content_hash 相等 + 模块/source 过滤"}}
    )
    steps.append(
        {"stage": "06_deduped", "state": "deduped",
         "detail": {"kept": kept, "requested": limit, "fill_rate": round(kept / limit, 4)}}
    )

    top = (res.get("items") or [])[:3]
    steps.append(
        {"stage": "07_output", "state": "ok",
         "detail": {"total_ms": round((time.perf_counter() - t_all) * 1000, 2),
                    "top3": [{"title": (i.get("title") or "")[:70],
                              "citation": (i.get("citation") or "")[:40],
                              "score": round(float(i.get("score") or 0), 4)} for i in top]}}
    )
    return {"query": query, "steps": steps}


def trace_hybrid(query: str, limit: int = 8) -> dict:
    t0 = time.perf_counter()
    res = hybrid_search(query, module="canada", source_filter="all", limit=limit)
    ms = round((time.perf_counter() - t0) * 1000, 2)
    src = res.get("structured_query") or {}
    steps = [
        {"stage": "01_input", "state": "received", "detail": {"query": query, "limit": limit}},
        {"stage": "02_translate", "state": "translated",
         "detail": {"source_lang": (res.get("query_translation") or {}).get("source_lang"),
                    "translated": (res.get("retrieval_query") or "")[:160]}},
        {"stage": "03_plan", "state": "planned",
         "detail": {"law_query": (src.get("law_query") or "")[:100],
                    "case_query": (src.get("case_query") or "")[:100],
                    "preferred_sources": src.get("preferred_sources")}},
    ]
    for p in res["channels"]["partitions"]:
        steps.append(
            {"stage": f"04_partition:{p['partition']}", "state": f"lex={p['lexical_status']}/vec={p['vector_status']}",
             "detail": {"lexical_total": p["lexical_total"], "vector_total": p["vector_total"],
                        "vector_error": p["vector_error"],
                        "lexical_ms": p.get("lexical_ms"),
                        "vector_ms": p.get("vector_ms"),
                        "vector_retrieval_stats": p.get("vector_retrieval_stats") or {}}}
        )
    ch = res["channels"]
    steps.append(
        {"stage": "05_channels", "state": "fused",
         "detail": {"lexical": ch["lexical"]["total"], "vector": ch["vector"]["total"],
                    "graph": ch["graph"]["total"], "graph_paths": ch["graph"].get("paths"),
                    "vector_pgvector_enabled": ch["vector"]["pgvector_enabled"],
                    "vector_status": ch["vector"]["status"]}}
    )
    steps.append(
        {"stage": "06_rerank_window", "state": (ch.get("context_window") or {}).get("status", "n/a"),
         "detail": {"window": ch.get("context_window"), "timing_ms": ch.get("timing_ms") or {}}}
    )
    steps.append(
        {"stage": "07_output", "state": res.get("status"),
         "detail": {"total": res.get("total"), "strategy": res.get("strategy"), "ms": ms,
                    "top3": [{"title": (i.get("title") or "")[:70],
                              "score": round(float(i.get("score") or 0), 4),
                              "vector_score": round(float(i.get("vector_score") or 0), 4),
                              "lexical_score": round(float(i.get("lexical_score") or 0), 4)}
                             for i in (res.get("items") or [])[:3]]}}
    )
    return {"query": query, "total_ms": ms, "steps": steps}


def section_e() -> dict:
    traces = {"vector": [], "hybrid": []}
    for qid, query in (QUERIES[0], QUERIES[2], QUERIES[4]):
        traces["vector"].append({"id": qid, **trace_vector(query)})
    for qid, query in (QUERIES[0], QUERIES[3], QUERIES[6]):
        traces["hybrid"].append({"id": qid, **trace_hybrid(query)})

    # 边界输入
    edge = []
    for q in ("", "   ", "a" * 3000, "《民法典》第 1165 条", "🚗🚗🚗"):
        t0 = time.perf_counter()
        try:
            r = vector_search(q, module="canada", source_filter="all", limit=8)
            edge.append({"input": q[:40], "status": r.get("status"), "total": r.get("total"),
                         "ms": round((time.perf_counter() - t0) * 1000, 2)})
        except Exception as exc:  # noqa: BLE001
            edge.append({"input": q[:40], "status": "EXCEPTION", "error": str(exc)[:160]})
    traces["edge_cases"] = edge
    return traces


# ------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--section", default="A,B,C,D,E")
    args = ap.parse_args()
    wanted = {s.strip().upper() for s in args.section.split(",") if s.strip()}
    os.makedirs(OUT, exist_ok=True)

    report: dict = {"generated_at": datetime.now().isoformat(timespec="seconds")}
    path = os.path.join(OUT, "acceptance.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            report.update(json.load(fh))
    report["updated_at"] = datetime.now().isoformat(timespec="seconds")

    if "A" in wanted:
        print("[A] 环境与配置基线 ...", flush=True)
        report["A_env"] = section_a()
        print("    index valid=%s ready=%s size=%s" % (
            report["A_env"]["index"]["valid"], report["A_env"]["index"]["ready"],
            report["A_env"]["index"]["size"]), flush=True)

    if "B" in wanted:
        print("[B] 查询计划矩阵 ...", flush=True)
        report["B_plan"] = section_b()
        print("    hit_rate=%s (%d/%d)" % (
            report["B_plan"]["hit_rate"], report["B_plan"]["hnsw_hit"],
            report["B_plan"]["total_combinations"]), flush=True)

    if "C" in wanted:
        print("[C] 延迟与并发 ...", flush=True)
        report["C_perf"] = section_c()
        print("    limit24 p50=%sms p95=%sms" % (
            report["C_perf"]["limit_sweep"]["24"]["p50_ms"],
            report["C_perf"]["limit_sweep"]["24"]["p95_ms"]), flush=True)

    if "D" in wanted:
        print("[D] 语义质量（含精确基线，较慢）...", flush=True)
        report["D_semantic"] = section_d()
        print("    recall@10=%s mrr=%s" % (
            report["D_semantic"]["recall_at_10_mean"],
            report["D_semantic"]["mrr_mean"]), flush=True)

    if "E" in wanted:
        print("[E] 端到端状态链 ...", flush=True)
        traces = section_e()
        os.makedirs(os.path.join(OUT, "traces"), exist_ok=True)
        for kind in ("vector", "hybrid"):
            for t in traces[kind]:
                with open(os.path.join(OUT, "traces", f"{kind}_{t['id']}.json"), "w",
                          encoding="utf-8") as fh:
                    json.dump(t, fh, ensure_ascii=False, indent=2, default=str)
        report["E_traces"] = {
            k: ([{kk: vv for kk, vv in t.items()} for t in traces[k]] if k != "edge_cases"
                else traces[k])
            for k in ("vector", "hybrid", "edge_cases")
        }
        print("    traces=%d files" % (len(traces["vector"]) + len(traces["hybrid"])), flush=True)

    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, ensure_ascii=False, indent=2, default=str)
    print("saved:", path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
