from __future__ import annotations

import json

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text
from app.service.deep_analysis_service import DeepAnalysis


class QualityGateResult(BaseModel):
    citation_traceability: float = Field(ge=0.0, le=1.0)
    rule_currency: float = Field(ge=0.0, le=1.0)
    evidence_completeness: float = Field(ge=0.0, le=1.0)
    low_confidence_ratio: float = Field(ge=0.0, le=1.0)
    manual_review_items: int
    passed: bool
    downgrade_reason: str | None = None
    gate_details: dict = Field(default_factory=dict)
    thresholds_used: dict = Field(default_factory=dict)


def _ratio(numerator: int, denominator: int, *, empty_value: float = 0.0) -> float:
    if denominator <= 0:
        return empty_value
    return round(max(0.0, min(numerator / denominator, 1.0)), 3)


def _thresholds() -> dict:
    return {
        "citation_traceability_min": float(getattr(settings, "quality_gate_citation_traceability_min", 0.7)),
        "rule_currency_min": float(getattr(settings, "quality_gate_rule_currency_min", 0.8)),
        "evidence_completeness_min": float(getattr(settings, "quality_gate_evidence_completeness_min", 0.6)),
        "low_confidence_ratio_max": float(getattr(settings, "quality_gate_low_confidence_ratio_max", 0.4)),
        "manual_review_max": int(getattr(settings, "quality_gate_manual_review_max", 10)),
    }


def ensure_quality_gate_tables() -> None:
    id_column = "INTEGER PRIMARY KEY AUTOINCREMENT" if is_sqlite() else "BIGSERIAL PRIMARY KEY"
    json_column = "TEXT" if is_sqlite() else "JSONB"
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                CREATE TABLE IF NOT EXISTS quality_gate_runs (
                    id {id_column},
                    tenant_id VARCHAR(120) NOT NULL DEFAULT '',
                    user_id BIGINT NULL,
                    input_summary TEXT NOT NULL DEFAULT '',
                    passed BOOLEAN NOT NULL DEFAULT FALSE,
                    downgrade_reason TEXT NULL,
                    metrics_json {json_column} NOT NULL,
                    thresholds_json {json_column} NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        conn.execute(text("CREATE INDEX IF NOT EXISTS idx_quality_gate_runs_created ON quality_gate_runs (created_at DESC)"))


def _audit_gate(
    result: QualityGateResult,
    *,
    input_summary: str,
    tenant_id: str,
    user_id: int | None,
) -> str:
    try:
        ensure_quality_gate_tables()
        metrics = {
            "citation_traceability": result.citation_traceability,
            "rule_currency": result.rule_currency,
            "evidence_completeness": result.evidence_completeness,
            "low_confidence_ratio": result.low_confidence_ratio,
            "manual_review_items": result.manual_review_items,
            "gate_details": result.gate_details,
        }
        metrics_json = json.dumps(metrics, ensure_ascii=False)
        thresholds_json = json.dumps(result.thresholds_used, ensure_ascii=False)
        if is_sqlite():
            sql = """
                INSERT INTO quality_gate_runs (
                    tenant_id, user_id, input_summary, passed, downgrade_reason,
                    metrics_json, thresholds_json
                ) VALUES (
                    :tenant_id, :user_id, :input_summary, :passed, :downgrade_reason,
                    :metrics_json, :thresholds_json
                )
            """
        else:
            sql = """
                INSERT INTO quality_gate_runs (
                    tenant_id, user_id, input_summary, passed, downgrade_reason,
                    metrics_json, thresholds_json
                ) VALUES (
                    :tenant_id, :user_id, :input_summary, :passed, :downgrade_reason,
                    CAST(:metrics_json AS jsonb), CAST(:thresholds_json AS jsonb)
                )
            """
        with engine.begin() as conn:
            conn.execute(
                text(sql),
                {
                    "tenant_id": repair_text(tenant_id),
                    "user_id": user_id,
                    "input_summary": repair_text(input_summary)[:1200],
                    "passed": result.passed,
                    "downgrade_reason": result.downgrade_reason,
                    "metrics_json": metrics_json,
                    "thresholds_json": thresholds_json,
                },
            )
        return "recorded"
    except (SQLAlchemyError, OSError, ValueError, TypeError):
        return "failed"


def evaluate_quality_gate(
    analysis: DeepAnalysis,
    *,
    input_summary: str = "",
    tenant_id: str = "",
    user_id: int | None = None,
    audit: bool = True,
) -> QualityGateResult:
    details = analysis.dispute_details
    risks = analysis.risk_predictions
    total_conclusions = len(details) + len(risks)
    traceable_details = sum(bool(item.legal_basis or item.case_support) for item in details)
    traceable_risks = sum(bool(item.supporting_rule_ids or item.supporting_case_ids) for item in risks)
    citation_traceability = _ratio(traceable_details + traceable_risks, total_conclusions)

    rules = [rule for detail in details for rule in detail.legal_basis]
    current_statuses = {"current", "in_force", "active", "relation_verified", "verified_relation"}
    current_rules = sum(
        repair_text(rule.get("effectiveness_status") or rule.get("citation_status")).lower() in current_statuses
        for rule in rules
    )
    rule_currency = _ratio(current_rules, len(rules), empty_value=0.0)

    evidence_supported = sum(
        bool(detail.legal_basis or detail.case_support)
        and float(detail.strength_breakdown.get("supporting_facts") or 0) > 0
        for detail in details
    )
    evidence_completeness = _ratio(evidence_supported, len(details))
    low_confidence_items = sum(item.strength_score < 0.5 for item in details) + sum(item.confidence < 0.5 for item in risks)
    low_confidence_ratio = _ratio(low_confidence_items, total_conclusions)
    manual_review_count = max(
        len(analysis.manual_review_items),
        sum(item.manual_review_required for item in risks),
    )

    thresholds = _thresholds()
    checks = [
        (citation_traceability >= thresholds["citation_traceability_min"], "引用可追溯率低于阈值"),
        (rule_currency >= thresholds["rule_currency_min"], "法规效力核验率低于阈值"),
        (evidence_completeness >= thresholds["evidence_completeness_min"], "争点证据完整度低于阈值"),
        (low_confidence_ratio <= thresholds["low_confidence_ratio_max"], "低置信度结论比例高于阈值"),
        (manual_review_count <= thresholds["manual_review_max"], "人工复核事项超过阈值"),
    ]
    downgrade_reason = next((reason for passed, reason in checks if not passed), None)
    result = QualityGateResult(
        citation_traceability=citation_traceability,
        rule_currency=rule_currency,
        evidence_completeness=evidence_completeness,
        low_confidence_ratio=low_confidence_ratio,
        manual_review_items=manual_review_count,
        passed=downgrade_reason is None,
        downgrade_reason=downgrade_reason,
        gate_details={
            "total_conclusions": total_conclusions,
            "traceable_conclusions": traceable_details + traceable_risks,
            "current_rules": current_rules,
            "total_rules": len(rules),
            "evidence_supported_issues": evidence_supported,
            "total_issues": len(details),
            "low_confidence_items": low_confidence_items,
        },
        thresholds_used=thresholds,
    )
    if audit:
        result.gate_details["audit_status"] = _audit_gate(
            result,
            input_summary=input_summary or analysis.case_summary,
            tenant_id=tenant_id,
            user_id=user_id,
        )
    else:
        result.gate_details["audit_status"] = "skipped"
    return result
