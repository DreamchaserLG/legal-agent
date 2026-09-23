from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text
from app.service.module_service import normalize_module
from app.service.skill_runtime_service import run_legal_skill


COLLABORATION_VERSION = "ca-skill-collaboration-1.0"
COMPLEX_INTENTS = {"research", "risk_analysis", "hearing_assessment"}


class SkillCollaborationRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20000)
    intent: Literal["auto", "research", "risk_analysis", "hearing_assessment", "single_question"] = "auto"
    module: str = "canada"
    filters: dict[str, str] = Field(default_factory=dict)
    case_acl: list[str] = Field(default_factory=list, max_length=500)
    max_skills: int = Field(default=6, ge=2, le=8)
    timeout_seconds: int = Field(default=25, ge=3, le=90)


class CollaborationStep(BaseModel):
    agent: str
    skill_name: str
    status: Literal["ok", "blocked", "skipped", "timeout"]
    reason: str
    run_id: int | None = None
    duration_ms: int = 0


class SkillCollaborationState(BaseModel):
    query: str
    intent: str
    module: str
    filters: dict[str, str]
    tenant_id: str
    case_acl: list[str]
    max_skills: int
    timeout_seconds: int
    started_at: float = Field(default_factory=time.monotonic)
    steps: list[CollaborationStep] = Field(default_factory=list)
    facts: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    authority_links: list[dict[str, Any]] = Field(default_factory=list)
    citation_verification: dict[str, Any] = Field(default_factory=dict)
    risks: dict[str, Any] = Field(default_factory=dict)
    blocked_reasons: list[str] = Field(default_factory=list)
    stop_reason: str = ""


