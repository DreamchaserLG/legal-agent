from __future__ import annotations

import re
from datetime import datetime

from app.core.config import settings
from app.service.common_service import repair_text
from app.service.legal_query_planner_service import build_legal_query_plan
from app.service.module_service import normalize_module
from app.service.rag_service import rag_search, rebuild_rag_index
from app.service.reranker_service import rerank_legal_evidence
from app.service.vector_store_service import rebuild_chunk_embeddings, vector_search


_GENERIC_QUERY_TERMS = {
    "canada",
    "canadian",
    "ontario",
    "federal",
    "provincial",
    "legal",
    "law",
    "laws",
    "case",
    "cases",
    "issue",
    "issues",
    "dispute",
    "matter",
}


def _terms(text: str) -> set[str]:
    cleaned = repair_text(text).lower()
    english = re.findall(r"[a-z][a-z0-9'./-]{2,}", cleaned)
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", cleaned)
    return {item for item in english + chinese if item}


def _item_key(item: dict) -> str:
    source_url = repair_text(item.get("source_url")).lower().rstrip("/")
    if source_url:
        return f"url:{source_url}"
    source_code = repair_text(item.get("source_code")).lower()
    source_uid = repair_text(item.get("source_uid")).lower()
    if source_code and source_uid:
        return f"source:{source_code}:{source_uid}"
    source_table = repair_text(item.get("source_table"))
    source_id = item.get("source_id")
    if source_table and source_id:
        return f"{source_table}:{source_id}"
    chunk_id = item.get("chunk_id")
    if chunk_id:
        return f"chunk:{chunk_id}"
    return repair_text(item.get("source_url") or item.get("title")).lower()


def _rank_map(items: list[dict]) -> dict[str, int]:
    result = {}
    for index, item in enumerate(items, start=1):
        key = _item_key(item)
        if key and key not in result:
            result[key] = index
    return result


def _retrieval_partitions(source_filter: str, plan: dict) -> list[tuple[str, str, str]]:
    source = repair_text(source_filter or "all").lower()
    law_query = repair_text(plan.get("law_query")) or repair_text(plan.get("original_query"))
    case_query = repair_text(plan.get("case_query")) or repair_text(plan.get("original_query"))
    if source == "law":
        return [("law", "law", law_query)]
    if source == "case":
        return [("case", "case", case_query)]
    if source not in {"", "all", "canada"}:
        return [("mixed", source, repair_text(plan.get("original_query")))]

    partition_map = {
        "law": ("law", "law", law_query),
        "case": ("case", "case", case_query),
    }
    ordered = []
    seen = set()
    for source_kind in plan.get("preferred_sources") or ["law", "case"]:
        key = repair_text(source_kind).lower()
        if key in partition_map and key not in seen:
            ordered.append(partition_map[key])
            seen.add(key)
    for key in ("law", "case"):
        if key not in seen:
            ordered.append(partition_map[key])
    return ordered


def _authority_boost(item: dict, query_terms: set[str]) -> float:
    boost = 0.0
    source_kind = repair_text(item.get("source_kind")).lower()
    if source_kind == "law":
        boost += 0.004
    elif source_kind == "case":
        boost += 0.004
    title_terms = _terms(item.get("title", ""))
    excerpt_terms = _terms(item.get("excerpt", ""))
    if query_terms and title_terms:
        boost += min(len(query_terms & title_terms) * 0.004, 0.02)
    if query_terms and excerpt_terms:
        boost += min(len(query_terms & excerpt_terms) * 0.0015, 0.012)
    return boost


def _meaningful_query_overlap(item: dict, query_terms: set[str]) -> set[str]:
    meaningful_terms = {term for term in query_terms if term not in _GENERIC_QUERY_TERMS and len(term) >= 4}
    if not meaningful_terms:
        return set()
    item_terms = _terms(
        " ".join(
            [
                repair_text(item.get("title")),
                repair_text(item.get("excerpt")),
                " ".join([repair_text(value) for value in (item.get("metadata", {}) or {}).get("keywords", [])]),
            ]
        )
    )
    return meaningful_terms & item_terms


