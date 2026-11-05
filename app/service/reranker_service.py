from __future__ import annotations

import re
from collections import Counter

from app.service.common_service import repair_text


_DOMAIN_TOKENS = {
    "residential tenancy": {"tenant", "tenancy", "landlord", "lease", "residential", "rental", "ltb", "onltb"},
    "eviction": {"eviction", "evict", "termination", "possession", "notice", "n4", "n5"},
    "repair": {"repair", "repairs", "maintenance", "habitability", "uninhabitable", "vital", "services"},
}

_BAD_TENANCY_DRIFT = {
    "motor",
    "vehicle",
    "storage",
    "lien",
    "electrical",
    "hydro",
    "environmental",
    "election",
}

_COMMERCIAL_DRIFT = {
    "residential",
    "onltb",
    "ltb",
    "eviction",
    "arrears",
}


def _terms(text: str) -> set[str]:
    cleaned = repair_text(text).lower()
    english = re.findall(r"[a-z][a-z0-9'./-]{2,}", cleaned)
    chinese = re.findall(r"[\u4e00-\u9fff]{2,}", cleaned)
    return {item for item in english + chinese if item}


def _item_text(item: dict) -> str:
    metadata = item.get("metadata") or {}
    keywords = metadata.get("keywords") if isinstance(metadata, dict) else []
    if not isinstance(keywords, list):
        keywords = []
    return " ".join(
        [
            repair_text(item.get("title")),
            repair_text(item.get("excerpt")),
            " ".join([repair_text(value) for value in keywords]),
        ]
    )


def _domain_signals(plan: dict) -> set[str]:
    signals: set[str] = set()
    for value in (plan.get("domains") or []) + (plan.get("issues") or []):
        lowered = repair_text(value).lower()
        for domain, tokens in _DOMAIN_TOKENS.items():
            if domain in lowered or lowered in tokens:
                signals.update(tokens)
    return signals


def _jurisdiction_match(item: dict, jurisdictions: list[str]) -> bool:
    if not jurisdictions:
        return False
    haystack = " ".join(
        [
            repair_text(item.get("jurisdiction")),
            repair_text(item.get("court_level")),
            repair_text(item.get("citation")),
            repair_text(item.get("source_code")),
            repair_text(item.get("title")),
        ]
    ).lower()
    for jurisdiction in jurisdictions:
        lowered = repair_text(jurisdiction).lower()
        if lowered and lowered in haystack:
            return True
        if lowered == "ontario" and (
            repair_text(item.get("source_code")).lower() in {"on_statute", "on_regulation"}
            or re.search(r"(^|[^a-z])(onca|onsc|oncj|onltb)([^a-z]|$)", haystack)
        ):
            return True
    return False


def _ensure_preferred_source_coverage(
    ranked: list[dict], preferred_sources: list[str], limit: int
) -> list[dict]:
    """Keep at least one result from every requested legal source partition."""
    selected = list(ranked[:limit])
    if not selected:
        return selected
    present = {repair_text(item.get("source_kind")).lower() for item in selected}
    counts = Counter(repair_text(item.get("source_kind")).lower() for item in selected)
    selected_ids = {id(item) for item in selected}
    for source_kind in preferred_sources:
        if not source_kind or source_kind in present:
            continue
        replacement = next(
            (
                item
                for item in ranked
                if repair_text(item.get("source_kind")).lower() == source_kind
                and id(item) not in selected_ids
            ),
            None,
        )
        if replacement is None:
            continue
        replacement_index = next(
            (
                index
                for index in range(len(selected) - 1, -1, -1)
                if counts[repair_text(selected[index].get("source_kind")).lower()] > 1
                or repair_text(selected[index].get("source_kind")).lower() not in preferred_sources
            ),
            None,
        )
        if replacement_index is None:
            continue
        removed = selected[replacement_index]
        removed_kind = repair_text(removed.get("source_kind")).lower()
        selected_ids.discard(id(removed))
        selected[replacement_index] = replacement
        selected_ids.add(id(replacement))
        counts[removed_kind] -= 1
        counts[source_kind] += 1
        present.add(source_kind)
    return selected