def ensure_skill_collaboration_tables() -> None:
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS skill_collaboration_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                collaboration_version TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id INTEGER,
                intent TEXT NOT NULL,
                query_text TEXT NOT NULL,
                status TEXT NOT NULL,
                state_json TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_skill_collaboration_tenant_created ON skill_collaboration_runs (tenant_id, created_at DESC)",
        ]
    else:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS skill_collaboration_runs (
                id BIGSERIAL PRIMARY KEY,
                collaboration_version TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                user_id BIGINT NULL,
                intent TEXT NOT NULL,
                query_text TEXT NOT NULL,
                status TEXT NOT NULL,
                state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_skill_collaboration_tenant_created ON skill_collaboration_runs (tenant_id, created_at DESC)",
        ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _safe_filters(filters: dict[str, str]) -> dict[str, str]:
    allowed = {"jurisdiction", "document_type", "court_level", "language", "date_from", "date_to"}
    return {key: repair_text(value) for key, value in filters.items() if key in allowed and repair_text(value)}


def _classify_intent(request: SkillCollaborationRequest) -> str:
    if request.intent != "auto":
        return request.intent
    query = repair_text(request.query).lower()
    if any(term in query for term in ("庭审", "hearing", "举证", "质证")):
        return "hearing_assessment"
    if any(term in query for term in ("风险", "risk", "时效", "管辖", "执行")):
        return "risk_analysis"
    if len(query) > 100 or any(term in query for term in ("研究", "research", "案例", "法条")):
        return "research"
    return "single_question"


def _timed_out(state: SkillCollaborationState) -> bool:
    return time.monotonic() - state.started_at >= state.timeout_seconds


def _allowed(item: dict[str, Any], case_acl: list[str]) -> bool:
    if not case_acl:
        return True
    allowed = {repair_text(value) for value in case_acl if repair_text(value)}
    candidates = {
        repair_text(item.get("doc_id")),
        repair_text(item.get("source_uid")),
        f"{repair_text(item.get('source_table'))}:{item.get('source_id')}",
        f"{repair_text(item.get('source_table'))}:{item.get('source_id')}:{item.get('chunk_id')}",
    }
    return bool(allowed.intersection(candidates))


def _evidence_key(item: dict[str, Any]) -> str:
    return repair_text(item.get("doc_id")) or f"{repair_text(item.get('source_table'))}:{item.get('source_id')}:{item.get('chunk_id')}"


def _merge_evidence(*collections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for collection in collections:
        for item in collection:
            key = _evidence_key(item)
            if key and key not in merged:
                merged[key] = dict(item)
    return list(merged.values())


def _run_skill(state: SkillCollaborationState, agent: str, skill_name: str, reason: str) -> dict[str, Any] | None:
    if len(state.steps) >= state.max_skills:
        state.stop_reason = "达到最大 Skill 数，停止继续协作。"
        state.steps.append(CollaborationStep(agent=agent, skill_name=skill_name, status="blocked", reason=state.stop_reason))
        return None
    if _timed_out(state):
        state.stop_reason = "达到协作超时预算，停止继续协作。"
        state.steps.append(CollaborationStep(agent=agent, skill_name=skill_name, status="timeout", reason=state.stop_reason))
        return None
    started = time.monotonic()
    result = run_legal_skill(skill_name, state.query, filters=state.filters, limit=8)
    status = "blocked" if result.get("status") == "blocked" else "ok"
    state.steps.append(
        CollaborationStep(
            agent=agent,
            skill_name=skill_name,
            status=status,
            reason=reason,
            run_id=result.get("run_id"),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
    )
    if status == "blocked":
        blocked_reason = repair_text(result.get("reason")) or f"{skill_name} 被安全策略阻断。"
        state.blocked_reasons.append(blocked_reason)
        state.stop_reason = blocked_reason
        return None
    return result


def _persist(state: SkillCollaborationState, user_id: int | None, status: str) -> int | None:
    try:
        ensure_skill_collaboration_tables()
        payload = state.model_dump(mode="json")
        duration_ms = int((time.monotonic() - state.started_at) * 1000)
        params = {
            "version": COLLABORATION_VERSION,
            "tenant": state.tenant_id,
            "user_id": user_id,
            "intent": state.intent,
            "query": state.query,
            "status": status,
            "state": json.dumps(payload, ensure_ascii=False),
            "duration": duration_ms,
        }
        with engine.begin() as conn:
            if is_sqlite():
                row = conn.execute(text("""INSERT INTO skill_collaboration_runs (collaboration_version, tenant_id, user_id, intent, query_text, status, state_json, duration_ms) VALUES (:version,:tenant,:user_id,:intent,:query,:status,:state,:duration) RETURNING id"""), params).mappings().first()
            else:
                row = conn.execute(text("""INSERT INTO skill_collaboration_runs (collaboration_version, tenant_id, user_id, intent, query_text, status, state_json, duration_ms) VALUES (:version,:tenant,:user_id,:intent,:query,:status,CAST(:state AS jsonb),:duration) RETURNING id"""), params).mappings().first()
        return int(row["id"]) if row else None
    except Exception:
        return None


def run_skill_collaboration(request: SkillCollaborationRequest, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    if not settings.agent_collaboration_enabled:
        return {
            "status": "disabled",
            "reason": "Skill 协作功能已由配置关闭。",
            "review_required": True,
        }
    state = SkillCollaborationState(
        query=repair_text(request.query),
        intent=_classify_intent(request),
        module=normalize_module(request.module),
        filters=_safe_filters(request.filters),
        tenant_id=repair_text(tenant_id) or "public",
        case_acl=[repair_text(value) for value in request.case_acl if repair_text(value)],
        max_skills=request.max_skills,
        timeout_seconds=request.timeout_seconds,
    )

    intake = _run_skill(state, "intake_agent", "issue_fact_extraction", "先识别法域、日期、法院和事实缺口。")
    if intake:
        state.facts = (intake.get("result") or {}).get("intake") or {}
        if state.facts.get("missing_facts"):
            state.blocked_reasons.append("存在待补充事实：" + "；".join(state.facts["missing_facts"]))

    if not state.stop_reason:
        case_result = _run_skill(state, "case_retrieval_agent", "case_retrieval_ca", "检索与争点相关的案例候选。")
        law_result = _run_skill(state, "statute_retrieval_agent", "statute_retrieval_ca", "检索适用法规候选。")
        case_evidence = ((case_result or {}).get("result") or {}).get("evidence") or []
        law_evidence = ((law_result or {}).get("result") or {}).get("evidence") or []
        state.evidence = [item for item in _merge_evidence(case_evidence, law_evidence) if _allowed(item, state.case_acl)]
        if not state.evidence:
            state.blocked_reasons.append("没有检索到通过案件 ACL 的可核验证据。")

    if state.evidence and not state.stop_reason:
        authority = _run_skill(state, "authority_agent", "authority_linking", "案例和法规均有候选，查询正式关联。")
        if authority:
            state.authority_links = ((authority.get("result") or {}).get("formal_relations") or [])
        verification = _run_skill(state, "citation_agent", "evidence_verification", "核验候选资料的引用与来源状态。")
        if verification:
            state.citation_verification = (verification.get("result") or {})

    if state.intent in COMPLEX_INTENTS and state.evidence and not state.stop_reason:
        risk = _run_skill(state, "risk_agent", "risk_assessment_ca", "复杂研究需要输出受证据约束的风险复核草稿。")
        if risk:
            state.risks = risk.get("result") or {}

    if not state.stop_reason:
        state.stop_reason = "已完成允许的 Skill 协作链。"
    status = "blocked" if not state.evidence else "ok"
    run_id = _persist(state, user_id, status)
    return {
        "collaboration_version": COLLABORATION_VERSION,
        "run_id": run_id,
        "status": status,
        "intent": state.intent,
        "tenant_id": state.tenant_id,
        "case_acl_enforced": bool(state.case_acl),
        "facts": state.facts,
        "evidence": state.evidence,
        "authority_links": state.authority_links,
        "citation_verification": state.citation_verification,
        "risks": state.risks,
        "steps": [step.model_dump() for step in state.steps],
        "blocked_reasons": state.blocked_reasons,
        "stop_reason": state.stop_reason,
        "review_required": True,
        "disclaimer": "Skill 协作结果仅用于法律研究辅助，必须由具备相应法域资格的律师复核，不构成法律意见。",
    }
