from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.service.common_service import plain_text_preview, repair_text
from app.service.legal_skill_service import extract_legal_entities


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _normalize_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", repair_text(value).lower()).strip()


def _dedupe(values, limit: int | None = None) -> list[str]:
    result = []
    seen = set()
    for value in values or []:
        clean = repair_text(value)
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if limit and len(result) >= limit:
            break
    return result


def _title_supported(candidate: str, available_titles: list[str]) -> bool:
    candidate_key = _normalize_key(candidate)
    if not candidate_key:
        return False
    for title in available_titles:
        title_key = _normalize_key(title)
        if not title_key:
            continue
        if candidate_key in title_key or title_key in candidate_key:
            return True
        if SequenceMatcher(None, candidate_key, title_key).ratio() >= 0.82:
            return True
    return False


def _case_entries(module_packet: dict, groups: list[dict], precedents: list[dict]) -> list[dict]:
    rows = []
    rows.extend((module_packet or {}).get("case_law_rows") or [])
    for group in groups or []:
        rows.extend(group.get("cases") or [])
    rows.extend(precedents or [])

    result = []
    seen = set()
    for row in rows:
        title = repair_text(row.get("title") or row.get("case_title"))
        key = _normalize_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(dict(row))
    return result


def _law_entries(module_packet: dict, groups: list[dict]) -> list[dict]:
    rows = []
    rows.extend((module_packet or {}).get("relevant_laws") or [])
    rows.extend(groups or [])
    result = []
    seen = set()
    for row in rows:
        title = repair_text(row.get("title") or row.get("rule_title"))
        key = _normalize_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(dict(row))
    return result


def _prediction_text(prediction: dict) -> str:
    values = [
        prediction.get("predicted_outcome"),
        prediction.get("likely_prevailing_party"),
        prediction.get("reasoning"),
    ]
    values.extend(prediction.get("reason_points") or [])
    values.extend(prediction.get("risk_points") or [])
    values.extend(prediction.get("supporting_case_titles") or [])
    return " ".join(repair_text(value) for value in values if repair_text(value))


