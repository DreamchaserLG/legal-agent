from __future__ import annotations

import re
from collections.abc import Iterable

from app.service.common_service import plain_text_preview, repair_text


STOPWORDS = {
    "about",
    "after",
    "against",
    "and",
    "before",
    "between",
    "case",
    "claim",
    "court",
    "dispute",
    "failed",
    "from",
    "legal",
    "matter",
    "over",
    "related",
    "seeking",
    "that",
    "their",
    "this",
    "with",
}


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


def _tokenize_terms(values: Iterable[str], limit: int = 18) -> list[str]:
    terms: list[str] = []
    seen: set[str] = set()

    def add(term: str):
        clean = repair_text(term).strip(" ,.;:()[]")
        key = clean.lower()
        if not clean or key in seen or key in STOPWORDS:
            return
        if len(key) < 3:
            return
        seen.add(key)
        terms.append(clean)

    for value in values or []:
        text = repair_text(value)
        if not text:
            continue
        if len(text.split()) <= 5:
            add(text)
        tokens = [
            token.lower()
            for token in re.findall(r"[A-Za-z][A-Za-z'-]{2,}", text)
            if token.lower() not in STOPWORDS
        ]
        for left, right in zip(tokens, tokens[1:]):
            add(f"{left} {right}")
        for token in tokens:
            add(token)
        for token in re.findall(r"[\u4e00-\u9fff]{2,}", text):
            add(token)
        if len(terms) >= limit:
            break
    return terms[:limit]


def _find_highlight(text: str, terms: list[str], *, label: str, field: str) -> dict | None:
    source_text = plain_text_preview(text)[:900]
    lowered = source_text.lower()
    best_term = ""
    best_index = -1
    best_end = -1
    for term in terms:
        term_text = repair_text(term)
        if re.search(r"[A-Za-z0-9]", term_text):
            match = re.search(rf"(?<![A-Za-z0-9]){re.escape(term_text)}(?![A-Za-z0-9])", source_text, re.IGNORECASE)
            index = match.start() if match else -1
            end = match.end() if match else -1
        else:
            index = lowered.find(term_text.lower())
            end = index + len(term_text) if index >= 0 else -1
        if index < 0:
            continue
        if not best_term or len(term) > len(best_term):
            best_term = term_text
            best_index = index
            best_end = end
    if not best_term:
        return None
    start = max(0, best_index - 70)
    end = min(len(source_text), best_end + 90)
    return {
        "label": label,
        "field": field,
        "term": source_text[best_index:best_end],
        "before": source_text[start:best_index].strip(),
        "after": source_text[best_end:end].strip(),
    }


def _collect_highlights(case_entry: dict, rule_entries: list[dict], terms: list[str]) -> list[dict]:
    fields = [
        ("案例标题", "title", case_entry.get("title")),
        ("案情摘要", "summary", case_entry.get("summary") or case_entry.get("facts")),
        ("裁判结果", "judgment_result", case_entry.get("judgment_result")),
        ("匹配理由", "match_reason", case_entry.get("match_reason")),
        ("证据片段", "evidence_excerpt", case_entry.get("evidence_excerpt")),
    ]
    for rule in rule_entries or []:
        fields.extend(
            [
                ("法规名称", "law_title", rule.get("title")),
                ("条款编号", "article_no", rule.get("article_no")),
                ("法规摘要", "article_summary", rule.get("article_summary") or rule.get("article_text")),
                ("规则理由", "rule_match_reason", rule.get("match_reason")),
            ]
        )

    highlights = []
    seen = set()
    for label, field, value in fields:
        hit = _find_highlight(value or "", terms, label=label, field=field)
        if not hit:
            continue
        key = (hit["field"], hit["term"].lower(), hit["before"], hit["after"])
        if key in seen:
            continue
        seen.add(key)
        highlights.append(hit)
        if len(highlights) >= 5:
            break
    return highlights


def _level(score: float) -> str:
    if score >= 0.72:
        return "strong"
    if score >= 0.52:
        return "moderate"
    if score >= 0.34:
        return "limited"
    return "weak"


def _level_label(score: float) -> str:
    if score >= 0.72:
        return "强相关"
    if score >= 0.52:
        return "中等相关"
    if score >= 0.34:
        return "有限相关"
    return "弱相关"


