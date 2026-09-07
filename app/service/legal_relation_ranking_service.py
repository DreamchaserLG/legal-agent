from __future__ import annotations

import math
import re
from collections.abc import Iterable

from app.service.common_service import plain_text_preview, repair_text


LEGAL_STOPWORDS = {
    "about",
    "after",
    "against",
    "also",
    "and",
    "any",
    "are",
    "before",
    "between",
    "case",
    "claim",
    "court",
    "dispute",
    "does",
    "for",
    "from",
    "have",
    "into",
    "law",
    "legal",
    "matter",
    "over",
    "party",
    "related",
    "rule",
    "section",
    "that",
    "the",
    "their",
    "this",
    "under",
    "with",
}

LEGAL_SIGNAL_TERMS = {
    "act",
    "appeal",
    "breach",
    "causation",
    "contract",
    "damages",
    "disclosure",
    "duty",
    "eviction",
    "fiduciary",
    "fraud",
    "lease",
    "liability",
    "misrepresentation",
    "negligence",
    "notice",
    "purchaser",
    "remedy",
    "standard",
    "statute",
    "tenant",
    "termination",
    "vendor",
}

CASE_FIELDS = (
    ("case_title", "案例标题", 3.2),
    ("case_type", "案件类型", 1.4),
    ("case_summary", "案例摘要", 2.5),
    ("case_facts", "案件事实", 2.2),
    ("case_raw_text", "案例全文", 1.2),
    ("judgment_result", "裁判结果", 1.8),
    ("match_reason", "既有关联理由", 2.0),
    ("evidence_excerpt", "既有证据片段", 2.8),
)

LAW_FIELDS = (
    ("rule_title", "法规名称", 3.2),
    ("article_no", "条款编号", 2.0),
    ("article_summary", "法规摘要", 2.2),
    ("article_text", "法规正文", 1.1),
    ("matched_alias", "法规引用", 3.0),
)


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize(value: str | None) -> str:
    return re.sub(r"\s+", " ", repair_text(value)).strip()


def _token_terms(values: Iterable[str], limit: int = 32) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str):
        clean = _normalize(term).strip(" ,.;:()[]{}\"'")
        key = clean.lower()
        if not clean or key in seen or key in LEGAL_STOPWORDS:
            return
        if len(key) < 3:
            return
        seen.add(key)
        terms.append(clean)

    for value in values or []:
        text = _normalize(value)
        if not text:
            continue
        if len(text.split()) <= 6:
            add(text)
        tokens = [
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text)
            if token.lower() not in LEGAL_STOPWORDS
        ]
        for size in (3, 2):
            for index in range(0, max(0, len(tokens) - size + 1)):
                add(" ".join(tokens[index:index + size]))
        for token in tokens:
            add(token)
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            add(token)
        if len(terms) >= limit:
            break
    return terms[:limit]


def _term_pattern(term: str) -> re.Pattern:
    if re.search(r"[A-Za-z0-9]", term):
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", re.IGNORECASE)
    return re.compile(re.escape(term), re.IGNORECASE)


def _term_weight(term: str, original_terms: set[str]) -> float:
    lowered = term.lower()
    words = lowered.split()
    weight = 1.0
    if lowered in original_terms:
        weight += 0.7
    if len(words) >= 3:
        weight += 0.8
    elif len(words) == 2:
        weight += 0.45
    if any(token in LEGAL_SIGNAL_TERMS for token in words) or lowered in LEGAL_SIGNAL_TERMS:
        weight += 0.25
    return weight


def _score_field(text_value: str, terms: list[str], original_terms: set[str], field_weight: float) -> tuple[float, set[str], dict | None]:
    text = plain_text_preview(text_value)[:2200]
    if not text:
        return 0.0, set(), None

    matched_terms: set[str] = set()
    score = 0.0
    best_hit = None
    best_hit_score = 0.0
    length_norm = max(1.0, math.sqrt(max(80, len(text)) / 420.0))

    for term in terms:
        matches = list(_term_pattern(term).finditer(text))
        if not matches:
            continue
        matched_terms.add(term.lower())
        occurrences = min(len(matches), 4)
        term_score = field_weight * _term_weight(term, original_terms) * (1.0 + math.log1p(occurrences)) / length_norm
        score += term_score
        if term_score > best_hit_score:
            match = matches[0]
            start = max(0, match.start() - 80)
            end = min(len(text), match.end() + 120)
            best_hit_score = term_score
            best_hit = {
                "term": text[match.start():match.end()],
                "before": text[start:match.start()].strip(),
                "after": text[match.end():end].strip(),
            }
    return score, matched_terms, best_hit