def hybrid_search(
    query: str,
    *,
    keywords: list[str] | None = None,
    module: str = "canada",
    source_filter: str = "all",
    limit: int | None = None,
    lexical_limit: int | None = None,
    vector_limit: int | None = None,
    filters: dict | None = None,
) -> dict:
    clean_query = repair_text(query)
    if not clean_query:
        return {"query": clean_query, "items": [], "total": 0, "status": "empty_query"}

    final_limit = max(1, min(int(limit or getattr(settings, "rag_max_context_items", 8)), 30))
    lexical_limit = max(final_limit, int(lexical_limit or getattr(settings, "rag_lexical_candidate_limit", 40)))
    vector_limit = max(final_limit, int(vector_limit or getattr(settings, "rag_vector_candidate_limit", 40)))
    lexical_weight = float(getattr(settings, "rag_lexical_weight", 0.55))
    vector_weight = float(getattr(settings, "rag_vector_weight", 0.45))
    rrf_k = 60.0

    query_plan = build_legal_query_plan(clean_query, keywords=keywords or [])
    expanded_keywords = list(keywords or []) + list(query_plan.get("meaningful_terms") or [])[:8]
    lexical_items: list[dict] = []
    vector_items: list[dict] = []
    partition_stats = []
    vector_embedding = {}
    vector_status = ""
    vector_pgvector_enabled = False
    for partition_name, partition_source, partition_query in _retrieval_partitions(source_filter, query_plan):
        lexical_result = rag_search(
            clean_query,
            keywords=keywords or [],
            module=module,
            source_filter=partition_source,
            limit=lexical_limit,
            filters=filters,
        )
        vector_result = vector_search(
            partition_query,
            module=module,
            source_filter=partition_source,
            limit=vector_limit,
            filters=filters,
        )
        for item in lexical_result.get("items") or []:
            current = dict(item)
            current["retrieval_partition"] = partition_name
            lexical_items.append(current)
        for item in vector_result.get("items") or []:
            current = dict(item)
            current["retrieval_partition"] = partition_name
            vector_items.append(current)
        vector_embedding = vector_result.get("embedding") or vector_embedding
        vector_status = vector_result.get("status", vector_status)
        vector_pgvector_enabled = bool(vector_result.get("pgvector_enabled", vector_pgvector_enabled))
        partition_stats.append(
            {
                "partition": partition_name,
                "source_filter": partition_source,
                "query": partition_query,
                "lexical_total": len(lexical_result.get("items") or []),
                "vector_total": len(vector_result.get("items") or []),
            }
        )
    vector_provider = repair_text(vector_embedding.get("provider")).lower()
    vector_diagnostic_only = vector_provider == "hash"
    effective_vector_weight = 0.0 if vector_diagnostic_only else vector_weight
    lexical_ranks = _rank_map(lexical_items)
    vector_ranks = _rank_map(vector_items)

    merged: dict[str, dict] = {}
    for channel, items in (("lexical", lexical_items), ("vector", vector_items)):
        for item in items:
            key = _item_key(item)
            if not key:
                continue
            current = merged.setdefault(key, dict(item))
            channels = set(current.get("search_channels") or [])
            channels.add(channel)
            current["search_channels"] = sorted(channels)
            if channel == "lexical":
                current["lexical_score"] = item.get("score", 0)
            else:
                current["vector_score"] = item.get("vector_score", item.get("score", 0))

    query_terms = _terms(
        " ".join(
            [clean_query]
            + list(keywords or [])
            + list(query_plan.get("meaningful_terms") or [])
            + list(query_plan.get("domains") or [])
            + list(query_plan.get("issues") or [])
        )
    )
    scored_items = []
    for key, item in merged.items():
        channels = set(item.get("search_channels") or [])
        overlap = _meaningful_query_overlap(item, query_terms)
        if vector_diagnostic_only and channels == {"vector"} and not overlap:
            continue
        if channels == {"vector"} and not overlap and float(item.get("vector_score") or 0) < 0.58:
            continue
        if not overlap and ("vector" not in channels or vector_diagnostic_only):
            continue
        score = 0.0
        if key in lexical_ranks:
            score += lexical_weight / (rrf_k + lexical_ranks[key])
        if key in vector_ranks:
            score += effective_vector_weight / (rrf_k + vector_ranks[key])
            score += max(float(item.get("vector_score") or 0) - 0.5, 0.0) * 0.1
        score += _authority_boost(item, query_terms)
        item["hybrid_score"] = round(score, 6)
        item["score"] = item["hybrid_score"]
        item.setdefault("lexical_score", 0)
        item.setdefault("vector_score", 0)
        if vector_diagnostic_only:
            item["vector_diagnostic_only"] = True
        scored_items.append(item)

    fused_items = sorted(
        scored_items,
        key=lambda item: (
            float(item.get("hybrid_score") or 0),
            float(item.get("vector_score") or 0),
            float(item.get("lexical_score") or 0),
        ),
        reverse=True,
    )
    items = rerank_legal_evidence(clean_query, fused_items, plan=query_plan, limit=final_limit)

    return {
        "query": clean_query,
        "keywords": keywords or [],
        "structured_query": query_plan,
        "module": normalize_module(module),
        "source_filter": source_filter,
        "filters": filters or {},
        "items": items,
        "total": len(items),
        "status": "ok",
        "strategy": "structured_hybrid_rrf_rerank",
        "channels": {
            "lexical": {
                "status": "ok",
                "total": len(lexical_items),
                "weight": lexical_weight,
            },
            "vector": {
                "status": vector_status,
                "total": len(vector_items),
                "weight": effective_vector_weight,
                "configured_weight": vector_weight,
                "diagnostic_only": vector_diagnostic_only,
                "pgvector_enabled": vector_pgvector_enabled,
                "embedding": vector_embedding,
            },
            "partitions": partition_stats,
        },
    }


def rebuild_hybrid_index(source_filter: str = "all", limit: int | None = None, module: str = "canada") -> dict:
    started_at = datetime.utcnow()
    rag_result = rebuild_rag_index(source_filter=source_filter, limit=limit)
    vector_result = rebuild_chunk_embeddings(source_filter=source_filter, limit=limit, module=module)
    status = "completed" if rag_result.get("status") == "completed" and vector_result.get("status") == "completed" else "failed"
    return {
        "status": status,
        "source_filter": source_filter,
        "module": normalize_module(module),
        "rag": rag_result,
        "vector": vector_result,
        "duration_seconds": round((datetime.utcnow() - started_at).total_seconds(), 3),
    }