def calculate_case_law_relevance(
    case_entry: dict,
    rule_entries: list[dict],
    *,
    keywords: list[str] | None = None,
    relation_status: str = "",
) -> dict:
    raw_relation_score = min(max(_safe_float(case_entry.get("match_score")), 0.0), 1.0)
    keyword_score = _safe_float(case_entry.get("keyword_score"))
    relation_rank_score = min(max(_safe_float(case_entry.get("relation_rank_score")), 0.0), 1.0)
    terms = _tokenize_terms(
        list(keywords or [])
        + [case_entry.get("case_type"), case_entry.get("match_reason")]
        + [rule.get("title") for rule in rule_entries or []]
        + [rule.get("article_no") for rule in rule_entries or []],
        limit=22,
    )
    highlights = _collect_highlights(case_entry, rule_entries, terms)
    case_highlight_count = sum(
        1
        for hit in highlights
        if hit.get("field") in {"title", "summary", "judgment_result", "match_reason", "evidence_excerpt"}
    )
    law_highlight_count = len(highlights) - case_highlight_count
    relation_is_pending = relation_status == "retrieved_pending_relation" or bool(case_entry.get("is_retrieved_result"))
    has_law = bool(rule_entries)
    has_source = bool(repair_text(case_entry.get("source_url")))
    court_rank = _safe_int(case_entry.get("court_rank"))

    score = 0.05
    if has_law:
        score += 0.10
    if raw_relation_score > 0:
        score += min(raw_relation_score, 1.0) * (0.16 if relation_is_pending else 0.22)
    if keyword_score > 0:
        score += min(keyword_score / 24.0, 1.0) * 0.18
    if relation_rank_score > 0:
        score += relation_rank_score * 0.08
    score += min(len(highlights), 4) * 0.055
    if any(hit.get("field") == "law_title" for hit in highlights):
        score += 0.05
    if any(hit.get("field") == "title" for hit in highlights):
        score += 0.04
    if any(hit.get("field") in {"match_reason", "evidence_excerpt", "rule_match_reason"} for hit in highlights):
        score += 0.05
    if has_source:
        score += 0.025
    if court_rank >= 4:
        score += 0.035
    elif court_rank >= 2:
        score += 0.015

    cap = 0.82
    if relation_is_pending:
        cap = min(cap, 0.42)
    if not has_law:
        cap = min(cap, 0.48)
    if not highlights:
        cap = min(cap, 0.36)
    elif len(highlights) == 1:
        cap = min(cap, 0.52)
    if case_highlight_count == 0:
        cap = min(cap, 0.46)
    if law_highlight_count == 0:
        cap = min(cap, 0.64)
    if not has_source:
        cap = min(cap, 0.68)

    score = round(max(0.05, min(score, cap)), 4)
    return {
        "score": score,
        "level": _level(score),
        "label": _level_label(score),
        "highlights": highlights,
        "terms": terms[:10],
        "raw_relation_score": raw_relation_score,
        "score_components": {
            "has_law": has_law,
            "has_source": has_source,
            "highlight_count": len(highlights),
            "case_highlight_count": case_highlight_count,
            "law_highlight_count": law_highlight_count,
            "court_rank": court_rank,
            "keyword_score": keyword_score,
            "relation_rank_score": relation_rank_score,
            "relation_status": relation_status or "formal_relation",
        },
    }


def apply_case_law_relevance(
    case_entry: dict,
    *,
    keywords: list[str] | None = None,
) -> dict:
    item = dict(case_entry)
    rules = [dict(rule) for rule in item.get("rules") or []]
    relation_status = repair_text(item.get("relation_status"))
    relevance = calculate_case_law_relevance(
        item,
        rules,
        keywords=keywords,
        relation_status=relation_status,
    )
    item["raw_match_score"] = relevance["raw_relation_score"]
    item["match_score"] = relevance["score"]
    item["relevance_score"] = relevance["score"]
    item["relevance_level"] = relevance["level"]
    item["relevance_label"] = relevance["label"]
    item["relevance_highlights"] = relevance["highlights"]
    item["relevance_terms"] = relevance["terms"]
    item["relevance_components"] = relevance["score_components"]
    item["relation_rank_score"] = relevance["score_components"].get("relation_rank_score", 0.0)
    updated_rules = []
    for rule in rules:
        rule_entry = dict(rule)
        rule_entry.setdefault("raw_match_score", _safe_float(rule_entry.get("match_score")))
        rule_entry["match_score"] = min(_safe_float(rule_entry.get("match_score")), relevance["score"])
        rule_entry["relevance_score"] = relevance["score"]
        rule_entry["relevance_label"] = relevance["label"]
        rule_entry["relevance_highlights"] = [
            hit for hit in relevance["highlights"]
            if hit.get("field") in {"law_title", "article_no", "article_summary", "rule_match_reason"}
        ]
        updated_rules.append(rule_entry)
    item["rules"] = updated_rules
    return item
