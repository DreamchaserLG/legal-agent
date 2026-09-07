from __future__ import annotations

import re
from collections.abc import Iterable

from app.service.common_service import plain_text_preview, repair_text


LEGAL_SKILL_CATALOG = [
    {
        "id": "lawthinker_evm",
        "name": "LawThinker Explore-Verify-Memorize",
        "source_url": "https://github.com/yxy-919/LawThinker-agent",
        "role": "Use retrieved authorities only after evidence verification, then preserve reusable retrieval context.",
    },
    {
        "id": "legalbenchrag_passage_precision",
        "name": "LegalBench-RAG passage precision",
        "source_url": "https://github.com/zeroentropy-cc/legalbenchrag",
        "role": "Prefer exact legal passages, citations, and authority snippets over broad keyword hits.",
    },
    {
        "id": "lexnlp_entity_extraction",
        "name": "LexNLP-style legal entity extraction",
        "source_url": "https://github.com/LexPredict/lexpredict-lexnlp",
        "role": "Extract legal citations, statutes, section references, courts, dates, and legal concepts before search.",
    },
    {
        "id": "caselink_authority_rerank",
        "name": "CaseLink authority-aware reranking",
        "source_url": "https://github.com/yanran-tang/CaseLink",
        "role": "Rerank cases by authority signals and links between cases and governing rules.",
    },
    {
        "id": "eyecite_citation_validation",
        "name": "eyecite citation validation",
        "source_url": "https://github.com/freelawproject/eyecite",
        "role": "Detect legal citations in generated reasoning so unsupported authority mentions can be flagged.",
    },
    {
        "id": "ragas_context_precision",
        "name": "RAGAS-style context precision",
        "source_url": "https://github.com/explodinggradients/ragas",
        "role": "Score whether retrieved context is sufficiently grounded before allowing high-confidence answers.",
    },
    {
        "id": "guardrails_grounded_generation",
        "name": "Guardrails grounded generation",
        "source_url": "https://github.com/NVIDIA/NeMo-Guardrails",
        "role": "Constrain generated conclusions to supplied evidence and add reliability warnings when evidence is weak.",
    },
]

NEUTRAL_CITATION_RE = re.compile(
    r"\b(?:19|20)\d{2}\s+(?:SCC|FCA|FC|ONCA|ONSC|ONCJ|BCCA|BCSC|ABCA|ABKB|ABQB|QCCA|QCCS|MBCA|MBKB|SKCA|SKKB|NSCA|NSSC|NBCA|NBKB|NLCA|NLSC|YKCA|YKSC|NTCA|NTSC|NUCA|NUCJ)\s+\d+\b",
    re.IGNORECASE,
)
CASE_STYLE_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9&'.,() -]{1,80}\s+v\.?\s+[A-Z][A-Za-z0-9&'.,() -]{1,80}\b"
)
STATUTE_TITLE_RE = re.compile(
    r"\b[A-Z][A-Za-z0-9&'.,() -]{2,120}\s+(?:Act|Code|Rules|Regulation|Regulations|Charter|Convention|Order)\b"
)
SECTION_RE = re.compile(r"\b(?:s\.|ss\.|sec\.|section)\s*\d+[A-Za-z0-9().-]*\b", re.IGNORECASE)

COURT_MARKERS = {
    "scc": "Supreme Court of Canada",
    "supreme court of canada": "Supreme Court of Canada",
    "fca": "Federal Court of Appeal",
    "federal court of appeal": "Federal Court of Appeal",
    "fc": "Federal Court",
    "onca": "Ontario Court of Appeal",
    "onsc": "Ontario Superior Court of Justice",
    "bc ca": "British Columbia Court of Appeal",
    "bcca": "British Columbia Court of Appeal",
    "bcsc": "British Columbia Supreme Court",
}