def build_prediction_evidence_quality(
    *,
    analysis_result: dict,
    prediction: dict,
    module_packet: dict,
    supporting_case_groups: list[dict],
    precedents: list[dict],
) -> dict:
    cases = _case_entries(module_packet, supporting_case_groups, precedents)
    laws = _law_entries(module_packet, supporting_case_groups)

    available_case_titles = _dedupe([row.get("title") or row.get("case_title") for row in cases], limit=80)
    available_law_titles = _dedupe([row.get("title") or row.get("rule_title") for row in laws], limit=80)
    prediction_entities = extract_legal_entities(_prediction_text(prediction))

    supporting_titles = _dedupe(prediction.get("supporting_case_titles") or [], limit=20)
    unsupported_titles = [
        title for title in supporting_titles
        if not _title_supported(title, available_case_titles)
    ]

    available_blob = " ".join(available_case_titles + available_law_titles).lower()
    unsupported_citations = [
        item for item in prediction_entities.get("case_citations", [])
        if item.lower() not in available_blob
    ]
    unsupported_statutes = [
        item for item in prediction_entities.get("statute_references", [])
        if item.lower() not in available_blob
    ]

    case_count = len(cases)
    law_count = len(laws)
    cases_with_summary = len([row for row in cases if repair_text(row.get("summary") or row.get("facts"))])
    cases_with_url = len([row for row in cases if repair_text(row.get("source_url") or row.get("url"))])
    cases_with_rules = len([row for row in cases if row.get("rules") or row.get("law_titles") or row.get("linked_law_titles")])
    higher_court_cases = len([
        row for row in cases
        if _safe_float(row.get("court_rank")) >= 4 or "supreme" in repair_text(row.get("court_level") or row.get("court_name")).lower()
    ])

    case_ratio = min(case_count / 4.0, 1.0)
    law_ratio = min(law_count / 3.0, 1.0)
    summary_ratio = cases_with_summary / max(case_count, 1)
    url_ratio = cases_with_url / max(case_count, 1)
    rule_ratio = cases_with_rules / max(case_count, 1)
    authority_ratio = min(higher_court_cases / 2.0, 1.0)
    unsupported_penalty = 0.18 if unsupported_titles else 0.0
    unsupported_penalty += 0.12 if unsupported_citations or unsupported_statutes else 0.0

    score = (
        0.24 * case_ratio
        + 0.18 * law_ratio
        + 0.16 * summary_ratio
        + 0.12 * url_ratio
        + 0.14 * rule_ratio
        + 0.08 * authority_ratio
        + 0.08 * (1.0 if not unsupported_titles and not unsupported_citations and not unsupported_statutes else 0.0)
        - unsupported_penalty
    )
    score = round(max(0.0, min(score, 1.0)), 4)

    if score >= 0.72 and case_count >= 2 and law_count >= 1:
        status = "strong"
        max_confidence = 0.84
    elif score >= 0.45 and case_count >= 1 and law_count >= 1:
        status = "moderate"
        max_confidence = 0.68
    elif case_count >= 1 or law_count >= 1:
        status = "thin"
        max_confidence = 0.45
    else:
        status = "insufficient"
        max_confidence = 0.25

    warnings = []
    actions = []
    if case_count <= 0:
        warnings.append("No supporting case is available for the prediction.")
        actions.append("Retrieve at least one relevant case before presenting an outcome prediction.")
    if law_count <= 0:
        warnings.append("No governing statute or regulation is linked to the prediction.")
        actions.append("Map the case to at least one governing law before increasing confidence.")
    if unsupported_titles:
        warnings.append("Some supporting case titles in the generated answer are not present in retrieved evidence.")
        actions.append("Regenerate or edit the prediction so case titles come only from the retrieved case list.")
    if unsupported_citations or unsupported_statutes:
        warnings.append("The generated reasoning mentions legal authorities that were not found in retrieved evidence.")
        actions.append("Check citation/statute mentions against the retrieved laws and cases before relying on the answer.")
    if summary_ratio < 0.5 and case_count:
        warnings.append("Most supporting cases have weak summaries, so fact matching may be shallow.")
        actions.append("Import better case summaries or authorized full text for stronger comparison.")

    return {
        "status": status,
        "score": score,
        "max_recommended_confidence": max_confidence,
        "case_count": case_count,
        "law_count": law_count,
        "cases_with_summary": cases_with_summary,
        "cases_with_source_url": cases_with_url,
        "cases_with_rule_links": cases_with_rules,
        "higher_court_cases": higher_court_cases,
        "unsupported_case_titles": unsupported_titles,
        "unsupported_case_citations": unsupported_citations,
        "unsupported_statute_references": unsupported_statutes,
        "matched_case_titles": available_case_titles[:10],
        "matched_law_titles": available_law_titles[:10],
        "warnings": warnings,
        "recommended_actions": actions[:5],
        "skills": [
            {
                "id": "eyecite_citation_validation",
                "role": "citation and statute mention validation",
            },
            {
                "id": "ragas_context_precision",
                "role": "context sufficiency scoring",
            },
            {
                "id": "guardrails_grounded_generation",
                "role": "confidence cap for weak or unsupported evidence",
            },
        ],
        "preview": plain_text_preview(_prediction_text(prediction))[:220],
    }


def apply_evidence_quality_guard(prediction: dict, evidence_quality: dict) -> dict:
    guarded = dict(prediction)
    guarded["evidence_quality"] = evidence_quality
    max_confidence = _safe_float(evidence_quality.get("max_recommended_confidence"))
    if max_confidence > 0:
        guarded["confidence"] = min(_safe_float(guarded.get("confidence")), max_confidence)
        guarded["confidence_percent"] = int(round(guarded["confidence"] * 100))
    warnings = _dedupe((guarded.get("reliability_warnings") or []) + (evidence_quality.get("warnings") or []), limit=8)
    guarded["reliability_warnings"] = warnings
    if evidence_quality.get("status") in {"thin", "insufficient"}:
        guarded["evidence_status"] = "insufficient"
    return guarded
