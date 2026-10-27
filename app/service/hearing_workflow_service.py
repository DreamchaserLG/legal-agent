from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.core.config import settings
from app.service.common_service import repair_text
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.llm_service import LLMServiceError, create_structured_response, is_llm_configured
from app.service.legal_safety_service import (
    build_role_instruction,
    enforce_grounded_legal_text,
    find_role_repetition,
)
from app.service.multi_agent_service import assert_agent_tool
from app.service.rag_service import rag_search


WORKFLOW_VERSION = "canadian-hearing-simulation-3.0"
HEARING_STAGES = [
    "Pre-Hearing Preparation",
    "Opening Statements",
    "Evidence Examination",
    "Legal Arguments",
    "Judicial Questions and Summary",
    "Record and Authority Review",
    "Final Training Report",
]
HEARING_ROLES = {"User Counsel", "Opposing Counsel", "Presiding Judge", "Court Clerk", "System"}
STAGE_FOCUS = {
    "Pre-Hearing Preparation": "Define the issues, exhibit list, procedural timeline, jurisdiction, and research scope.",
    "Opening Statements": "State the requested relief, material facts, theory of the case, and applicable burdens.",
    "Evidence Examination": "Examine authenticity, admissibility, relevance, weight, and the evidentiary foundation of each item.",
    "Legal Arguments": "Apply verified Canadian authorities to the facts and distinguish adverse authorities.",
    "Judicial Questions and Summary": "Answer neutral judicial questions and identify admitted, disputed, and unproven propositions.",
    "Record and Authority Review": "Audit the record, citation currency, authority hierarchy, and remaining verification gaps.",
    "Final Training Report": "Summarize completed stages, traceable sources, safety gates, and training feedback only.",
}

HEARING_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "speaker": {"type": "string", "enum": ["Opposing Counsel", "Presiding Judge"]},
        "content": {"type": "string"},
        "disputed_issue": {"type": "string"},
        "cited_evidence_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "legal_basis_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "pending_verifications": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "confidence": {"type": "number"},
    },
    "required": ["speaker", "content", "disputed_issue", "cited_evidence_ids", "legal_basis_ids", "pending_verifications", "confidence"],
    "additionalProperties": False,
}


class HearingRunCreate(BaseModel):
    case_summary: str = Field(min_length=10, max_length=20000)
    module: str = "canada"
    case_acl: list[str] = Field(default_factory=list, max_length=500)
    jurisdiction: str = ""

    @field_validator("case_summary", "jurisdiction")
    @classmethod
    def require_english_input(cls, value: str) -> str:
        if re.search(r"[\u3400-\u9fff]", value or ""):
            raise ValueError("The Canadian hearing workflow accepts English input only.")
        return value


class HearingTurnInput(BaseModel):
    content: str = Field(min_length=2, max_length=12000)
    action: Literal["continue", "complete_stage", "procedure_omission"] = "continue"
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    legal_basis_ids: list[str] = Field(default_factory=list, max_length=12)
    omission_basis: str = Field(default="", max_length=1200)

    @field_validator("content", "omission_basis")
    @classmethod
    def require_english_input(cls, value: str) -> str:
        if re.search(r"[\u3400-\u9fff]", value or ""):
            raise ValueError("The Canadian hearing workflow accepts English submissions only.")
        return value


