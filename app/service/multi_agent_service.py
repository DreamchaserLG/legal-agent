from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.module_service import normalize_module


GRAPH_VERSION = "ca-legal-graph-1.0"
GRAPH_ROUTING = {
    "simple": ["single_rag_agent", "rule_engine"],
    "complex": ["query_agent", "retrieval_agent", "authority_agent", "verification_agent", "risk_agent", "arbitration_agent"],
}
SIMPLE_INTENTS = {"statute_lookup", "case_lookup", "single_question"}
COMPLEX_INTENTS = {"risk_analysis", "hearing_simulation", "cross_jurisdiction", "complex_research"}
AGENT_TOOL_ALLOWLIST = {
    "single_rag_agent": {"query_understanding", "hybrid_search", "citation_verification"},
    "query_agent": {"query_understanding"},
    "retrieval_agent": {"hybrid_search"},
    "authority_agent": {"authority_links"},
    "verification_agent": {"citation_verification"},
    "risk_agent": {"risk_annotation"},
    "arbitration_agent": {"rule_arbitration"},
    # 庭审模拟沿用现有工具语义，不赋予任意数据库或网络权限。
    "hearing_orchestrator": {"hearing_state"},
    "judge_agent": {"issue_fact_extraction", "evidence_verification"},
    "opposing_counsel_agent": {"authority_links", "evidence_verification"},
    "evidence_agent": {"evidence_verification", "authority_links"},
    "evaluation_agent": {"risk_annotation", "citation_verification"},
    "report_agent": {"rule_arbitration", "citation_verification"},
}


class MultiAgentRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20000)
    intent: Literal[
        "auto", "statute_lookup", "case_lookup", "single_question", "risk_analysis",
        "hearing_simulation", "cross_jurisdiction", "complex_research",
    ] = "auto"
    module: str = "canada"
    filters: dict[str, str] = Field(default_factory=dict)
    case_acl: list[str] = Field(default_factory=list, max_length=500)
    max_steps: int = Field(default=8, ge=2, le=12)
    timeout_seconds: int = Field(default=25, ge=3, le=90)
    token_budget: int = Field(default=6000, ge=500, le=20000)


class AgentStep(BaseModel):
    agent: str
    status: Literal["ok", "blocked", "skipped", "timeout"]
    allowed_tools: list[str]
    duration_ms: int
    notes: list[str] = Field(default_factory=list)


class LegalGraphState(BaseModel):
    query: str
    intent: str
    module: str
    filters: dict[str, str]
    tenant_id: str
    case_acl: list[str]
    max_steps: int
    timeout_seconds: int
    token_budget: int
    started_at: float = Field(default_factory=time.monotonic)
    steps: list[AgentStep] = Field(default_factory=list)
    query_plan: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    authority_links: list[dict[str, Any]] = Field(default_factory=list)
    citation_verification: list[dict[str, Any]] = Field(default_factory=list)
    risks: list[dict[str, Any]] = Field(default_factory=list)
    blocked_reasons: list[str] = Field(default_factory=list)
    review_required: bool = True