DOMAIN_SKILL_MAP = {
    "real_estate": {
        "markers": (
            "real estate",
            "property",
            "land",
            "vendor",
            "purchaser",
            "realtor",
            "broker",
            "latent defect",
            "disclosure",
            "\u623f\u4ea7",
            "\u5730\u4ea7",
            "\u4e0d\u52a8\u4ea7",
            "\u4e70\u5356",
            "\u62ab\u9732",
        ),
        "terms": (
            "vendor disclosure",
            "latent defect",
            "fraudulent misrepresentation",
            "negligent misrepresentation",
            "real estate agent duty",
            "Land Titles Act",
            "Conveyancing and Law of Property Act",
        ),
    },
    "contract": {
        "markers": (
            "contract",
            "agreement",
            "breach",
            "termination",
            "specific performance",
            "\u5408\u540c",
            "\u8fdd\u7ea6",
        ),
        "terms": (
            "breach of contract",
            "contract interpretation",
            "damages for breach",
            "specific performance",
            "good faith performance",
        ),
    },
    "employment": {
        "markers": (
            "employment",
            "employee",
            "employer",
            "dismissal",
            "termination",
            "severance",
            "\u52b3\u52a8",
            "\u96c7\u4f63",
            "\u89e3\u96c7",
        ),
        "terms": (
            "wrongful dismissal",
            "constructive dismissal",
            "reasonable notice",
            "employment standards",
            "duty to mitigate",
        ),
    },
    "tort": {
        "markers": (
            "tort",
            "negligence",
            "injury",
            "liability",
            "duty of care",
            "standard of care",
            "\u4fb5\u6743",
            "\u8fc7\u5931",
            "\u635f\u5bb3",
        ),
        "terms": (
            "duty of care",
            "standard of care",
            "causation",
            "contributory negligence",
            "damages",
        ),
    },
    "family": {
        "markers": (
            "family",
            "divorce",
            "custody",
            "spousal",
            "child support",
            "\u79bb\u5a5a",
            "\u629a\u517b",
            "\u5a5a\u59fb",
        ),
        "terms": (
            "best interests of the child",
            "child support",
            "spousal support",
            "parenting time",
            "Family Law Act",
            "Divorce Act",
        ),
    },
    "criminal_fraud": {
        "markers": (
            "criminal",
            "fraud",
            "offence",
            "sentencing",
            "prosecution",
            "\u5211\u4e8b",
            "\u8bc8\u9a97",
            "\u72af\u7f6a",
        ),
        "terms": (
            "fraud over",
            "Criminal Code section 380",
            "dishonesty and deprivation",
            "sentencing fraud",
            "proof beyond reasonable doubt",
        ),
    },
    "privacy": {
        "markers": (
            "privacy",
            "personal information",
            "data",
            "consent",
            "pipeda",
            "\u9690\u79c1",
            "\u4e2a\u4eba\u4fe1\u606f",
            "\u6570\u636e",
        ),
        "terms": (
            "PIPEDA",
            "Personal Information Protection and Electronic Documents Act",
            "meaningful consent",
            "privacy breach",
            "data retention",
        ),
    },
    "sanctions": {
        "markers": (
            "ofac",
            "sanctions",
            "sdn",
            "designated",
            "blocked property",
            "\u5236\u88c1",
            "\u540d\u5355",
        ),
        "terms": (
            "OFAC designation",
            "SDN List",
            "blocked property",
            "delisting petition",
            "specific license",
        ),
    },
}

BROAD_LOW_VALUE_TERMS = {
    "case",
    "law",
    "legal",
    "court",
    "issue",
    "matter",
    "claim",
    "dispute",
    "analysis",
    "canada",
}

GENERAL_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "been",
    "but",
    "by",
    "did",
    "do",
    "does",
    "for",
    "from",
    "had",
    "has",
    "have",
    "in",
    "into",
    "is",
    "it",
    "its",
    "of",
    "on",
    "or",
    "that",
    "the",
    "their",
    "this",
    "to",
    "was",
    "were",
    "with",
    "after",
    "before",
    "failed",
    "seeking",
    "sues",
}