def _score_fields(row: dict, field_specs: tuple[tuple[str, str, float], ...], terms: list[str], original_terms: set[str]) -> tuple[float, set[str], dict | None]:
    total = 0.0
    matched_terms: set[str] = set()
    best_hit = None
    best_score = 0.0
    for key, label, weight in field_specs:
        field_score, field_terms, hit = _score_field(row.get(key) or "", terms, original_terms, weight)
        total += field_score
        matched_terms.update(field_terms)
        if hit and field_score > best_score:
            best_score = field_score
            best_hit = {**hit, "label": label, "field": key}
    return total, matched_terms, best_hit


def _normal_score(value: float, scale: float) -> float:
    if value <= 0:
        return 0.0
    return min(1.0, math.log1p(value) / math.log1p(scale))


def _relation_type_bonus(row: dict) -> float:
    relation = _normalize(row.get("relation_type") or row.get("match_source")).lower()
    if relation in {"citation", "cited", "official_title"}:
        return 0.10
    if relation in {"applied", "direct_mention"}:
        return 0.08
    if relation in {"explained", "alias"}:
        return 0.06
    if relation in {"topic_inference", "keyword"}:
        return -0.04
    return 0.0


def rank_case_rule_relation(
    row: dict,
    keywords: list[str],
    *,
    original_keywords: list[str] | None = None,
) -> dict:
    """BM25-style local reranker for one case-rule candidate.

    The score is intentionally conservative: a candidate must have case-side
    evidence and either law-side evidence or a strong stored relation.
    """
    terms = _token_terms(keywords, limit=36)
    if not terms:
        return {
            "rank_score": 0.0,
            "keyword_score": 0.0,
            "case_hit_count": 0,
            "law_hit_count": 0,
            "evidence_excerpt": "",
            "match_reason": "",
            "case_hit": None,
            "law_hit": None,
        }

    original_terms = {term.lower() for term in _token_terms(original_keywords or keywords, limit=36)}
    case_score, case_terms, case_hit = _score_fields(row, CASE_FIELDS, terms, original_terms)
    law_score, law_terms, law_hit = _score_fields(row, LAW_FIELDS, terms, original_terms)
    shared_terms = case_terms & law_terms
    raw_relation = min(max(_safe_float(row.get("match_score")), 0.0), 1.0)
    authority = min(max(_safe_int(row.get("court_rank")), 0), 5) / 5.0

    if not case_terms:
        rank_score = 0.0
    elif not law_terms and raw_relation < 0.78:
        rank_score = 0.0
    else:
        case_norm = _normal_score(case_score, 18.0)
        law_norm = _normal_score(law_score, 12.0)
        shared_norm = min(1.0, len(shared_terms) / max(2, min(len(case_terms), len(law_terms)) or 2))
        rank_score = (
            case_norm * 0.44
            + law_norm * 0.24
            + shared_norm * 0.16
            + raw_relation * 0.11
            + authority * 0.05
            + _relation_type_bonus(row)
        )
        rank_score = max(0.0, min(1.0, rank_score))

    keyword_score = round(min(24.0, rank_score * 28.0), 4)
    evidence_parts = []
    if case_hit:
        evidence_parts.append(f"{case_hit['label']}: {case_hit['before']} {case_hit['term']} {case_hit['after']}".strip())
    if law_hit:
        evidence_parts.append(f"{law_hit['label']}: {law_hit['before']} {law_hit['term']} {law_hit['after']}".strip())
    evidence_excerpt = " | ".join(part for part in evidence_parts if part)[:900]

    matched_terms = sorted(shared_terms or (case_terms | law_terms), key=lambda item: (-len(item), item))[:6]
    reason = ""
    if keyword_score > 0:
        reason = (
            "本地关系重排命中："
            f"案例侧 {len(case_terms)} 个术语、法规侧 {len(law_terms)} 个术语"
        )
        if shared_terms:
            reason += f"，共同术语 {', '.join(matched_terms[:4])}"
        if raw_relation:
            reason += f"，既有关联分 {raw_relation:.2f}"

    return {
        "rank_score": round(rank_score, 6),
        "keyword_score": keyword_score,
        "case_hit_count": len(case_terms),
        "law_hit_count": len(law_terms),
        "shared_hit_count": len(shared_terms),
        "evidence_excerpt": evidence_excerpt,
        "match_reason": reason,
        "case_hit": case_hit,
        "law_hit": law_hit,
    }