def ensure_multi_agent_tables() -> None:
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS multi_agent_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                graph_version TEXT NOT NULL,
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
            "CREATE INDEX IF NOT EXISTS idx_multi_agent_runs_tenant_created ON multi_agent_runs (tenant_id, created_at DESC)",
        ]
    else:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS multi_agent_runs (
                id BIGSERIAL PRIMARY KEY,
                graph_version TEXT NOT NULL,
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
            "CREATE INDEX IF NOT EXISTS idx_multi_agent_runs_tenant_created ON multi_agent_runs (tenant_id, created_at DESC)",
        ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _safe_filters(filters: dict[str, str]) -> dict[str, str]:
    allowed = {"jurisdiction", "document_type", "court_level", "language", "date_from", "date_to"}
    return {key: repair_text(value) for key, value in filters.items() if key in allowed and repair_text(value)}


def _redact_sensitive_text(value: Any) -> str:
    """Prevent common direct identifiers from being returned in evidence excerpts."""
    text_value = repair_text(value)
    text_value = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[redacted-email]", text_value, flags=re.IGNORECASE)
    return re.sub(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)", "[redacted-phone]", text_value)


def _acl_allows(item: dict[str, Any], case_acl: list[str]) -> bool:
    """An empty ACL means the request is limited to the shared public legal corpus."""
    if not case_acl:
        return True
    allowed = {repair_text(value) for value in case_acl}
    candidates = {
        repair_text(item.get("doc_id")),
        repair_text(item.get("source_uid")),
        f"{repair_text(item.get('source_table'))}:{item.get('source_id')}",
    }
    return bool(allowed.intersection(candidates))


def _classify_intent(request: MultiAgentRequest) -> str:
    if request.intent != "auto":
        return request.intent
    query = repair_text(request.query).lower()
    if any(term in query for term in ("风险", "risk", "胜诉", "liability", "执行", "时效", "jurisdiction")):
        return "risk_analysis"
    if any(term in query for term in ("庭审", "hearing", "cross-border", "跨法域", "美国", "eu", "欧盟")):
        return "cross_jurisdiction" if any(term in query for term in ("cross-border", "跨法域", "美国", "eu", "欧盟")) else "hearing_simulation"
    if any(term in query for term in ("section", "s.", "r.s.c", "r.s.o", "法条", "法规")):
        return "statute_lookup"
    return "single_question"


def assert_agent_tool(agent: str, tool: str) -> None:
    if tool not in AGENT_TOOL_ALLOWLIST.get(agent, set()):
        raise PermissionError(f"{agent} is not permitted to call {tool}")


def _assert_tool(agent: str, tool: str) -> None:
    """Backward-compatible internal alias for existing multi-agent nodes."""
    assert_agent_tool(agent, tool)


def _step(state: LegalGraphState, agent: str, started: float, status: str = "ok", notes: list[str] | None = None) -> None:
    state.steps.append(
        AgentStep(
            agent=agent,
            status=status,
            allowed_tools=sorted(AGENT_TOOL_ALLOWLIST.get(agent, set())),
            duration_ms=int((time.monotonic() - started) * 1000),
            notes=notes or [],
        )
    )


def _time_exceeded(state: LegalGraphState) -> bool:
    return time.monotonic() - state.started_at >= state.timeout_seconds


def _query_agent(state: LegalGraphState, agent: str = "query_agent", record_step: bool = True) -> None:
    started = time.monotonic()
    _assert_tool(agent, "query_understanding")
    query = state.query.lower()
    jurisdictions = []
    if any(term in query for term in ("ontario", "安大略", "onca", "onsc")):
        jurisdictions.append("Ontario")
    if any(term in query for term in ("canada", "canadian", "加拿大", "scc")):
        jurisdictions.append("Canada")
    if not jurisdictions:
        jurisdictions.append("Canada")
    issue_terms = re.findall(r"[A-Za-z][A-Za-z0-9'/-]{3,}", state.query)
    dates = re.findall(r"\b(?:19|20)\d{2}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?\b", state.query)
    state.query_plan = {
        "intent": state.intent,
        "jurisdictions": jurisdictions,
        "issue_terms": issue_terms[:16],
        "dates": dates[:8],
        "limitation_review_required": state.intent in COMPLEX_INTENTS or not dates,
        "cross_jurisdiction_requested": state.intent == "cross_jurisdiction",
    }
    if state.intent == "cross_jurisdiction":
        state.blocked_reasons.append("当前部署仅启用加拿大本地知识库；跨法域材料必须接入对应法域的受控知识库后再比较。")
    if record_step:
        _step(state, agent, started)


def _evidence_item(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") or {}
    return {
        "doc_id": f"{item.get('source_table')}:{item.get('source_id')}:{item.get('chunk_id')}",
        "source_uid": repair_text(item.get("source_uid")),
        "source_table": repair_text(item.get("source_table")),
        "source_id": item.get("source_id"),
        "chunk_id": item.get("chunk_id"),
        "title": repair_text(item.get("title")),
        "citation": repair_text(item.get("citation") or metadata.get("citation")),
        "article_id": repair_text(metadata.get("article_no") or metadata.get("article_id")),
        "court": repair_text(item.get("court_level") or metadata.get("court")),
        "decision_date": repair_text(item.get("published_at")),
        "authority_status": repair_text(metadata.get("authority_status") or metadata.get("in_force_status") or "unknown"),
        "source_url": repair_text(item.get("source_url")),
        "excerpt": _redact_sensitive_text(item.get("excerpt")),
        "score": float(item.get("score") or 0),
        "vector_score": float(item.get("vector_score") or 0),
        "lexical_score": float(item.get("lexical_score") or 0),
        "source_kind": repair_text(item.get("source_kind")),
    }


def _retrieval_agent(state: LegalGraphState, agent: str = "retrieval_agent", record_step: bool = True) -> None:
    started = time.monotonic()
    _assert_tool(agent, "hybrid_search")
    if _time_exceeded(state):
        if record_step:
            _step(state, agent, started, "timeout", ["全局超时预算已耗尽。"])
        state.blocked_reasons.append("检索超时，未生成结论。")
        return
    result = hybrid_search(
        state.query,
        module=state.module,
        source_filter="canada",
        limit=min(12 if state.intent in COMPLEX_INTENTS else 8, max(4, state.token_budget // 600)),
        filters=state.filters,
    )
    candidates = [_evidence_item(item) for item in result.get("items") or []]
    state.evidence = [item for item in candidates if _acl_allows(item, state.case_acl)]
    filtered_count = len(candidates) - len(state.evidence)
    if filtered_count:
        state.query_plan["acl_filtered_count"] = filtered_count
    if not state.evidence:
        state.blocked_reasons.append("没有检索到可核验的本地证据。")
    if record_step:
        _step(state, agent, started, notes=[f"retrieved={len(state.evidence)}", f"acl_filtered={filtered_count}", result.get("strategy", "")])


def _authority_agent(state: LegalGraphState) -> None:
    started = time.monotonic()
    _assert_tool("authority_agent", "authority_links")
    source_ids = sorted({int(item["source_id"]) for item in state.evidence if item.get("source_table") == "source_items" and item.get("source_id")})
    if not source_ids:
        _step(state, "authority_agent", started, "skipped", ["没有可用于正式关联查询的案例来源。"])
        return
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT cl.case_item_id, cl.law_id, cl.matched_alias, cl.match_source,
                       cl.match_score, cl.evidence_excerpt, l.title AS law_title,
                       l.citation AS law_citation
                FROM canada_case_law_links cl
                JOIN canada_laws l ON l.id = cl.law_id
                WHERE cl.case_item_id = ANY(:case_ids)
                ORDER BY cl.match_score DESC
                LIMIT 48
                """
            ),
            {"case_ids": source_ids},
        ).mappings().all()
    state.authority_links = [
        {
            "case_source_id": int(row["case_item_id"]),
            "law_id": int(row["law_id"]),
            "law_title": repair_text(row["law_title"]),
            "law_citation": repair_text(row["law_citation"]),
            "matched_alias": repair_text(row["matched_alias"]),
            "match_source": repair_text(row["match_source"]),
            "confidence": float(row["match_score"] or 0),
            "evidence_excerpt": repair_text(row["evidence_excerpt"]),
        }
        for row in rows
    ]
    _step(state, "authority_agent", started, notes=[f"formal_links={len(state.authority_links)}"])


def _verification_agent(state: LegalGraphState, agent: str = "verification_agent", record_step: bool = True) -> None:
    started = time.monotonic()
    _assert_tool(agent, "citation_verification")
    verification = []
    for item in state.evidence:
        citation = repair_text(item.get("citation"))
        verification.append(
            {
                "doc_id": item["doc_id"],
                "citation_present": bool(citation),
                "source_url_present": bool(item.get("source_url")),
                "authority_status": item["authority_status"],
                "decision_date": item["decision_date"],
                "verdict": "verified_metadata" if citation or item.get("source_url") else "needs_manual_citation_check",
            }
        )
    state.citation_verification = verification
    if verification and not any(row["verdict"] == "verified_metadata" for row in verification):
        state.blocked_reasons.append("检索证据缺少可核验引用或来源 URL。")
    if record_step:
        _step(state, agent, started, notes=[f"verified={sum(x['verdict'] == 'verified_metadata' for x in verification)}"])


def _risk_binding(state: LegalGraphState, category: str, statement: str, suggestions: list[str]) -> dict[str, Any]:
    laws = [link for link in state.authority_links[:3] if link.get("law_id")]
    if not laws:
        laws = [
            {
                "law_id": item["doc_id"],
                "law_title": item["title"],
                "law_citation": item["citation"],
                "confidence": item["score"],
            }
            for item in state.evidence
            if item.get("source_kind") == "law"
        ][:3]
    cases = [item for item in state.evidence if item.get("source_kind") == "case"][:3]
    evidence_count = len(laws) + len(cases)
    confidence = min(0.75, 0.2 + min(evidence_count, 6) * 0.08)
    if state.query_plan.get("limitation_review_required") and category == "procedural":
        confidence = min(confidence, 0.55)
    return {
        "category": category,
        "assessment": statement,
        "confidence": round(confidence, 2),
        "legal_bases": [{"law_id": row["law_id"], "title": row["law_title"], "citation": row["law_citation"], "link_confidence": row["confidence"]} for row in laws],
        "similar_cases": [{"doc_id": row["doc_id"], "title": row["title"], "citation": row["citation"], "court": row["court"], "decision_date": row["decision_date"], "score": row["score"]} for row in cases],
        "suggested_materials_or_actions": suggestions,
        "binding_status": "bound" if laws and cases else "insufficient_evidence",
        "review_required": True,
    }


def _risk_agent(state: LegalGraphState) -> None:
    started = time.monotonic()
    _assert_tool("risk_agent", "risk_annotation")
    missing_dates = not state.query_plan.get("dates")
    state.risks = [
        _risk_binding(state, "实体风险", "需核实请求权基础、构成要件与法律关系竞合；现有材料不足以确认实体结论。", ["补充合同、通知、沟通记录及争点时间线。", "逐项映射请求权构成要件与反驳事实。"]),
        _risk_binding(state, "程序风险", "需核验管辖、主体资格、时效及举证期限；系统不根据不完整日期推断期限结论。", ["补充起诉/送达/知悉损害等关键日期。", "确认法院层级、适用法与当事人主体资格。"]),
        _risk_binding(state, "证据风险", "需核验证据的真实性、关联性、合法性、证明力和证据链完整性。", ["提供原始文件、元数据、证人来源及保全记录。", "建立待证事实到证据的对应表并标记缺口。"]),
        _risk_binding(state, "执行风险", "需评估被告履行能力、资产线索、保全条件和跨区域执行障碍。", ["补充主体登记、资产线索、保险及既有执行信息。", "在律师建议下评估保全和执行路径。"]),
    ]
    if missing_dates:
        state.risks[1]["suggested_materials_or_actions"].insert(0, "优先补充时效起算与中断/中止事实。")
    _step(state, "risk_agent", started, notes=["风险仅为证据绑定的复核草稿，不构成预测结论。"])


def _arbitration_agent(state: LegalGraphState, record_step: bool = True) -> None:
    started = time.monotonic()
    _assert_tool("arbitration_agent", "rule_arbitration")
    if len(state.steps) > state.max_steps:
        state.blocked_reasons.append("超过最大 Agent 步数，已停止继续推理。")
    if not state.evidence:
        state.blocked_reasons.append("规则引擎禁止在无证据时输出实体结论。")
    if state.intent == "cross_jurisdiction":
        state.blocked_reasons.append("规则优先：未接入目标法域知识库时不得进行跨法域比较结论。")
    if any(row["verdict"] == "needs_manual_citation_check" for row in state.citation_verification):
        state.review_required = True
    if record_step:
        _step(state, "arbitration_agent", started, notes=["规则引擎优先于 Agent 票选。"])


def _persist(state: LegalGraphState, user_id: int | None, status: str) -> int | None:
    try:
        ensure_multi_agent_tables()
        payload = state.model_dump(mode="json")
        duration_ms = int((time.monotonic() - state.started_at) * 1000)
        with engine.begin() as conn:
            if is_sqlite():
                row = conn.execute(
                    text("""INSERT INTO multi_agent_runs (graph_version, tenant_id, user_id, intent, query_text, status, state_json, duration_ms) VALUES (:version,:tenant,:user_id,:intent,:query,:status,:state,:duration) RETURNING id"""),
                    {"version": GRAPH_VERSION, "tenant": state.tenant_id, "user_id": user_id, "intent": state.intent, "query": state.query, "status": status, "state": json.dumps(payload, ensure_ascii=False), "duration": duration_ms},
                ).mappings().first()
            else:
                row = conn.execute(
                    text("""INSERT INTO multi_agent_runs (graph_version, tenant_id, user_id, intent, query_text, status, state_json, duration_ms) VALUES (:version,:tenant,:user_id,:intent,:query,:status,CAST(:state AS jsonb),:duration) RETURNING id"""),
                    {"version": GRAPH_VERSION, "tenant": state.tenant_id, "user_id": user_id, "intent": state.intent, "query": state.query, "status": status, "state": json.dumps(payload, ensure_ascii=False), "duration": duration_ms},
                ).mappings().first()
        return int(row["id"]) if row else None
    except Exception:
        return None


def run_multi_agent_analysis(request: MultiAgentRequest, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    clean_query = repair_text(request.query)
    if not clean_query:
        raise ValueError("query is required")
    intent = _classify_intent(request)
    state = LegalGraphState(
        query=clean_query,
        intent=intent,
        module=normalize_module(request.module),
        filters=_safe_filters(request.filters),
        tenant_id=repair_text(tenant_id) or "public",
        case_acl=[repair_text(value) for value in request.case_acl if repair_text(value)],
        max_steps=request.max_steps,
        timeout_seconds=request.timeout_seconds,
        token_budget=request.token_budget,
    )
    if intent in COMPLEX_INTENTS:
        _query_agent(state)
        _retrieval_agent(state)
        _authority_agent(state)
        _verification_agent(state)
        _risk_agent(state)
    else:
        started = time.monotonic()
        _query_agent(state, "single_rag_agent", record_step=False)
        _retrieval_agent(state, "single_rag_agent", record_step=False)
        _verification_agent(state, "single_rag_agent", record_step=False)
        _step(state, "single_rag_agent", started, notes=[f"evidence={len(state.evidence)}", "single-agent-rag"])
    _arbitration_agent(state, record_step=intent in COMPLEX_INTENTS)
    status = "blocked" if state.blocked_reasons else "ok"
    run_id = _persist(state, user_id, status)
    return {
        "graph_version": GRAPH_VERSION,
        "mode": "multi_agent" if intent in COMPLEX_INTENTS else "single_agent_rag",
        "intent": intent,
        "status": status,
        "run_id": run_id,
        "tenant_id": state.tenant_id,
        "case_acl_enforced": bool(state.case_acl),
        "tenant_scope": "tenant_audited_public_corpus",
        "graph_routing": GRAPH_ROUTING["complex" if intent in COMPLEX_INTENTS else "simple"],
        "query_plan": state.query_plan,
        "evidence": state.evidence,
        "authority_links": state.authority_links,
        "citation_verification": state.citation_verification,
        "risks": state.risks,
        "steps": [step.model_dump() for step in state.steps],
        "blocked_reasons": state.blocked_reasons,
        "review_required": True,
        "disclaimer": "系统输出仅用于法律研究辅助，必须由具备相应法域资格的律师复核，不构成法律意见。",
    }
