from __future__ import annotations

import re

from app.service.common_service import repair_text


_GENERIC_TERMS = {
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
    "problem",
    "claim",
    "claims",
}

_JURISDICTION_HINTS = {
    "ontario": ["Ontario"],
    "onltb": ["Ontario", "ONLTB", "Landlord and Tenant Board"],
    "ltb": ["Ontario", "ONLTB", "Landlord and Tenant Board"],
    "canada": ["Canada"],
}

_DOMAIN_HINTS = {
    "tenant": ["residential tenancy", "tenant", "landlord", "lease"],
    "tenancy": ["residential tenancy", "tenant", "landlord", "lease"],
    "landlord": ["residential tenancy", "tenant", "landlord", "lease"],
    "lease": ["residential tenancy", "tenant", "landlord", "lease"],
    "commercial": ["commercial tenancy", "commercial lease"],
    "eviction": ["eviction", "termination", "possession", "Residential Tenancies Act"],
    "repair": ["repair", "maintenance", "habitability", "vital services"],
    "repairs": ["repair", "maintenance", "habitability", "vital services"],
    "habitability": ["repair", "maintenance", "habitability", "vital services"],
    "employment": ["employment", "termination", "dismissal"],
    "contract": ["contract", "breach", "damages"],
    "negligence": ["negligence", "duty of care", "causation", "damages"],
}

_LAW_EXPANSIONS = {
    "residential tenancy": ["Residential Tenancies Act", "landlord obligations", "tenant remedies"],
    "commercial tenancy": ["Commercial Tenancies Act", "Landlord and Tenant Act", "commercial lease"],
    "eviction": ["eviction", "termination order", "notice of termination"],
    "repair": ["repair", "maintenance", "vital services", "reasonable enjoyment"],
}

_CASE_EXPANSIONS = {
    "residential tenancy": ["ONLTB", "Landlord and Tenant Board", "lease and tenancy"],
    "commercial tenancy": ["commercial tenancy", "commercial lease"],
    "eviction": ["eviction ordered", "relief from eviction", "termination for cause"],
    "repair": ["repair", "maintenance", "unit uninhabitable", "vital services"],
}


def _terms(text: str) -> list[str]:
    cleaned = repair_text(text).lower()
    return re.findall(r"[a-z][a-z0-9'./-]{2,}", cleaned)


def _unique(values: list[str], limit: int = 24) -> list[str]:
    result = []
    seen = set()
    for value in values:
        clean = repair_text(value)
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def build_legal_query_plan(query: str, keywords: list[str] | None = None) -> dict:
    clean_query = repair_text(query)
    keyword_text = " ".join([repair_text(item) for item in (keywords or [])])
    all_terms = _terms(" ".join([clean_query, keyword_text]))
    term_set = set(all_terms)

    jurisdictions: list[str] = []
    domains: list[str] = []
    issues: list[str] = []
    law_expansions: list[str] = []
    case_expansions: list[str] = []

    for term in all_terms:
        jurisdictions.extend(_JURISDICTION_HINTS.get(term, []))
        hints = _DOMAIN_HINTS.get(term, [])
        domains.extend(hints)
        if term in {"eviction", "termination", "repair", "repairs", "habitability", "maintenance"}:
            issues.extend(hints or [term])

    for value in domains + issues:
        law_expansions.extend(_LAW_EXPANSIONS.get(value, []))
        case_expansions.extend(_CASE_EXPANSIONS.get(value, []))

    meaningful_terms = [
        term
        for term in all_terms
        if len(term) >= 4 and term not in _GENERIC_TERMS
    ]

    base_terms = _unique([clean_query] + list(keywords or []) + meaningful_terms)
    law_terms = _unique(jurisdictions + domains + issues + law_expansions + base_terms)
    case_terms = _unique(jurisdictions + domains + issues + case_expansions + base_terms)

    if not law_terms:
        law_terms = base_terms
    if not case_terms:
        case_terms = base_terms

    preferred_sources = ["law", "case"]
    if "commercial" in term_set:
        preferred_sources = ["law", "case"]
    if {"tenant", "landlord", "eviction", "repair", "repairs"} & term_set:
        preferred_sources = ["case", "law"]
    if "commercial" in term_set:
        preferred_sources = ["law", "case"]

    return {
        "original_query": clean_query,
        "keywords": _unique(list(keywords or [])),
        "jurisdictions": _unique(jurisdictions),
        "domains": _unique(domains),
        "issues": _unique(issues),
        "meaningful_terms": _unique(meaningful_terms),
        "law_query": " ".join(law_terms),
        "case_query": " ".join(case_terms),
        "preferred_sources": preferred_sources,
    }
