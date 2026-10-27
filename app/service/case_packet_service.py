from __future__ import annotations

import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from app.service.common_service import repair_text
from app.service.legal_query_planner_service import build_deep_query_plan


CASE_PACKET_VERSION = "case-packet-1.0"


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "|".join(repair_text(part).lower() for part in parts if repair_text(part))
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{digest}"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fact_item(fact: dict[str, Any], *, source_id: str, source_type: str) -> dict[str, Any]:
    content = repair_text(fact.get("content"))
    source_span = repair_text(fact.get("source_span"))
    return {
        "fact_id": _stable_id("F", source_id, fact.get("fact_type", "fact"), content, source_span),
        "fact_type": repair_text(fact.get("fact_type")) or "action",
        "content": content,
        "source_span": source_span,
        "source_id": source_id,
        "source_type": source_type,
        "status": "asserted",
        "confidence": round(float(fact.get("confidence") or 0.5), 3),
        "support_refs": [],
    }


def _issue_item(issue: dict[str, Any], *, source_id: str) -> dict[str, Any]:
    issue_text = repair_text(issue.get("issue"))
    return {
        "issue_id": _stable_id("I", source_id, issue_text),
        "description": issue_text,
        "legal_domain": repair_text(issue.get("legal_domain")),
        "status": "disputed_or_missing",
        "related_fact_ids": [],
        "source_id": source_id,
        "confidence": round(float(issue.get("issue_confidence") or 0.5), 3),
    }


def _dedupe(items: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        value = repair_text(item.get(key))
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(item)
    return result


def build_case_packet(case_text: str, *, use_llm: bool = True) -> dict[str, Any]:
    clean_text = repair_text(case_text)
    if not clean_text:
        raise ValueError("case_text is required")
    plan = build_deep_query_plan(clean_text, use_llm=use_llm, load_priors=True)
    plan_data = plan.model_dump(mode="json")
    asserted = [
        _fact_item(item, source_id="case_input", source_type="human_case_input")
        for item in plan_data.get("structured_facts") or []
        if repair_text(item.get("content"))
    ]
    issues = [
        _issue_item(item, source_id="case_input")
        for item in plan_data.get("dispute_issues") or []
        if repair_text(item.get("issue"))
    ]
    fact_lookup = {repair_text(item.get("content")): item["fact_id"] for item in asserted}
    for issue, raw_issue in zip(issues, plan_data.get("dispute_issues") or []):
        issue["related_fact_ids"] = [
            fact_lookup[text]
            for text in (raw_issue.get("supporting_facts") or [])
            if text in fact_lookup
        ]
    return {
        "schema_version": CASE_PACKET_VERSION,
        "revision": 1,
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
        "raw_case_text": clean_text,
        "asserted_facts": _dedupe(asserted, "fact_id"),
        "supported_facts": [],
        "disputed_or_missing_facts": _dedupe(issues, "issue_id"),
        "external_research": [],
        "legal_domains": list(plan_data.get("legal_domains") or []),
        "research_queries": {
            "rule_query": repair_text(plan_data.get("rule_query")),
            "case_query": repair_text(plan_data.get("case_query")),
            "keywords": list(plan_data.get("keywords") or []),
        },
        "planning_confidence": float(plan_data.get("planning_confidence") or 0),
        "warnings": list(plan_data.get("planning_warnings") or []),
    }


def merge_human_submission(
    packet: dict[str, Any],
    statement: str,
    *,
    submission_id: str,
    evidence_refs: list[str] | None = None,
) -> dict[str, Any]:
    clean_statement = repair_text(statement)
    if not clean_statement:
        return deepcopy(packet)
    updated = deepcopy(packet)
    plan = build_deep_query_plan(clean_statement, use_llm=False, load_priors=False).model_dump(mode="json")
    new_facts = [
        _fact_item(item, source_id=f"human_submission:{submission_id}", source_type="human_submission")
        for item in plan.get("structured_facts") or []
        if repair_text(item.get("content"))
    ]
    refs = [repair_text(value) for value in (evidence_refs or []) if repair_text(value)]
    for fact in new_facts:
        fact["support_refs"] = refs
        if refs:
            fact["status"] = "asserted_with_reference"
    new_issues = [
        _issue_item(item, source_id=f"human_submission:{submission_id}")
        for item in plan.get("dispute_issues") or []
        if repair_text(item.get("issue"))
    ]
    updated["asserted_facts"] = _dedupe(list(updated.get("asserted_facts") or []) + new_facts, "fact_id")
    updated["disputed_or_missing_facts"] = _dedupe(
        list(updated.get("disputed_or_missing_facts") or []) + new_issues,
        "issue_id",
    )
    updated["legal_domains"] = list(dict.fromkeys(
        list(updated.get("legal_domains") or []) + list(plan.get("legal_domains") or [])
    ))[:12]
    updated["revision"] = int(updated.get("revision") or 1) + 1
    updated["updated_at"] = _utc_now()
    return updated


def bind_external_research(packet: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    updated = deepcopy(packet)
    research = list(updated.get("external_research") or [])
    for item in evidence:
        evidence_id = repair_text(item.get("evidence_id") or item.get("doc_id"))
        if not evidence_id:
            continue
        research.append(
            {
                "evidence_id": evidence_id,
                "title": repair_text(item.get("title")),
                "source_kind": repair_text(item.get("source_kind")),
                "citation": repair_text(item.get("citation")),
                "source_url": repair_text(item.get("source_url")),
                "retrieved_at": repair_text(item.get("retrieved_at")),
                "content_hash": repair_text(item.get("content_hash")),
                "verification_status": repair_text(item.get("verification_status")) or "pending",
                "relation": "legal_context_or_analogous_fact_pattern",
            }
        )
    updated["external_research"] = _dedupe(research, "evidence_id")
    updated["revision"] = int(updated.get("revision") or 1) + 1
    updated["updated_at"] = _utc_now()
    return updated


def packet_reference_ids(packet: dict[str, Any]) -> dict[str, set[str]]:
    fact_ids = {
        repair_text(item.get("fact_id"))
        for group in ("asserted_facts", "supported_facts")
        for item in packet.get(group) or []
        if repair_text(item.get("fact_id"))
    }
    issue_ids = {
        repair_text(item.get("issue_id"))
        for item in packet.get("disputed_or_missing_facts") or []
        if repair_text(item.get("issue_id"))
    }
    evidence_ids = {
        repair_text(item.get("evidence_id"))
        for item in packet.get("external_research") or []
        if repair_text(item.get("evidence_id"))
    }
    return {"fact_ids": fact_ids, "issue_ids": issue_ids, "evidence_ids": evidence_ids}