def rerank_legal_evidence(query: str, items: list[dict], plan: dict | None = None, limit: int | None = None) -> list[dict]:
    plan = plan or {}
    query_terms = _terms(
        " ".join(
            [
                repair_text(query),
                " ".join(plan.get("meaningful_terms") or []),
                " ".join(plan.get("domains") or []),
                " ".join(plan.get("issues") or []),
            ]
        )
    )
    domain_signals = _domain_signals(plan)
    preferred_sources = [repair_text(value).lower() for value in (plan.get("preferred_sources") or [])]
    jurisdictions = [repair_text(value) for value in (plan.get("jurisdictions") or []) if repair_text(value)]

    reranked = []
    for item in items:
        current = dict(item)
        text = _item_text(current)
        item_terms = _terms(text)
        title_terms = _terms(current.get("title", ""))
        overlap = query_terms & item_terms
        domain_overlap = domain_signals & item_terms
        source_kind = repair_text(current.get("source_kind")).lower()
        channels = set(current.get("search_channels") or [])

        score = float(current.get("hybrid_score") or current.get("score") or 0)
        score += min(len(overlap) * 0.006, 0.036)
        score += min(len(domain_overlap) * 0.008, 0.048)
        score += max(float(current.get("vector_score") or 0) - 0.52, 0.0) * 0.12
        if {"lexical", "vector"} <= channels:
            score += 0.01
        if "graph" in channels:
            score += min(max(float(current.get("graph_score") or 0.0), 0.0) * 0.025, 0.02)
        if source_kind in preferred_sources:
            score += 0.006 if preferred_sources.index(source_kind) == 0 else 0.003
        jurisdiction_matched = _jurisdiction_match(current, jurisdictions)
        if jurisdiction_matched:
            score += 0.04
        if source_kind == "case" and {"tenant", "landlord", "eviction", "lease"} & query_terms:
            score += 0.012
        if source_kind == "law" and {"act", "statute", "section", "regulation"} & title_terms:
            score += 0.004
        if "commercial" in query_terms:
            if {"commercial", "tenancies"} <= title_terms or {"commercial", "tenancy"} <= title_terms:
                score += 0.08
            if source_kind == "law" and {"landlord", "tenant"} <= title_terms:
                score += 0.03

        penalty = 0.0
        if {"tenant", "landlord", "eviction", "lease"} & query_terms:
            if not ({"tenant", "landlord", "lease", "tenancy", "residential", "eviction", "ltb", "onltb"} & item_terms):
                penalty += 0.022
            if _BAD_TENANCY_DRIFT & title_terms:
                penalty += 0.018
        if channels == {"vector"} and not domain_overlap and float(current.get("vector_score") or 0) < 0.59:
            penalty += 0.02
        if "commercial" in query_terms and _COMMERCIAL_DRIFT & item_terms and "commercial" not in item_terms:
            penalty += 0.05
        score -= penalty

        current["rerank_score"] = round(score, 6)
        current["score"] = current["rerank_score"]
        current["rerank_signals"] = {
            "term_overlap": sorted(overlap)[:12],
            "domain_overlap": sorted(domain_overlap)[:12],
            "graph_supported": "graph" in channels,
            "jurisdiction_match": jurisdiction_matched,
            "penalty": round(penalty, 6),
        }
        reranked.append(current)

    reranked.sort(
        key=lambda item: (
            float(item.get("rerank_score") or 0),
            float(item.get("vector_score") or 0),
            float(item.get("hybrid_score") or 0),
        ),
        reverse=True,
    )
    if limit:
        bounded_limit = max(1, int(limit))
        return _ensure_preferred_source_coverage(reranked, preferred_sources, bounded_limit)
    return reranked