class SimulatedReply(BaseModel):
    speaker: Literal["Opposing Counsel", "Presiding Judge"]
    content: str = Field(min_length=1, max_length=3000)
    disputed_issue: str = Field(default="", max_length=600)
    cited_evidence_ids: list[str] = Field(default_factory=list, max_length=3)
    legal_basis_ids: list[str] = Field(default_factory=list, max_length=3)
    pending_verifications: list[str] = Field(default_factory=list, max_length=3)
    confidence: float = Field(ge=0, le=1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stage_template(stage: str, evidence: list[dict[str, Any]], laws: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_ids = [item["doc_id"] for item in evidence[:3]]
    law_ids = [item["doc_id"] for item in laws[:3]]
    pending = [] if law_ids else ["No verifiable Canadian authority was retrieved. Refine the facts or research query; do not guess a citation."]
    return {
        "stage": stage,
        "status": "active" if stage == HEARING_STAGES[0] else "pending",
        "participants": ["User Counsel", "Opposing Counsel", "Presiding Judge"],
        "focus": STAGE_FOCUS[stage],
        "disputed_issues": [],
        "facts": [],
        "evidence_ids": evidence_ids,
        "evidence_assessments": [],
        "legal_basis_ids": law_ids,
        "opponent_defences": [],
        "our_responses": [],
        "confidence": {"evidence_sufficiency": 0.0, "legal_relevance": 0.0, "stage_readiness": 0.0},
        "state_delta": {"evidence_sufficiency": 0, "legal_relevance": 0, "fact_consistency": 0, "procedure_compliance": 0, "opposing_strength": 0, "case_support": 0},
        "risks": [],
        "pending_verifications": pending,
        "procedure_omission": None,
        "checkpoint_version": 0,
    }


def _normalize_legacy_state(state: dict[str, Any]) -> dict[str, Any]:
    """Upgrade earlier Chinese-keyed hearing records into the English Canadian workflow."""
    if state.get("workflow_version") == WORKFLOW_VERSION:
        return state
    legacy = state.get("stages") or {}
    evidence = state.get("evidence_catalog") or []
    laws = [item for item in evidence if item.get("source_kind") == "law"]
    stage_aliases = {
        "Pre-Hearing Preparation": ["Pre-Hearing Preparation", "庭前准备"],
        "Opening Statements": ["Opening Statements", "开庭陈述", "法庭调查"],
        "Evidence Examination": ["Evidence Examination", "举证质证"],
        "Legal Arguments": ["Legal Arguments", "法庭辩论"],
        "Judicial Questions and Summary": ["Judicial Questions and Summary", "法官总结与评议", "最后陈述"],
        "Record and Authority Review": ["Record and Authority Review", "状态评估与检索"],
        "Final Training Report": ["Final Training Report", "胜诉概率报告", "裁判预测"],
    }
    upgraded = {}
    for stage in HEARING_STAGES:
        template = _stage_template(stage, evidence, laws)
        previous = next((legacy.get(alias) for alias in stage_aliases[stage] if legacy.get(alias)), None)
        if previous:
            template.update({key: value for key, value in previous.items() if key in template})
            template["focus"] = STAGE_FOCUS[stage]
            template.setdefault("evidence_assessments", [])
            template.setdefault("state_delta", {"evidence_sufficiency": 0, "legal_relevance": 0, "fact_consistency": 0, "procedure_compliance": 0, "opposing_strength": 0, "case_support": 0})
            template.setdefault("risks", [])
        upgraded[stage] = template
    old_index = int(state.get("active_stage_index") or 0)
    state["active_stage_index"] = min(max(old_index, 0), len(HEARING_STAGES) - 1)
    state["stages"] = upgraded
    state["workflow_version"] = WORKFLOW_VERSION
    state.setdefault("agent_trace", [])
    state.setdefault("turn_results", [])
    return state


def ensure_hearing_workflow_tables() -> None:
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS hearing_runs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER,
                module_code TEXT NOT NULL, status TEXT NOT NULL, active_stage_index INTEGER NOT NULL,
                case_summary TEXT NOT NULL, state_json TEXT NOT NULL, created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hearing_runs_tenant_updated ON hearing_runs (tenant_id, updated_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS hearing_checkpoints (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, stage TEXT NOT NULL,
                version INTEGER NOT NULL, state_json TEXT NOT NULL, created_at TIMESTAMP NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hearing_checkpoints_run_stage ON hearing_checkpoints (run_id, stage, version DESC)",
        ]
    else:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS hearing_runs (
                id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, user_id BIGINT NULL,
                module_code TEXT NOT NULL, status TEXT NOT NULL, active_stage_index INTEGER NOT NULL,
                case_summary TEXT NOT NULL, state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hearing_runs_tenant_updated ON hearing_runs (tenant_id, updated_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS hearing_checkpoints (
                id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES hearing_runs(id) ON DELETE CASCADE,
                stage TEXT NOT NULL, version INTEGER NOT NULL, state_json JSONB NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_hearing_checkpoints_run_stage ON hearing_checkpoints (run_id, stage, version DESC)",
        ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _evidence_from_search(query: str, module: str, case_acl: list[str], jurisdiction: str = "") -> list[dict[str, Any]]:
    filters = {"jurisdiction": repair_text(jurisdiction)} if repair_text(jurisdiction) else {}

    def search(source_filter: str, limit: int) -> dict[str, Any]:
        try:
            return hybrid_search(query, module=module, source_filter=source_filter, limit=limit, filters=filters)
        except Exception as exc:
            result = rag_search(query, module=module, source_filter=source_filter, limit=limit, filters=filters)
            result["fallback_reason"] = f"hybrid_search_unavailable: {type(exc).__name__}"
            result["strategy"] = "lexical_fallback"
            return result

    with ThreadPoolExecutor(max_workers=2) as executor:
        case_future = executor.submit(search, "case", 6)
        law_future = executor.submit(search, "law", 4)
        results = [case_future.result(), law_future.result()]
    output = []
    allowed = {repair_text(item) for item in case_acl if repair_text(item)}
    for result in results:
        for item in result.get("items") or []:
            doc_id = f"{item.get('source_table')}:{item.get('source_id')}:{item.get('chunk_id')}"
            source_uid = repair_text(item.get("source_uid"))
            if allowed and doc_id not in allowed and source_uid not in allowed:
                continue
            metadata = item.get("metadata") or {}
            output.append(
                {
                    "doc_id": doc_id,
                    "title": repair_text(item.get("title")) or "Untitled source",
                    "citation": repair_text(item.get("citation") or metadata.get("citation")),
                    "source_kind": repair_text(item.get("source_kind")) or "unknown",
                    "source_url": repair_text(item.get("source_url")),
                    "court": repair_text(item.get("court_level") or metadata.get("court")),
                    "decision_date": repair_text(item.get("published_at")),
                    "score": round(float(item.get("score") or 0), 4),
                    "excerpt": repair_text(item.get("excerpt"))[:700],
                }
            )
    return output


def _persist_run(state: dict[str, Any], *, user_id: int | None, tenant_id: str, is_new: bool = False) -> None:
    ensure_hearing_workflow_tables()
    state_json = json.dumps(state, ensure_ascii=False)
    params = {
        "id": state["id"], "tenant": tenant_id, "user": user_id, "module": state["module"],
        "status": state["status"], "stage_index": state["active_stage_index"], "summary": state["case_summary"],
        "state": state_json, "now": _now(),
    }
    with engine.begin() as conn:
        if is_new:
            if is_sqlite():
                conn.execute(text("""INSERT INTO hearing_runs (id, tenant_id, user_id, module_code, status, active_stage_index, case_summary, state_json, created_at, updated_at) VALUES (:id,:tenant,:user,:module,:status,:stage_index,:summary,:state,:now,:now)"""), params)
            else:
                conn.execute(text("""INSERT INTO hearing_runs (id, tenant_id, user_id, module_code, status, active_stage_index, case_summary, state_json, created_at, updated_at) VALUES (CAST(:id AS uuid),:tenant,:user,:module,:status,:stage_index,:summary,CAST(:state AS jsonb),:now,:now)"""), params)
        elif is_sqlite():
            conn.execute(text("""UPDATE hearing_runs SET status=:status, active_stage_index=:stage_index, state_json=:state, updated_at=:now WHERE id=:id AND tenant_id=:tenant"""), params)
        else:
            conn.execute(text("""UPDATE hearing_runs SET status=:status, active_stage_index=:stage_index, state_json=CAST(:state AS jsonb), updated_at=:now WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant"""), params)


def _checkpoint(state: dict[str, Any], tenant_id: str) -> None:
    stage = HEARING_STAGES[state["active_stage_index"]]
    stage_state = state["stages"][stage]
    version = int(stage_state["checkpoint_version"]) + 1
    stage_state["checkpoint_version"] = version
    payload = json.dumps(state, ensure_ascii=False)
    params = {"id": str(uuid4()), "run_id": state["id"], "stage": stage, "version": version, "state": payload, "now": _now(), "tenant": tenant_id}
    with engine.begin() as conn:
        if is_sqlite():
            conn.execute(text("""INSERT INTO hearing_checkpoints (id, run_id, stage, version, state_json, created_at) VALUES (:id,:run_id,:stage,:version,:state,:now)"""), params)
        else:
            conn.execute(text("""INSERT INTO hearing_checkpoints (id, run_id, stage, version, state_json, created_at) VALUES (CAST(:id AS uuid),CAST(:run_id AS uuid),:stage,:version,CAST(:state AS jsonb),:now)"""), params)
    state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "System", "kind": "checkpoint", "content": f"Checkpoint v{version} saved for {stage}.", "created_at": _now()})


def _load_run(run_id: str, tenant_id: str) -> dict[str, Any]:
    ensure_hearing_workflow_tables()
    sql = "SELECT state_json FROM hearing_runs WHERE id=:id AND tenant_id=:tenant" if is_sqlite() else "SELECT state_json FROM hearing_runs WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant"
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"id": run_id, "tenant": tenant_id}).mappings().first()
    if not row:
        raise LookupError("The hearing simulation was not found or access is not permitted.")
    value = row["state_json"]
    state = json.loads(value) if isinstance(value, str) else dict(value)
    return _normalize_legacy_state(state)


def _record_agent_step(state: dict[str, Any], agent: str, tool: str, started: float, status: str = "ok", notes: list[str] | None = None) -> None:
    assert_agent_tool(agent, tool)
    state.setdefault("agent_trace", []).append(
        {
            "agent": agent,
            "tool": tool,
            "status": status,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "notes": notes or [],
            "created_at": _now(),
        }
    )


def _allowed_ids(state: dict[str, Any]) -> set[str]:
    return {item["doc_id"] for item in state.get("evidence_catalog") or []}


def _role_fallback_content(role: Literal["Opposing Counsel", "Presiding Judge"], stage: str) -> str:
    if role == "Presiding Judge":
        return f"The Court asks counsel to address the following focus: {STAGE_FOCUS.get(stage, stage)} Identify the record references and any matter that remains unverified."
    return "Opposing counsel disputes propositions that are not tied to the record. Identify the evidence for each required element, alleged loss, and causal link."


def _guard_simulated_reply(
    state: dict[str, Any],
    reply: SimulatedReply,
    role: Literal["Opposing Counsel", "Presiding Judge"],
    stage: str,
) -> SimulatedReply:
    catalog = list(state.get("evidence_catalog") or [])
    allowed_ids = {item["doc_id"] for item in catalog}
    law_ids = {item["doc_id"] for item in catalog if item.get("source_kind") == "law"}
    evidence_refs = [item for item in reply.cited_evidence_ids if item in allowed_ids]
    legal_refs = [item for item in reply.legal_basis_ids if item in law_ids]
    grounded = enforce_grounded_legal_text(reply.content, catalog)
    content = grounded["safe_text"]
    repetition = find_role_repetition(content, state.get("messages") or [], speaker=role)
    pending = list(reply.pending_verifications)
    if grounded["blocked_count"]:
        pending.append(f"Blocked {grounded['blocked_count']} legal citation(s) not supported by the retrieved sources.")
    if repetition["repeated"]:
        content = _role_fallback_content(role, stage)
        pending.append(f"The draft matched earlier speech at {repetition['similarity']:.2f} similarity and was rewritten for this role.")
    if re.search(r"[\u3400-\u9fff]", content):
        content = _role_fallback_content(role, stage)
        pending.append("A non-English draft was blocked and replaced with role-specific English training copy.")
    return reply.model_copy(
        update={
            "speaker": role,
            "content": content,
            "cited_evidence_ids": evidence_refs,
            "legal_basis_ids": legal_refs,
            "pending_verifications": list(dict.fromkeys(pending))[:3],
        }
    )


def _simulate_reply(
    state: dict[str, Any],
    stage: str,
    role: Literal["Opposing Counsel", "Presiding Judge"] = "Opposing Counsel",
) -> SimulatedReply:
    current = state["stages"][stage]
    allowed_ids = _allowed_ids(state)
    evidence = [item for item in state["evidence_catalog"] if item["doc_id"] in current["evidence_ids"]][:3]
    compact_evidence = [{"id": item["doc_id"], "title": item["title"], "citation": item["citation"], "kind": item["source_kind"]} for item in evidence]
    role_key = "judge" if role == "Presiding Judge" else "opposing_counsel"
    instructions = build_role_instruction(
        role_key,
        "This is a Canadian civil-hearing training simulation. Ask a focused question or advance a simulated response; do not represent a real party. "
        "Do not create facts, authorities, or evidence IDs. Use only candidate IDs supplied in the input. Return concise English JSON and identify verification gaps. ",
    )
    user_input = json.dumps({"required_speaker": role, "stage": stage, "case_summary": state["case_summary"][:3000], "our_latest": current["our_responses"][-1:] or current["facts"][-1:], "candidate_evidence": compact_evidence}, ensure_ascii=False)
    if is_llm_configured():
        try:
            raw = create_structured_response("hearing_simulated_reply_v1", HEARING_REPLY_SCHEMA, instructions, user_input)
            reply = SimulatedReply.model_validate(raw["data"])
            return _guard_simulated_reply(state, reply, role, stage)
        except (LLMServiceError, ValueError):
            pass
    evidence_ids = [item["doc_id"] for item in evidence[:1]]
    law_ids = [item["doc_id"] for item in evidence if item["source_kind"] == "law"][:1]
    reply = SimulatedReply(speaker=role, content=_role_fallback_content(role, stage), disputed_issue="The material facts, evidentiary nexus, and applicable legal elements remain to be verified.", cited_evidence_ids=evidence_ids, legal_basis_ids=law_ids, pending_verifications=["This is deterministic training copy and must be checked against the original record by Canadian counsel."], confidence=0.35)
    return _guard_simulated_reply(state, reply, role, stage)


def _refresh_stage_metrics(stage_state: dict[str, Any]) -> None:
    evidence_score = min(1.0, len(stage_state["evidence_ids"]) / 3)
    legal_score = min(1.0, len(stage_state["legal_basis_ids"]) / 2)
    interaction_score = min(1.0, (len(stage_state["opponent_defences"]) + len(stage_state["our_responses"])) / 3)
    stage_state["confidence"] = {
        "evidence_sufficiency": round(evidence_score, 2),
        "legal_relevance": round(legal_score, 2),
        "stage_readiness": round((evidence_score * 0.45) + (legal_score * 0.35) + (interaction_score * 0.20), 2),
    }


def _simulate_replies(state: dict[str, Any], stage: str) -> list[SimulatedReply]:
    """Generate one role-distinct reply for opposing counsel and the presiding judge."""
    replies = []
    for role, agent, tool in (("Opposing Counsel", "opposing_counsel_agent", "authority_links"), ("Presiding Judge", "judge_agent", "evidence_verification")):
        started = time.monotonic()
        reply = _simulate_reply(state, stage, role)
        _record_agent_step(state, agent, tool, started, notes=[f"stage={stage}", "simulated=true"])
        replies.append(reply)
    return replies


def _reference_rows(state: dict[str, Any], ids: list[str]) -> list[dict[str, Any]]:
    lookup = {item["doc_id"]: item for item in state.get("evidence_catalog") or []}
    return [lookup[item] for item in ids if item in lookup]


def _turn_risks(state: dict[str, Any], stage: str, content: str, stage_state: dict[str, Any]) -> list[dict[str, Any]]:
    lowered = repair_text(content).lower()
    references = _reference_rows(state, stage_state.get("legal_basis_ids") or stage_state.get("evidence_ids") or [])[:3]
    reference_payload = [{"doc_id": row["doc_id"], "title": row["title"], "citation": row["citation"], "source_url": row["source_url"]} for row in references]
    risks = []
    def add(category: str, level: str, reason: str, suggestion: str, confidence: float) -> None:
        risks.append({
            "node": stage,
            "category": category,
            "level": level,
            "reason": reason,
            "suggestion": suggestion,
            "confidence": confidence,
            "references": reference_payload,
            "direct_authority_status": "candidate_authority_attached" if reference_payload else "no_verified_direct_authority_retrieved",
        })
    if any(token in lowered for token in ("没签合同", "未签合同", "no contract", "without contract")):
        add("substantive", "high", "The submission indicates that no written contract is available, which may impair proof of the relationship and material terms.", "Add communications, payment records, performance records, quotations, or other evidence of agreement and performance.", 0.82)
    if any(token in lowered for token in ("微信截图", "截图", "screenshot", "screen shot")):
        add("evidence", "high", "The original device, method of capture, and completeness of the screenshot have not been verified.", "Preserve the original device and file, export metadata, establish a timeline, and prepare an authentication foundation.", 0.86)
    if any(token in lowered for token in ("投资款", "investment")) and any(token in lowered for token in ("借", "loan")):
        add("substantive", "high", "Describing the funds as both a loan and an investment may create a material factual inconsistency.", "Explain each transfer and add evidence of repayment terms, demands, interest, or allocation of investment risk.", 0.8)
    if any(token in lowered for token in ("三年", "时效", "limitation", "limitation period")):
        add("procedure", "medium", "The submission raises a possible limitation-period issue; discoverability and any suspension or acknowledgment require verification.", "Add dated evidence concerning discovery, service, demands, acknowledgment, and any event that may affect the limitation analysis.", 0.7)
    if any(token in lowered for token in ("无财产", "没有财产", "履行能力", "资产", "asset", "insolvent")):
        add("enforcement", "medium", "The record suggests limited information about assets or ability to satisfy a judgment.", "Identify assets, accounts, employment, registrations, and preservation options for review by Canadian counsel.", 0.68)
    if not stage_state.get("legal_basis_ids"):
        add("substantive", "high", "No verifiable Canadian legal authority is attached to this stage.", "Retrieve an in-force provision or controlling Canadian authority and verify its jurisdiction, date, and source.", 0.9)
    return risks


def _evaluate_turn(state: dict[str, Any], stage: str, content: str, stage_state: dict[str, Any]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    started = time.monotonic()
    _refresh_stage_metrics(stage_state)
    lowered = repair_text(content).lower()
    evidence = min(100, int(stage_state["confidence"]["evidence_sufficiency"] * 100))
    legal = min(100, int(stage_state["confidence"]["legal_relevance"] * 100))
    facts = 72 if len(stage_state.get("facts") or []) >= 2 else 58
    if any(token in lowered for token in ("投资款", "investment")) and any(token in lowered for token in ("借", "loan")):
        facts = min(facts, 42)
    procedure = 78 if any(token in lowered for token in ("日期", "date", "送达", "时效", "limitation")) else 62
    opposing = min(90, 35 + len(stage_state.get("opponent_defences") or []) * 12 + len(stage_state.get("pending_verifications") or []) * 4)
    cases = min(100, len([row for row in _reference_rows(state, stage_state.get("evidence_ids") or []) if row.get("source_kind") == "case"]) * 25)
    delta = {"evidence_sufficiency": evidence, "legal_relevance": legal, "fact_consistency": facts, "procedure_compliance": procedure, "opposing_strength": opposing, "case_support": cases}
    stage_state["state_delta"] = delta
    risks = _turn_risks(state, stage, content, stage_state)
    stage_state["risks"] = risks
    stage_state["evidence_assessments"] = [
        {"doc_id": row["doc_id"], "authenticity": "unverified", "admissibility": "unverified", "relevance": "retrieval candidate requiring counsel review"}
        for row in _reference_rows(state, stage_state.get("evidence_ids") or [])
    ]
    _record_agent_step(state, "evaluation_agent", "risk_annotation", started, notes=[f"stage={stage}", f"risks={len(risks)}"])
    return delta, risks


def _stage_missing_v2(stage_state: dict[str, Any]) -> list[str]:
    if stage_state.get("procedure_omission"):
        return []
    missing = []
    if not stage_state.get("facts"):
        missing.append("User counsel has not supplied facts or a response.")
    if not stage_state.get("evidence_ids"):
        missing.append("No record evidence is attached.")
    if not stage_state.get("legal_basis_ids"):
        missing.append("No verifiable Canadian authority is attached.")
    if stage_state["stage"] not in {"Pre-Hearing Preparation", "Final Training Report"} and not stage_state.get("opponent_defences"):
        missing.append("No opposing submission or judicial question has been recorded.")
    return missing


def _stage_missing(stage_state: dict[str, Any]) -> list[str]:
    missing = []
    if not stage_state["facts"]:
        missing.append("User counsel has not supplied facts or a response.")
    if not stage_state["evidence_ids"]:
        missing.append("No record evidence is attached.")
    if not stage_state["legal_basis_ids"]:
        missing.append("No verifiable Canadian authority is attached.")
    if not stage_state["opponent_defences"] and stage_state["stage"] not in {"Pre-Hearing Preparation", "Final Training Report"}:
        missing.append("No opposing submission or judicial question has been recorded.")
    if not stage_state["our_responses"] and stage_state["stage"] not in {"Pre-Hearing Preparation", "Final Training Report"}:
        missing.append("User counsel has not answered the opposing submission or judicial question.")
    return missing


def _aggregate(state: dict[str, Any]) -> dict[str, Any]:
    completed = [state["stages"][stage] for stage in HEARING_STAGES[:-1]]
    metrics = [item["confidence"] for item in completed]
    evidence = sum(item["evidence_sufficiency"] for item in metrics) / len(metrics)
    legal = sum(item["legal_relevance"] for item in metrics) / len(metrics)
    readiness = sum(item["stage_readiness"] for item in metrics) / len(metrics)
    pending = [note for item in completed for note in item["pending_verifications"]]
    risks = []
    if evidence < 0.75:
        risks.append({"node": "Evidence Examination", "type": "evidence", "reason": "The record does not adequately cover the material propositions or chain of preservation.", "action": "Attach at least one verifiable record item to each material proposition."})
    if legal < 0.75:
        risks.append({"node": "Legal Arguments", "type": "authority", "reason": "The available statutes or precedents do not support a stable element-by-element analysis.", "action": "Add applicable in-force legislation and controlling or persuasive Canadian authorities."})
    if pending:
        risks.append({"node": "Pre-Hearing Preparation", "type": "verification", "reason": f"{len(pending)} item(s) remain unverified.", "action": "Verify source, currency, jurisdiction, and access before requesting the final report."})
    return {
        "status": "unavailable_unvalidated_model",
        "probability": None,
        "display_probability": False,
        "reason": "No outcome model has passed out-of-time testing and calibration, so the system does not display an arbitrary probability of success.",
        "readiness": {"evidence_sufficiency": round(evidence, 2), "legal_relevance": round(legal, 2), "workflow_completeness": round(readiness, 2)},
        "risks": risks,
        "required_for_probability": ["jurisdiction- and claim-specific outcome labels", "an out-of-time test set", "Brier score, ECE, and reliability-curve acceptance", "Canadian counsel review and a versioned evaluation report"],
    }


def _aggregate_final_report(state: dict[str, Any]) -> dict[str, Any]:
    completed = [state["stages"][stage] for stage in HEARING_STAGES[:-1] if state["stages"][stage]["status"] in {"completed", "procedure_omitted"}]
    if not completed:
        return {"status": "blocked", "display_probability": False, "probability": None, "reason": "No hearing stage has been completed; a final report cannot be generated.", "disclaimer": "Training simulation only; not legal advice. Consult qualified Canadian counsel."}
    keys = ("evidence_sufficiency", "legal_relevance", "fact_consistency", "procedure_compliance", "opposing_strength", "case_support")
    averages = {key: round(sum(item["state_delta"].get(key, 0) for item in completed) / len(completed), 2) for key in keys}
    score = (
        settings.hearing_score_evidence_weight * averages["evidence_sufficiency"]
        + settings.hearing_score_legal_weight * averages["legal_relevance"]
        + settings.hearing_score_fact_weight * averages["fact_consistency"]
        + settings.hearing_score_procedure_weight * averages["procedure_compliance"]
        + settings.hearing_score_case_weight * averages["case_support"]
        + settings.hearing_score_opposition_weight * (100 - averages["opposing_strength"])
    )
    probability = round(100 / (1 + math.exp(-((score - 50) / 10))), 1)
    all_risks = [risk for item in completed for risk in item.get("risks") or []]
    uncertainty = min(24, 6 + len(all_risks) * 2 + sum(len(item.get("pending_verifications") or []) for item in completed))
    optimized_score = min(100, score + min(15, len(all_risks) * 3 + 4))
    optimized_probability = round(100 / (1 + math.exp(-((optimized_score - 50) / 10))), 1)
    citation_rows = []
    seen = set()
    for item in state.get("evidence_catalog") or []:
        if item["doc_id"] in seen:
            continue
        seen.add(item["doc_id"])
        citation_rows.append({"doc_id": item["doc_id"], "title": item["title"], "citation": item["citation"], "source_url": item["source_url"], "source_kind": item["source_kind"]})
    probability_enabled = bool(settings.hearing_final_report_enabled and settings.hearing_simulation_probability_enabled)
    return {
        "status": "simulated_uncalibrated" if probability_enabled else "disabled_by_configuration",
        "display_probability": probability_enabled,
        "probability": probability if probability_enabled else None,
        "confidence_interval": [max(0, round(probability - uncertainty, 1)), min(100, round(probability + uncertainty, 1))] if probability_enabled else None,
        "optimized_probability": optimized_probability if probability_enabled else None,
        "score": round(score, 2),
        "score_formula": "Configured weights across six record-quality dimensions; this is a training-readiness score, not an outcome probability.",
        "calibration_status": "Uncalibrated: no frozen outcome-labelled dataset has passed out-of-time Brier score, ECE, and reliability-curve review.",
        "state_metrics": averages,
        "risks": all_risks,
        "key_drivers": [
            {"name": "Evidence sufficiency", "value": averages["evidence_sufficiency"]},
            {"name": "Authority relevance", "value": averages["legal_relevance"]},
            {"name": "Factual consistency", "value": averages["fact_consistency"]},
            {"name": "Procedural compliance", "value": averages["procedure_compliance"]},
            {"name": "Case-law support", "value": averages["case_support"]},
            {"name": "Opposing-case strength", "value": averages["opposing_strength"]},
        ],
        "citations": citation_rows,
        "disclaimer": "This report is a training aid based on the local record and retrieved Canadian sources. It is not legal advice and requires review by qualified Canadian counsel.",
    }


def _view(state: dict[str, Any]) -> dict[str, Any]:
    stage_nodes = []
    for index, stage in enumerate(HEARING_STAGES):
        record = state["stages"][stage]
        stage_nodes.append({"stage": stage, "status": record["status"], "index": index, "readiness": record["confidence"]["stage_readiness"], "pending_count": len(record["pending_verifications"])})
    return {**state, "stage_nodes": stage_nodes, "active_stage": HEARING_STAGES[state["active_stage_index"]] if state["active_stage_index"] < len(HEARING_STAGES) else "Completed", "disclaimer": "This is a Canadian legal research and hearing simulation. It does not represent a real party or court, and every source and conclusion requires review by qualified Canadian counsel."}


def _workflow_view(state: dict[str, Any]) -> dict[str, Any]:
    payload = _view(state)
    payload["workflow_version"] = WORKFLOW_VERSION
    payload["latest_turn"] = (state.get("turn_results") or [None])[-1]
    payload["messages"] = list(state.get("messages") or [])
    payload["agent_trace"] = list(state.get("agent_trace") or [])[-30:]
    return payload


def create_hearing_run(payload: HearingRunCreate, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    if not settings.hearing_simulation_enabled:
        raise ValueError("The Canadian hearing simulation is disabled by configuration.")
    started = time.monotonic()
    module = "canada"
    jurisdiction = repair_text(payload.jurisdiction) or "Canada"
    evidence = _evidence_from_search(payload.case_summary, module, payload.case_acl, jurisdiction)
    laws = [item for item in evidence if item["source_kind"] == "law"]
    run_id = str(uuid4())
    state = {
        "id": run_id, "workflow_version": WORKFLOW_VERSION, "module": module, "status": "active", "active_stage_index": 0,
        "case_summary": repair_text(payload.case_summary), "jurisdiction": jurisdiction,
        "evidence_catalog": evidence, "messages": [], "stages": {stage: _stage_template(stage, evidence, laws) for stage in HEARING_STAGES},
        "turn_results": [], "agent_trace": [], "final_report": None, "created_at": _now(), "updated_at": _now(),
    }
    _record_agent_step(state, "hearing_orchestrator", "hearing_state", started, notes=["run_created", f"evidence={len(evidence)}"])
    state["messages"].append({"id": str(uuid4()), "stage": HEARING_STAGES[0], "speaker": "System", "kind": "system", "content": "The Canadian hearing simulation is ready. State the relief sought, material facts, available evidence, procedural dates, and the applicable province or federal jurisdiction.", "created_at": _now()})
    _persist_run(state, user_id=user_id, tenant_id=tenant_id, is_new=True)
    return _workflow_view(state)


def get_hearing_run(run_id: str, *, tenant_id: str) -> dict[str, Any]:
    return _workflow_view(_load_run(run_id, tenant_id))


def _submit_hearing_turn_v1(run_id: str, payload: HearingTurnInput, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    state = _load_run(run_id, tenant_id)
    if state["status"] != "active":
        raise ValueError("This hearing simulation is closed and cannot accept another submission.")
    stage = HEARING_STAGES[state["active_stage_index"]]
    stage_state = state["stages"][stage]
    allowed_ids = _allowed_ids(state)
    evidence_ids = [item for item in payload.evidence_ids if item in allowed_ids]
    legal_ids = [item for item in payload.legal_basis_ids if item in allowed_ids]
    if evidence_ids:
        stage_state["evidence_ids"] = list(dict.fromkeys(stage_state["evidence_ids"] + evidence_ids))
    if legal_ids:
        stage_state["legal_basis_ids"] = list(dict.fromkeys(stage_state["legal_basis_ids"] + legal_ids))
    message = {"id": str(uuid4()), "stage": stage, "speaker": "User Counsel", "kind": "user", "content": repair_text(payload.content), "evidence_ids": evidence_ids, "legal_basis_ids": legal_ids, "created_at": _now()}
    state["messages"].append(message)
    stage_state["facts"].append(repair_text(payload.content))
    stage_state["our_responses"].append(repair_text(payload.content))
    if stage != "Final Training Report":
        default_role: Literal["Opposing Counsel", "Presiding Judge"] = "Presiding Judge" if stage in {"Evidence Examination", "Judicial Questions and Summary"} else "Opposing Counsel"
        reply = _simulate_reply(state, stage, default_role)
        stage_state["opponent_defences"].append(reply.content)
        if reply.disputed_issue:
            stage_state["disputed_issues"].append(reply.disputed_issue)
        stage_state["pending_verifications"] = list(dict.fromkeys(stage_state["pending_verifications"] + reply.pending_verifications))[:8]
        state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": reply.speaker, "kind": "simulated", "content": reply.content, "evidence_ids": reply.cited_evidence_ids, "legal_basis_ids": reply.legal_basis_ids, "confidence": reply.confidence, "created_at": _now(), "simulated": True})
    _refresh_stage_metrics(stage_state)
    if payload.action == "complete_stage":
        missing = _stage_missing(stage_state)
        if missing:
            state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "System", "kind": "blocked", "content": "This stage cannot be completed: " + "; ".join(missing), "created_at": _now()})
        elif stage == "Final Training Report":
            state["final_report"] = _aggregate(state)
            stage_state["status"] = "completed"
            state["status"] = "completed"
            _checkpoint(state, tenant_id)
        else:
            stage_state["status"] = "completed"
            _checkpoint(state, tenant_id)
            state["active_stage_index"] += 1
            next_stage = HEARING_STAGES[state["active_stage_index"]]
            state["stages"][next_stage]["status"] = "active"
            state["messages"].append({"id": str(uuid4()), "stage": next_stage, "speaker": "System", "kind": "stage_start", "content": f"The simulation has moved to {next_stage}. Address the stage focus with record evidence and verified Canadian authorities.", "created_at": _now()})
    state["updated_at"] = _now()
    _persist_run(state, user_id=user_id, tenant_id=tenant_id)
    return _workflow_view(state)


def _advance_stage(state: dict[str, Any], tenant_id: str) -> None:
    current_index = state["active_stage_index"]
    stage = HEARING_STAGES[current_index]
    state["stages"][stage]["status"] = "completed" if not state["stages"][stage].get("procedure_omission") else "procedure_omitted"
    _checkpoint(state, tenant_id)
    state["active_stage_index"] += 1
    next_stage = HEARING_STAGES[state["active_stage_index"]]
    state["stages"][next_stage]["status"] = "active"
    state["messages"].append({"id": str(uuid4()), "stage": next_stage, "speaker": "System", "kind": "stage_start", "content": f"Entering {next_stage}: {STAGE_FOCUS[next_stage]}", "created_at": _now()})


def submit_hearing_turn(run_id: str, payload: HearingTurnInput, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    state = _load_run(run_id, tenant_id)
    if state.get("status") != "active":
        raise ValueError("This hearing simulation is closed and cannot accept another submission.")
    started = time.monotonic()
    stage = HEARING_STAGES[state["active_stage_index"]]
    stage_state = state["stages"][stage]
    _record_agent_step(state, "hearing_orchestrator", "hearing_state", started, notes=[f"stage={stage}", f"action={payload.action}"])
    allowed_ids = _allowed_ids(state)
    evidence_ids = [item for item in payload.evidence_ids if item in allowed_ids]
    legal_ids = [item for item in payload.legal_basis_ids if item in allowed_ids]
    stage_state["evidence_ids"] = list(dict.fromkeys(stage_state.get("evidence_ids", []) + evidence_ids))
    stage_state["legal_basis_ids"] = list(dict.fromkeys(stage_state.get("legal_basis_ids", []) + legal_ids))
    user_message = {"id": str(uuid4()), "stage": stage, "speaker": "User Counsel", "kind": "user", "content": repair_text(payload.content), "evidence_ids": evidence_ids, "legal_basis_ids": legal_ids, "created_at": _now()}
    state["messages"].append(user_message)
    stage_state["facts"].append(user_message["content"])
    stage_state["our_responses"].append(user_message["content"])
    turn_messages = [{"role": "user", "content": user_message["content"]}]

    if payload.action == "procedure_omission":
        basis = repair_text(payload.omission_basis) or user_message["content"]
        if not basis:
            raise ValueError("A procedural omission requires a recorded legal or practical basis.")
        stage_state["procedure_omission"] = {"reason": basis, "recorded_at": _now(), "recorded_by": "User Counsel"}
        state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "System", "kind": "procedure_omission", "content": f"The procedural omission has been recorded and still requires counsel review: {basis}", "created_at": _now()})
    elif stage != "Final Training Report":
        for reply in _simulate_replies(state, stage):
            if reply.speaker == "Opposing Counsel":
                stage_state["opponent_defences"].append(reply.content)
                role = "opposing_counsel"
            else:
                role = "judge"
            if reply.disputed_issue:
                stage_state["disputed_issues"].append(reply.disputed_issue)
            stage_state["pending_verifications"] = list(dict.fromkeys(stage_state.get("pending_verifications", []) + reply.pending_verifications))[:12]
            state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": reply.speaker, "kind": "simulated", "content": reply.content, "evidence_ids": reply.cited_evidence_ids, "legal_basis_ids": reply.legal_basis_ids, "confidence": reply.confidence, "created_at": _now(), "simulated": True})
            turn_messages.append({"role": role, "content": reply.content})

    citation_started = time.monotonic()
    selected = _reference_rows(state, list(dict.fromkeys(stage_state["evidence_ids"] + stage_state["legal_basis_ids"])))
    missing_direct = [row["doc_id"] for row in selected if not (row.get("citation") or row.get("source_url"))]
    if missing_direct:
        stage_state["pending_verifications"] = list(dict.fromkeys(stage_state["pending_verifications"] + ["One or more candidate sources lack a traceable citation or URL; no verified direct authority was retrieved for those items."] ))
    _record_agent_step(state, "evidence_agent", "evidence_verification", citation_started, notes=[f"references={len(selected)}", f"unverified={len(missing_direct)}"])
    state_delta, risks = _evaluate_turn(state, stage, user_message["content"], stage_state)
    for risk in risks:
        state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "System", "kind": "risk", "content": f"{risk['level'].title()} risk: {risk['reason']} Recommended action: {risk['suggestion']}", "risk": risk, "created_at": _now()})

    next_step = "Awaiting user counsel's response or additional record evidence."
    if payload.action in {"complete_stage", "procedure_omission"}:
        missing = _stage_missing_v2(stage_state)
        if missing:
            next_step = "Complete the following before advancing: " + "; ".join(missing)
            state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "System", "kind": "blocked", "content": next_step, "created_at": _now()})
        elif stage == "Final Training Report":
            report_started = time.monotonic()
            _record_agent_step(state, "report_agent", "rule_arbitration", report_started, notes=["final_aggregate"])
            state["final_report"] = _aggregate_final_report(state)
            stage_state["status"] = "completed"
            state["status"] = "completed"
            _checkpoint(state, tenant_id)
            next_step = "The final training report is ready. Review every citation, scoring assumption, and disclaimer."
        else:
            _advance_stage(state, tenant_id)
            next_step = f"Entering {HEARING_STAGES[state['active_stage_index']]}."

    turn_result = {"stage": stage, "messages": turn_messages, "state_delta": state_delta, "risks": risks, "next_step": next_step, "references": [{"doc_id": row["doc_id"], "citation": row["citation"], "source_url": row["source_url"], "source_kind": row["source_kind"]} for row in selected], "disclaimer": "Training simulation only; not legal advice. Consult qualified Canadian counsel."}
    state.setdefault("turn_results", []).append(turn_result)
    state["updated_at"] = _now()
    _persist_run(state, user_id=user_id, tenant_id=tenant_id)
    return _workflow_view(state)


def rollback_hearing_run(run_id: str, *, stage_index: int, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    state = _load_run(run_id, tenant_id)
    if stage_index < 0 or stage_index >= len(HEARING_STAGES):
        raise ValueError("The rollback stage is invalid.")
    stage = HEARING_STAGES[stage_index]
    if state["stages"][stage]["checkpoint_version"] <= 0:
        raise ValueError("No rollback checkpoint exists for this stage.")
    state["active_stage_index"] = stage_index
    state["status"] = "active"
    state["final_report"] = None
    for later in HEARING_STAGES[stage_index + 1:]:
        state["stages"][later]["status"] = "pending"
    state["stages"][stage]["status"] = "active"
    state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "System", "kind": "rollback", "content": f"Rolled back to the latest checkpoint for {stage}. Later stages must be reconfirmed.", "created_at": _now()})
    _persist_run(state, user_id=user_id, tenant_id=tenant_id)
    return _workflow_view(state)
