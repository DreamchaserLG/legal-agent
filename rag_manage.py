from __future__ import annotations

import argparse
import json
import sys

from app.service.hybrid_retrieval_service import hybrid_search, rebuild_hybrid_index
from app.service.legal_ingestion_pipeline_service import hydrate_canlii_and_rebuild
from app.service.legal_query_planner_service import build_legal_query_plan
from app.service.rag_service import export_rag_chunks, get_rag_status, rag_search, rebuild_rag_index
from app.service.retrieval_evaluation_service import run_retrieval_evaluation
from app.service.vector_store_service import get_vector_status, rebuild_chunk_embeddings, vector_search

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _print(payload: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    status = payload.get("status")
    if status:
        print(f"status: {status}")
    for key, value in payload.items():
        if key == "status":
            continue
        print(f"{key}: {value}")


def _filters_from_args(args) -> dict:
    filters = {}
    for key in ("jurisdiction", "document_type", "court_level", "language", "date_from", "date_to"):
        value = getattr(args, key, None)
        if value:
            filters[key] = value
    return filters


def cmd_status(args) -> int:
    _print(get_rag_status(), args.json)
    return 0


def cmd_rebuild(args) -> int:
    result = rebuild_rag_index(source_filter=args.source, limit=args.limit)
    _print(result, args.json)
    return 0 if result.get("status") == "completed" else 1


def cmd_search(args) -> int:
    result = rag_search(
        args.query,
        keywords=args.keyword or [],
        module=args.module,
        source_filter=args.source,
        limit=args.limit,
        filters=_filters_from_args(args),
    )
    if args.json:
        _print(result, True)
        return 0
    print(f"status: {result.get('status')}")
    print(f"total: {result.get('total')}")
    for index, item in enumerate(result.get("items") or [], start=1):
        print(f"\n[{index}] {item.get('title')} ({item.get('source_kind')}, score={item.get('score')})")
        if item.get("source_url"):
            print(item.get("source_url"))
        print(item.get("excerpt", "")[:500])
    return 0


def cmd_plan_query(args) -> int:
    result = {
        "status": "ok",
        "query": args.query,
        "structured_query": build_legal_query_plan(args.query, keywords=args.keyword or []),
    }
    _print(result, args.json)
    return 0


def cmd_vector_status(args) -> int:
    _print(get_vector_status(), args.json)
    return 0


def cmd_rebuild_vectors(args) -> int:
    result = rebuild_chunk_embeddings(source_filter=args.source, limit=args.limit, module=args.module)
    _print(result, args.json)
    return 0 if result.get("status") == "completed" else 1


def cmd_vector_search(args) -> int:
    result = vector_search(
        args.query,
        module=args.module,
        source_filter=args.source,
        limit=args.limit,
        filters=_filters_from_args(args),
    )
    if args.json:
        _print(result, True)
        return 0
    print(f"status: {result.get('status')}")
    print(f"total: {result.get('total')}")
    for index, item in enumerate(result.get("items") or [], start=1):
        print(f"\n[{index}] {item.get('title')} ({item.get('source_kind')}, vector_score={item.get('vector_score')})")
        if item.get("source_url"):
            print(item.get("source_url"))
        print(item.get("excerpt", "")[:500])
    return 0


def cmd_hybrid_search(args) -> int:
    result = hybrid_search(
        args.query,
        keywords=args.keyword or [],
        module=args.module,
        source_filter=args.source,
        limit=args.limit,
        filters=_filters_from_args(args),
    )
    if args.json:
        _print(result, True)
        return 0
    print(f"status: {result.get('status')}")
    print(f"strategy: {result.get('strategy')}")
    print(f"total: {result.get('total')}")
    for index, item in enumerate(result.get("items") or [], start=1):
        channels = ",".join(item.get("search_channels") or [])
        score = item.get("rerank_score", item.get("hybrid_score", item.get("score")))
        print(f"\n[{index}] {item.get('title')} ({item.get('source_kind')}, score={score}, channels={channels})")
        if item.get("source_url"):
            print(item.get("source_url"))
        print(item.get("excerpt", "")[:500])
    return 0


def cmd_rebuild_hybrid(args) -> int:
    result = rebuild_hybrid_index(source_filter=args.source, limit=args.limit, module=args.module)
    _print(result, args.json)
    return 0 if result.get("status") == "completed" else 1


def cmd_hydrate_canlii(args) -> int:
    result = hydrate_canlii_and_rebuild(
        args.query,
        keywords=args.keyword or [],
        target_count=args.target_count,
        module=args.module,
        skip_ingest=args.skip_ingest,
        skip_rebuild=args.skip_rebuild,
    )
    _print(result, args.json)
    return 0 if result.get("status") in {"completed", "partial_failure"} else 1


def cmd_eval_retrieval(args) -> int:
    result = run_retrieval_evaluation(
        dataset_path=args.dataset,
        module=args.module,
        limit=args.limit,
        output_path=args.output,
    )
    if args.json:
        _print(result, True)
        return 0
    summary = result.get("summary") or {}
    print(f"status: {result.get('status')}")
    print(f"dataset: {result.get('dataset')}")
    print(f"cases: {summary.get('cases')}")
    print(f"hit@1: {summary.get('hit@1')}")
    print(f"hit@3: {summary.get('hit@3')}")
    print(f"hit@5: {summary.get('hit@5')}")
    print(f"hit@10: {summary.get('hit@10')}")
    print(f"mrr: {summary.get('mrr')}")
    if result.get("output_path"):
        print(f"output_path: {result.get('output_path')}")
    misses = [row for row in result.get("cases") or [] if not row.get("hit@10")]
    print(f"misses@10: {len(misses)}")
    for row in misses[:10]:
        print(f"- {row.get('id')}: {row.get('query')}")
        for item in (row.get("top_results") or [])[:3]:
            print(f"  [{item.get('rank')}] {item.get('title')} ({item.get('source_kind')})")
    return 0


def cmd_export(args) -> int:
    result = export_rag_chunks(source_filter=args.source, output_path=args.output, limit=args.limit)
    _print(result, args.json)
    return 0


def _add_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--jurisdiction", default="", help="Structured filter, for example Canada or Ontario.")
    parser.add_argument("--document-type", dest="document_type", default="", help="Structured filter, for example case, statute, or regulation.")
    parser.add_argument("--court-level", dest="court_level", default="", help="Structured court/tribunal filter.")
    parser.add_argument("--language", default="", help="Structured language filter, for example en or fr.")
    parser.add_argument("--date-from", dest="date_from", default="", help="Filter published_at on or after YYYY-MM-DD.")
    parser.add_argument("--date-to", dest="date_to", default="", help="Filter published_at on or before YYYY-MM-DD.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build, inspect, search, and export the local RAG index.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="Show RAG index status.")
    status.set_defaults(func=cmd_status)

    rebuild = sub.add_parser("rebuild", help="Rebuild PostgreSQL RAG chunks from local data.")
    rebuild.add_argument("--source", default="all", help="all, canada, ofac, canlii, case, law, or a source_code.")
    rebuild.add_argument("--limit", type=int, default=None, help="Maximum source documents to index.")
    rebuild.set_defaults(func=cmd_rebuild)

    search = sub.add_parser("search", help="Search the local RAG index.")
    search.add_argument("query")
    search.add_argument("--keyword", action="append", default=[], help="Optional retrieval keyword. Can repeat.")
    search.add_argument("--module", default="canada", choices=["canada", "us_sanctions"])
    search.add_argument("--source", default="all", help="all, canada, ofac, canlii, case, law, or a source_code.")
    search.add_argument("--limit", type=int, default=8)
    _add_filter_args(search)
    search.set_defaults(func=cmd_search)

    plan_query = sub.add_parser("plan-query", help="Show structured legal retrieval query plan.")
    plan_query.add_argument("query")
    plan_query.add_argument("--keyword", action="append", default=[], help="Optional retrieval keyword. Can repeat.")
    plan_query.set_defaults(func=cmd_plan_query)

    vector_status = sub.add_parser("vector-status", help="Show embedding/vector index status.")
    vector_status.set_defaults(func=cmd_vector_status)

    rebuild_vectors = sub.add_parser("rebuild-vectors", help="Build embeddings for existing RAG chunks.")
    rebuild_vectors.add_argument("--source", default="all", help="all, canada, ofac, canlii, case, law, or a source_code.")
    rebuild_vectors.add_argument("--module", default="canada", choices=["canada", "us_sanctions"])
    rebuild_vectors.add_argument("--limit", type=int, default=None, help="Maximum chunks to embed.")
    rebuild_vectors.set_defaults(func=cmd_rebuild_vectors)

    vector_search_parser = sub.add_parser("vector-search", help="Search the vector index.")
    vector_search_parser.add_argument("query")
    vector_search_parser.add_argument("--module", default="canada", choices=["canada", "us_sanctions"])
    vector_search_parser.add_argument("--source", default="all", help="all, canada, ofac, canlii, case, law, or a source_code.")
    vector_search_parser.add_argument("--limit", type=int, default=8)
    _add_filter_args(vector_search_parser)
    vector_search_parser.set_defaults(func=cmd_vector_search)

    hybrid_search_parser = sub.add_parser("hybrid-search", help="Search with lexical + vector retrieval.")
    hybrid_search_parser.add_argument("query")
    hybrid_search_parser.add_argument("--keyword", action="append", default=[], help="Optional retrieval keyword. Can repeat.")
    hybrid_search_parser.add_argument("--module", default="canada", choices=["canada", "us_sanctions"])
    hybrid_search_parser.add_argument("--source", default="all", help="all, canada, ofac, canlii, case, law, or a source_code.")
    hybrid_search_parser.add_argument("--limit", type=int, default=8)
    _add_filter_args(hybrid_search_parser)
    hybrid_search_parser.set_defaults(func=cmd_hybrid_search)

    rebuild_hybrid = sub.add_parser("rebuild-hybrid", help="Rebuild RAG chunks and embeddings together.")
    rebuild_hybrid.add_argument("--source", default="all", help="all, canada, ofac, canlii, case, law, or a source_code.")
    rebuild_hybrid.add_argument("--module", default="canada", choices=["canada", "us_sanctions"])
    rebuild_hybrid.add_argument("--limit", type=int, default=None, help="Maximum source documents/chunks to process.")
    rebuild_hybrid.set_defaults(func=cmd_rebuild_hybrid)

    hydrate_canlii = sub.add_parser(
        "hydrate-canlii",
        help="Hydrate CanLII by query keywords, then rebuild canlii RAG chunks and vectors.",
    )
    hydrate_canlii.add_argument("query")
    hydrate_canlii.add_argument("--keyword", action="append", default=[], help="Optional hydration keyword. Can repeat.")
    hydrate_canlii.add_argument("--target-count", type=int, default=None, help="Desired CanLII item count.")
    hydrate_canlii.add_argument("--module", default="canada", choices=["canada", "us_sanctions"])
    hydrate_canlii.add_argument("--skip-ingest", action="store_true", help="Only rebuild local canlii indexes.")
    hydrate_canlii.add_argument("--skip-rebuild", action="store_true", help="Only run CanLII hydration.")
    hydrate_canlii.set_defaults(func=cmd_hydrate_canlii)

    eval_retrieval = sub.add_parser("eval-retrieval", help="Evaluate hybrid retrieval against a labeled dataset.")
    eval_retrieval.add_argument("--dataset", default="", help="Evaluation JSON path.")
    eval_retrieval.add_argument("--module", default="canada", choices=["canada", "us_sanctions"])
    eval_retrieval.add_argument("--limit", type=int, default=10)
    eval_retrieval.add_argument("--output", default="", help="Optional JSON report path.")
    eval_retrieval.set_defaults(func=cmd_eval_retrieval)

    export = sub.add_parser("export", help="Export RAG chunks as JSONL for a server/vector database.")
    export.add_argument("--source", default="all", help="all, canada, ofac, canlii, case, law, or a source_code.")
    export.add_argument("--output", default="", help="Output JSONL path. Defaults to data_archive/exports.")
    export.add_argument("--limit", type=int, default=None)
    export.set_defaults(func=cmd_export)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
