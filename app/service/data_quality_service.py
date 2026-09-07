from __future__ import annotations

from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text
from app.service.module_service import normalize_module


def _safe_int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _fetch_scalar(sql: str, params: dict | None = None) -> int:
    try:
        with engine.connect() as conn:
            value = conn.execute(text(sql), params or {}).scalar()
            return _safe_int(value)
    except Exception:
        return 0


def get_source_quality_snapshot() -> dict:
    canlii_total = _fetch_scalar("SELECT COUNT(*) FROM source_items WHERE source_code = 'canlii'")
    fetch_mode_expr = (
        "COALESCE(json_extract(raw_json, '$.fetch_mode'), '')"
        if is_sqlite()
        else "COALESCE(raw_json->>'fetch_mode', '')"
    )
    canlii_api_metadata = _fetch_scalar(
        f"""
        SELECT COUNT(*)
        FROM source_items
        WHERE source_code = 'canlii'
          AND {fetch_mode_expr} = 'api_case_metadata'
        """
    )
    canlii_rss = _fetch_scalar(
        f"""
        SELECT COUNT(*)
        FROM source_items
        WHERE source_code = 'canlii'
          AND {fetch_mode_expr} IN ('sync', 'search_hydration')
        """
    )
    canlii_full_text_like = _fetch_scalar(
        """
        SELECT COUNT(*)
        FROM source_items
        WHERE source_code = 'canlii'
          AND LENGTH(COALESCE(raw_text, '')) >= 3000
        """
    )
    legislation_total = _fetch_scalar(
        """
        SELECT COUNT(*)
        FROM source_items
        WHERE source_code IN (
            'ca_federal_act', 'ca_federal_regulation', 'on_statute', 'on_regulation',
            'manual_canada_rule', 'url_canada_rule'
        )
        """
    )
    relation_total = _fetch_scalar("SELECT COUNT(*) FROM canada_case_law_links")
    law_total = _fetch_scalar("SELECT COUNT(*) FROM canada_laws")
    ofac_total = _fetch_scalar("SELECT COUNT(*) FROM source_items WHERE source_code = 'ofac'")
    total_items = _fetch_scalar("SELECT COUNT(*) FROM source_items")

    return {
        "total_items": total_items,
        "canlii_total": canlii_total,
        "canlii_api_metadata": canlii_api_metadata,
        "canlii_rss_or_hydrated": canlii_rss,
        "canlii_full_text_like": canlii_full_text_like,
        "legislation_total": legislation_total,
        "canada_law_total": law_total,
        "case_law_relation_total": relation_total,
        "ofac_total": ofac_total,
    }


def _case_rows_from_packet(module_packet: dict) -> int:
    packet = module_packet or {}
    case_keys = set()
    for case in packet.get("case_law_rows") or []:
        key = _safe_int(case.get("case_id")) or repair_text(case.get("title")).lower()
        if key:
            case_keys.add(key)
    for law in packet.get("relevant_laws") or []:
        for case in law.get("related_cases") or []:
            key = _safe_int(case.get("case_id")) or repair_text(case.get("title")).lower()
            if key:
                case_keys.add(key)
        for column in law.get("case_columns") or []:
            for case in column.get("items") or []:
                key = _safe_int(case.get("case_id")) or repair_text(case.get("title")).lower()
                if key:
                    case_keys.add(key)
    return len(case_keys)


def build_data_readiness(
    *,
    module: str,
    module_packet: dict | None = None,
    retrieval_summary: dict | None = None,
    rag_context: dict | None = None,
    local_result_total: int = 0,
) -> dict:
    normalized_module = normalize_module(module)
    snapshot = get_source_quality_snapshot()
    retrieval_summary = retrieval_summary or {}
    module_packet = module_packet or {}
    rag_context = rag_context or {}

    evidence_law_count = _safe_int(retrieval_summary.get("law_count")) or len(module_packet.get("relevant_laws") or [])
    evidence_case_count = _safe_int(retrieval_summary.get("case_count")) or _case_rows_from_packet(module_packet)
    rag_items = rag_context.get("items") or []
    rag_law_count = len([item for item in rag_items if item.get("source_kind") == "law"])
    rag_case_count = len([item for item in rag_items if item.get("source_kind") == "case"])
    evidence_law_count = max(evidence_law_count, rag_law_count)
    evidence_case_count = max(evidence_case_count, rag_case_count)
    local_result_total = _safe_int(local_result_total)

    missing = []
    actions = []
    warnings = []
    status = "ready"
    max_confidence = 0.78

    if normalized_module == "canada":
        if snapshot["canlii_total"] < 1000:
            missing.append("canlii_case_metadata")
            actions.append("Set CANLII_API_KEY and run `python canlii_ingest.py api-metadata` to import authorized case metadata.")
        if snapshot["canlii_full_text_like"] < 50:
            missing.append("case_text_or_authorized_summaries")
            actions.append("Import authorized case summaries/full text from a licensed source before relying on outcome predictions.")
        if snapshot["case_law_relation_total"] < 500:
            missing.append("case_law_relations")
            actions.append("Build more law-to-case relations after metadata import so results can cite why a case is relevant.")
        if evidence_case_count <= 0:
            missing.append("query_matched_cases")
            actions.append("Broaden or translate keywords, then rerun retrieval; do not ask the model for a legal conclusion with zero matched cases.")
        if evidence_law_count <= 0:
            missing.append("query_matched_laws")
            actions.append("Import or map governing statutes/regulations for the jurisdiction before prediction.")

        if missing:
            status = "insufficient" if "query_matched_cases" in missing else "partial"
            max_confidence = 0.25 if status == "insufficient" else 0.45
            warnings.append("Current Canadian-law data is not enough for a reliable legal outcome prediction.")
    elif normalized_module == "us_sanctions":
        if snapshot["ofac_total"] < 100:
            status = "insufficient"
            max_confidence = 0.25
            missing.append("ofac_snapshot")
            actions.append("Run the OFAC snapshot import before asking the model for sanctions analysis.")
        if local_result_total <= 0:
            missing.append("query_matched_ofac_records")
            actions.append("Use exact company names, aliases, and program names to improve local OFAC matching.")
    else:
        status = "partial"
        max_confidence = 0.35
        missing.append("module_data_profile")

    return {
        "status": status,
        "can_support_prediction": status == "ready",
        "can_support_research_summary": status in {"ready", "partial"},
        "max_recommended_confidence": max_confidence,
        "local_result_total": local_result_total,
        "evidence": {
            "matched_laws": evidence_law_count,
            "matched_cases": evidence_case_count,
            "retrieval_keywords": retrieval_summary.get("keywords") or [],
            "rag_law_chunks": rag_law_count,
            "rag_case_chunks": rag_case_count,
        },
        "corpus": snapshot,
        "missing": missing,
        "warnings": warnings,
        "recommended_actions": actions[:6],
        "field_contract": {
            "reliable_fields": [
                "input_text",
                "analysis.facts",
                "analysis.disputed_issues",
                "retrieval_keywords",
                "data_readiness",
                "module_packet.relevant_laws",
                "module_packet.case_law_rows",
            ],
            "guarded_fields": [
                "prediction.predicted_outcome",
                "prediction.likely_prevailing_party",
                "prediction.confidence",
                "prediction.reasoning",
            ],
            "rule": "Guarded fields must be treated as unreliable unless data_readiness.status is ready.",
        },
    }