def get_legal_skill_catalog() -> list[dict]:
    return [dict(item) for item in LEGAL_SKILL_CATALOG]


def _dedupe(values: Iterable[str], limit: int | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        clean = repair_text(value).strip(" \t\r\n,;")
        if not clean:
            continue
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if limit and len(result) >= limit:
            break
    return result


def _as_text(value: str | list[str]) -> str:
    if isinstance(value, list):
        return " ".join(repair_text(item) for item in value if repair_text(item))
    return repair_text(value)


def _base_terms(value: str | list[str]) -> list[str]:
    if isinstance(value, list):
        return _dedupe([str(item) for item in value])
    text = repair_text(value)

    def phrase_terms(raw: str) -> list[str]:
        tokens = [
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", raw)
            if token.lower() not in GENERAL_STOPWORDS
        ]
        bigrams = [f"{left} {right}" for left, right in zip(tokens, tokens[1:]) if left != right]
        return bigrams + tokens

    comma_parts = re.split(r"[,;\n]+", text)
    if len(comma_parts) > 1:
        terms = []
        for part in comma_parts:
            clean = repair_text(part)
            if not clean:
                continue
            if len(clean.split()) <= 4:
                terms.append(clean)
            else:
                terms.extend(phrase_terms(clean))
        return _dedupe(terms, limit=12)
    return _dedupe(phrase_terms(text), limit=12)


def extract_legal_entities(value: str | list[str]) -> dict:
    text = _as_text(value)
    lowered = text.lower()
    courts = []
    for marker, label in COURT_MARKERS.items():
        if marker in lowered:
            courts.append(label)
    return {
        "case_citations": _dedupe(NEUTRAL_CITATION_RE.findall(text) + CASE_STYLE_RE.findall(text), limit=8),
        "statute_references": _dedupe(STATUTE_TITLE_RE.findall(text), limit=8),
        "section_references": _dedupe(SECTION_RE.findall(text), limit=8),
        "courts": _dedupe(courts, limit=5),
    }


def _domain_terms(text: str) -> tuple[list[str], list[str]]:
    lowered = text.lower()
    matched_domains: list[str] = []
    terms: list[str] = []
    for domain, profile in DOMAIN_SKILL_MAP.items():
        markers = profile.get("markers", ())
        if any(marker and (marker in text or marker.lower() in lowered) for marker in markers):
            matched_domains.append(domain)
            terms.extend(profile.get("terms", ()))
    return _dedupe(matched_domains), _dedupe(terms)


def enhance_legal_retrieval_keywords(
    keywords_input: str | list[str],
    *,
    module: str = "canada",
    base_keywords: list[str] | None = None,
    keyword_weights: dict[str, float] | None = None,
    max_keywords: int = 18,
) -> dict:
    text = _as_text(keywords_input)
    base = _dedupe(base_keywords or _base_terms(keywords_input), limit=max_keywords)
    entities = extract_legal_entities(text)
    domains, domain_terms = _domain_terms(text)

    ordered = []
    ordered.extend(entities["case_citations"])
    ordered.extend(entities["statute_references"])
    ordered.extend(entities["section_references"])
    ordered.extend(domain_terms)
    ordered.extend(base)
    ordered.extend(entities["courts"])

    if module == "us_sanctions" and "sanctions" not in domains:
        if any(token in text.lower() for token in ("ofac", "sdn", "sanction", "blocked")):
            domains.append("sanctions")

    keywords = _dedupe(ordered, limit=max_keywords)
    weights: dict[str, float] = {}
    for key, value in (keyword_weights or {}).items():
        clean = repair_text(key)
        if clean:
            weights[clean] = float(value or 0.5)
            weights[clean.lower()] = float(value or 0.5)

    entity_terms = set(item.lower() for group in entities.values() for item in group)
    domain_term_set = {item.lower() for item in domain_terms}
    base_set = {item.lower() for item in base}
    for keyword in keywords:
        lower = keyword.lower()
        if lower in entity_terms:
            weight = 1.35
        elif lower in domain_term_set:
            weight = 0.85
        elif lower in base_set:
            weight = 1.0
        elif lower in BROAD_LOW_VALUE_TERMS:
            weight = 0.35
        else:
            weight = 0.65
        weights[keyword] = max(float(weights.get(keyword, 0)), weight)
        weights[lower] = max(float(weights.get(lower, 0)), weight)

    return {
        "keywords": keywords,
        "keyword_weights": weights,
        "entities": entities,
        "matched_domains": domains,
        "skills": get_legal_skill_catalog(),
        "strategy": "entity_expansion_passage_precision_authority_rerank",
    }


def _contains_any(haystack: str, needles: Iterable[str]) -> list[str]:
    matched = []
    for needle in needles or []:
        clean = repair_text(needle)
        if clean and clean.lower() in haystack:
            matched.append(clean)
    return _dedupe(matched, limit=5)


def score_result_with_legal_skills(row: dict, skill_profile: dict) -> dict:
    item = dict(row)
    text = " ".join(
        repair_text(part)
        for part in [
            item.get("title"),
            item.get("title_primary"),
            item.get("subtitle"),
            item.get("summary"),
            item.get("summary_primary"),
            item.get("excerpt"),
            plain_text_preview(item.get("raw_text") or "")[:600],
            item.get("source_code"),
            item.get("court_code"),
            item.get("court_level_label"),
        ]
        if repair_text(part)
    ).lower()

    entities = skill_profile.get("entities") or {}
    keywords = skill_profile.get("keywords") or []
    matched_citations = _contains_any(text, entities.get("case_citations") or [])
    matched_statutes = _contains_any(text, entities.get("statute_references") or [])
    matched_sections = _contains_any(text, entities.get("section_references") or [])
    matched_keywords = _contains_any(text, keywords[:12])

    skill_score = 0.0
    skill_score += min(0.35, 0.12 * len(matched_citations))
    skill_score += min(0.25, 0.08 * len(matched_statutes))
    skill_score += min(0.16, 0.04 * len(matched_sections))
    skill_score += min(0.34, 0.045 * len(matched_keywords))

    source_code = repair_text(item.get("source_code")).lower()
    if source_code == "canlii":
        skill_score += 0.06
    if source_code in {"ca_federal_act", "ca_federal_regulation", "on_statute", "on_regulation"}:
        skill_score += 0.05
    try:
        court_level = int(item.get("court_level") or 0)
    except (TypeError, ValueError):
        court_level = 0
    if court_level >= 4:
        skill_score += 0.05

    raw_score = 0.0
    try:
        raw_score = float(item.get("score") or item.get("relevance_score") or 0)
    except (TypeError, ValueError):
        raw_score = 0.0
    combined = min(1.0, max(0.0, raw_score * 0.68 + skill_score * 0.32))

    signals = []
    if matched_citations:
        signals.append("citation_match")
    if matched_statutes:
        signals.append("statute_match")
    if matched_sections:
        signals.append("section_match")
    if matched_keywords:
        signals.append("issue_keyword_match")
    if court_level >= 4:
        signals.append("higher_court_authority")

    item["legal_skill_score"] = round(min(skill_score, 1.0), 4)
    item["combined_relevance"] = round(combined, 4)
    item["legal_skill_signals"] = signals
    item["matched_legal_terms"] = _dedupe(
        matched_citations + matched_statutes + matched_sections + matched_keywords,
        limit=8,
    )
    return item


def apply_legal_result_verification(
    rows: list[dict],
    skill_profile: dict,
    *,
    sort: str = "relevance",
) -> list[dict]:
    scored = [score_result_with_legal_skills(row, skill_profile) for row in rows]
    if sort != "relevance":
        return scored
    return sorted(
        scored,
        key=lambda item: (
            float(item.get("combined_relevance") or 0),
            float(item.get("legal_skill_score") or 0),
            int(item.get("court_level") or 0),
            item.get("published_at") or "",
        ),
        reverse=True,
    )
