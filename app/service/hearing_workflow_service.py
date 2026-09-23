from __future__ import annotations

import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.core.config import settings
from app.service.common_service import repair_text
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.llm_service import LLMServiceError, create_structured_response, is_llm_configured
from app.service.module_service import normalize_module
from app.service.multi_agent_service import assert_agent_tool


WORKFLOW_VERSION = "hearing-simulation-2.0"
HEARING_STAGES = ["庭前准备", "开庭陈述", "举证质证", "法庭辩论", "法官总结与评议", "状态评估与检索", "胜诉概率报告"]
HEARING_ROLES = {"我方代理人", "对方代理人", "审判长", "书记员", "系统"}
STAGE_FOCUS = {
    "庭前准备": "明确争议焦点、证据清单、程序时点和检索范围。",
    "开庭陈述": "围绕请求事项、争点事实和证明责任作开场陈述。",
    "举证质证": "逐项审查证据真实性、合法性、关联性与证明力。",
    "法庭辩论": "围绕法律要件、事实适用和类案差异展开辩论。",
    "法官总结与评议": "归纳已确认和未确认事实，核查程序与举证缺口。",
    "状态评估与检索": "复核风险、类案支持、法规时效与优化动作。",
    "胜诉概率报告": "仅汇总已完成阶段的规则评分、引用和免责声明。",
}

HEARING_REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "speaker": {"type": "string", "enum": ["对方代理人", "审判长"]},
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


class HearingTurnInput(BaseModel):
    content: str = Field(min_length=2, max_length=12000)
    action: Literal["continue", "complete_stage", "procedure_omission"] = "continue"
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    legal_basis_ids: list[str] = Field(default_factory=list, max_length=12)
    omission_basis: str = Field(default="", max_length=1200)


class SimulatedReply(BaseModel):
    speaker: Literal["对方代理人", "审判长"]
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
    pending = [] if law_ids else ["未检索到可核验的法规依据，请补充法条编号或调整案情描述。"]
    return {
        "stage": stage,
        "status": "active" if stage == HEARING_STAGES[0] else "pending",
        "participants": ["我方代理人", "对方代理人", "审判长"],
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
    """Adapt persisted v1 hearing runs without changing their stored schema or deleting data."""
    if state.get("workflow_version") == WORKFLOW_VERSION:
        return state
    legacy = state.get("stages") or {}
    evidence = state.get("evidence_catalog") or []
    laws = [item for item in evidence if item.get("source_kind") == "law"]
    stage_map = {
        "庭前准备": "庭前准备",
        "开庭陈述": "法庭调查",
        "举证质证": "举证质证",
        "法庭辩论": "法庭辩论",
        "法官总结与评议": "最后陈述",
        "状态评估与检索": None,
        "胜诉概率报告": "裁判预测",
    }
    upgraded = {}
    for stage in HEARING_STAGES:
        template = _stage_template(stage, evidence, laws)
        previous = legacy.get(stage_map[stage]) if stage_map[stage] else None
        if previous:
            template.update({key: value for key, value in previous.items() if key in template})
            template["focus"] = STAGE_FOCUS[stage]
            template.setdefault("evidence_assessments", [])
            template.setdefault("state_delta", {"evidence_sufficiency": 0, "legal_relevance": 0, "fact_consistency": 0, "procedure_compliance": 0, "opposing_strength": 0, "case_support": 0})
            template.setdefault("risks", [])
        upgraded[stage] = template
    old_index = int(state.get("active_stage_index") or 0)
    old_stage = ["庭前准备", "法庭调查", "举证质证", "法庭辩论", "最后陈述", "裁判预测"]
    active_name = old_stage[min(old_index, len(old_stage) - 1)]
    reverse_map = {value: key for key, value in stage_map.items() if value}
    state["active_stage_index"] = HEARING_STAGES.index(reverse_map.get(active_name, "庭前准备"))
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


def _evidence_from_search(query: str, module: str, case_acl: list[str]) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        case_future = executor.submit(hybrid_search, query, module=module, source_filter="case", limit=6, filters={})
        law_future = executor.submit(hybrid_search, query, module=module, source_filter="law", limit=4, filters={})
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
                    "title": repair_text(item.get("title")) or "未命名资料",
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
    state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "系统", "kind": "checkpoint", "content": f"{stage}已保存检查点 v{version}。", "created_at": _now()})


def _load_run(run_id: str, tenant_id: str) -> dict[str, Any]:
    ensure_hearing_workflow_tables()
    sql = "SELECT state_json FROM hearing_runs WHERE id=:id AND tenant_id=:tenant" if is_sqlite() else "SELECT state_json FROM hearing_runs WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant"
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"id": run_id, "tenant": tenant_id}).mappings().first()
    if not row:
        raise LookupError("未找到庭审模拟记录或无权访问。")
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


def _simulate_reply(state: dict[str, Any], stage: str) -> SimulatedReply:
    current = state["stages"][stage]
    allowed_ids = _allowed_ids(state)
    evidence = [item for item in state["evidence_catalog"] if item["doc_id"] in current["evidence_ids"]][:3]
    compact_evidence = [{"id": item["doc_id"], "title": item["title"], "citation": item["citation"], "kind": item["source_kind"]} for item in evidence]
    instructions = (
        "你是加拿大法律庭审模拟中的对方代理人或审判长。只能提出模拟性问题或抗辩，不代表真实当事人。"
        "不得创建事实、法律依据或证据 ID；只能使用提供的候选 ID。输出中文 JSON，内容简洁，指出待验证事项。"
    )
    user_input = json.dumps({"stage": stage, "case_summary": state["case_summary"][:3000], "our_latest": current["our_responses"][-1:] or current["facts"][-1:], "candidate_evidence": compact_evidence}, ensure_ascii=False)
    if is_llm_configured():
        try:
            raw = create_structured_response("hearing_simulated_reply_v1", HEARING_REPLY_SCHEMA, instructions, user_input)
            reply = SimulatedReply.model_validate(raw["data"])
            reply.cited_evidence_ids = [item for item in reply.cited_evidence_ids if item in allowed_ids]
            reply.legal_basis_ids = [item for item in reply.legal_basis_ids if item in allowed_ids]
            return reply
        except (LLMServiceError, ValueError):
            pass
    evidence_ids = [item["doc_id"] for item in evidence[:1]]
    law_ids = [item["doc_id"] for item in evidence if item["source_kind"] == "law"][:1]
    speaker = "审判长" if stage in {"法庭调查", "举证质证", "最后陈述"} else "对方代理人"
    content = "请明确说明该主张对应的事实来源、证据形成时间及与争议焦点的直接关系。" if speaker == "审判长" else "模拟抗辩：现有陈述仍需以可核验材料证明关键构成要件及损失因果关系。"
    return SimulatedReply(speaker=speaker, content=content, disputed_issue="关键事实、证据关联性与法律要件尚待核验。", cited_evidence_ids=evidence_ids, legal_basis_ids=law_ids, pending_verifications=["该发言为规则化模拟，须由律师根据原始材料核验。"], confidence=0.35)


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
    """在同一份受候选证据约束的上下文中生成对方代理人与审判长各一轮发言。"""
    replies = []
    for role, agent, tool in (("对方代理人", "opposing_counsel_agent", "authority_links"), ("审判长", "judge_agent", "evidence_verification")):
        started = time.monotonic()
        reply = _simulate_reply(state, stage)
        reply = reply.model_copy(update={"speaker": role})
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
            "direct_authority_status": "已绑定候选依据" if reference_payload else "未检索到直接依据",
        })
    if any(token in lowered for token in ("没签合同", "未签合同", "no contract", "without contract")):
        add("实体风险", "高", "陈述显示缺少书面合同，法律关系和关键条款的证明可能不足。", "补充聊天记录、付款记录、履行记录、报价或其他可证明合意和履行的材料。", 0.82)
    if any(token in lowered for token in ("微信截图", "截图", "screenshot", "screen shot")):
        add("证据风险", "高", "截图的原始载体、生成方式和完整性尚未核验，对方可能质疑真实性。", "保留原始设备和原始文件，补充导出记录、时间线、公证或当庭演示方案。", 0.86)
    if any(token in lowered for token in ("投资款", "investment")) and any(token in lowered for token in ("借", "loan")):
        add("实体风险", "高", "借款与投资款表述可能矛盾，影响事实一致性和款项性质认定。", "逐笔解释转账备注，并补充借贷合意、催收、利息或风险承担的证据。", 0.8)
    if any(token in lowered for token in ("三年", "时效", "limitation", "limitation period")):
        add("程序风险", "中", "陈述涉及潜在时效或期间问题，需要核验起算、中断和中止事实。", "补充知悉损害、送达、催收、确认债务或其他可能影响期限的日期证据。", 0.7)
    if any(token in lowered for token in ("无财产", "没有财产", "履行能力", "资产", "asset", "insolvent")):
        add("执行风险", "中", "陈述涉及履行能力或财产线索不足，胜诉后仍可能面临执行回收风险。", "补充可执行财产、账户、雇主、登记信息或保全可行性的线索，并由律师评估执行措施。", 0.68)
    if not stage_state.get("legal_basis_ids"):
        add("实体风险", "高", "当前阶段未绑定可核验法律依据。", "通过本地法规检索补充现行有效的法条、条款和适用时点。", 0.9)
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
        {"doc_id": row["doc_id"], "authenticity": "待核验", "legality": "待核验", "relevance": "已由本地检索候选，需律师复核"}
        for row in _reference_rows(state, stage_state.get("evidence_ids") or [])
    ]
    _record_agent_step(state, "evaluation_agent", "risk_annotation", started, notes=[f"stage={stage}", f"risks={len(risks)}"])
    return delta, risks


def _stage_missing_v2(stage_state: dict[str, Any]) -> list[str]:
    if stage_state.get("procedure_omission"):
        return []
    missing = []
    if not stage_state.get("facts"):
        missing.append("缺少我方事实或回应。")
    if not stage_state.get("evidence_ids"):
        missing.append("未绑定证据。")
    if not stage_state.get("legal_basis_ids"):
        missing.append("未绑定可核验法律依据。")
    if stage_state["stage"] not in {"庭前准备", "胜诉概率报告"} and not stage_state.get("opponent_defences"):
        missing.append("未生成对方抗辩或法官问话。")
    return missing


def _stage_missing(stage_state: dict[str, Any]) -> list[str]:
    missing = []
    if not stage_state["facts"]:
        missing.append("缺少我方输入的事实或回应。")
    if not stage_state["evidence_ids"]:
        missing.append("未绑定证据。")
    if not stage_state["legal_basis_ids"]:
        missing.append("未绑定可核验法律依据。")
    if not stage_state["opponent_defences"] and stage_state["stage"] not in {"庭前准备", "裁判预测"}:
        missing.append("尚未形成对方抗辩或法官问话。")
    if not stage_state["our_responses"] and stage_state["stage"] not in {"庭前准备", "裁判预测"}:
        missing.append("缺少我方对抗辩或问话的回应。")
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
        risks.append({"node": "举证质证", "type": "证据风险", "reason": "证据覆盖不足，需补充原始材料、形成时间或保全链。", "action": "为每个待证事实绑定至少一项可核验证据。"})
    if legal < 0.75:
        risks.append({"node": "法庭辩论", "type": "法律风险", "reason": "法规或先例依据不足，不能形成稳定的法律要件映射。", "action": "补充适用法条、效力状态和同级以上法院先例。"})
    if pending:
        risks.append({"node": "庭前准备", "type": "待核验", "reason": f"仍有 {len(pending)} 项待验证内容。", "action": "完成来源、时效和权限核验后再请求最终报告。"})
    return {
        "status": "unavailable_unvalidated_model",
        "probability": None,
        "display_probability": False,
        "reason": "当前未接入经时间外测试和校准验证的结果模型，系统不展示任意胜诉百分比。",
        "readiness": {"evidence_sufficiency": round(evidence, 2), "legal_relevance": round(legal, 2), "workflow_completeness": round(readiness, 2)},
        "risks": risks,
        "required_for_probability": ["锁定法域与案由的真实结果标签", "时间外测试集", "Brier score、ECE 和可靠性曲线验收", "律师复核与版本化评估报告"],
    }


def _aggregate_final_report(state: dict[str, Any]) -> dict[str, Any]:
    completed = [state["stages"][stage] for stage in HEARING_STAGES[:-1] if state["stages"][stage]["status"] in {"completed", "procedure_omitted"}]
    if not completed:
        return {"status": "blocked", "display_probability": False, "probability": None, "reason": "没有完成的庭审阶段，不能生成最终报告。", "disclaimer": "非法律意见，仅供参考，建议咨询执业律师。"}
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
        "score_formula": "配置权重 × 六项状态评分，经 sigmoid 转换；仅反映本次模拟状态。",
        "calibration_status": "未校准：尚未使用冻结的真实结果标签完成时间外测试、Brier score、ECE 和可靠性曲线验收。",
        "state_metrics": averages,
        "risks": all_risks,
        "key_drivers": [
            {"name": "证据充分性", "value": averages["evidence_sufficiency"]},
            {"name": "法律相关性", "value": averages["legal_relevance"]},
            {"name": "事实一致性", "value": averages["fact_consistency"]},
            {"name": "程序合规性", "value": averages["procedure_compliance"]},
            {"name": "类案支持度", "value": averages["case_support"]},
            {"name": "对方抗辩强度", "value": averages["opposing_strength"]},
        ],
        "citations": citation_rows,
        "disclaimer": "本报告为基于本地检索资料和庭审模拟状态的辅助分析，非法律意见，仅供参考，建议咨询具备相应法域资格的执业律师。",
    }


def _view(state: dict[str, Any]) -> dict[str, Any]:
    stage_nodes = []
    for index, stage in enumerate(HEARING_STAGES):
        record = state["stages"][stage]
        stage_nodes.append({"stage": stage, "status": record["status"], "index": index, "readiness": record["confidence"]["stage_readiness"], "pending_count": len(record["pending_verifications"])})
    return {**state, "stage_nodes": stage_nodes, "active_stage": HEARING_STAGES[state["active_stage_index"]] if state["active_stage_index"] < len(HEARING_STAGES) else "已完成", "disclaimer": "庭审对话与抗辩为研究模拟，不代表真实对方或法院意见；所有材料须由具备相应法域资格的律师复核。"}


def _workflow_view(state: dict[str, Any]) -> dict[str, Any]:
    payload = _view(state)
    payload["workflow_version"] = WORKFLOW_VERSION
    payload["latest_turn"] = (state.get("turn_results") or [None])[-1]
    payload["messages"] = list(state.get("messages") or [])
    payload["agent_trace"] = list(state.get("agent_trace") or [])[-30:]
    return payload


def create_hearing_run(payload: HearingRunCreate, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    if not settings.hearing_simulation_enabled:
        raise ValueError("庭审模拟当前已由配置关闭。")
    started = time.monotonic()
    module = normalize_module(payload.module)
    evidence = _evidence_from_search(payload.case_summary, module, payload.case_acl)
    laws = [item for item in evidence if item["source_kind"] == "law"]
    run_id = str(uuid4())
    state = {
        "id": run_id, "workflow_version": WORKFLOW_VERSION, "module": module, "status": "active", "active_stage_index": 0,
        "case_summary": repair_text(payload.case_summary), "jurisdiction": repair_text(payload.jurisdiction),
        "evidence_catalog": evidence, "messages": [], "stages": {stage: _stage_template(stage, evidence, laws) for stage in HEARING_STAGES},
        "turn_results": [], "agent_trace": [], "final_report": None, "created_at": _now(), "updated_at": _now(),
    }
    _record_agent_step(state, "hearing_orchestrator", "hearing_state", started, notes=["run_created", f"evidence={len(evidence)}"])
    state["messages"].append({"id": str(uuid4()), "stage": HEARING_STAGES[0], "speaker": "系统", "kind": "system", "content": "庭审模拟已建立。请先补充请求事项、关键事实、已掌握证据和适用法域。", "created_at": _now()})
    _persist_run(state, user_id=user_id, tenant_id=tenant_id, is_new=True)
    return _workflow_view(state)


def get_hearing_run(run_id: str, *, tenant_id: str) -> dict[str, Any]:
    return _workflow_view(_load_run(run_id, tenant_id))


def _submit_hearing_turn_v1(run_id: str, payload: HearingTurnInput, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    state = _load_run(run_id, tenant_id)
    if state["status"] != "active":
        raise ValueError("该庭审模拟已结束，不能继续提交。")
    stage = HEARING_STAGES[state["active_stage_index"]]
    stage_state = state["stages"][stage]
    allowed_ids = _allowed_ids(state)
    evidence_ids = [item for item in payload.evidence_ids if item in allowed_ids]
    legal_ids = [item for item in payload.legal_basis_ids if item in allowed_ids]
    if evidence_ids:
        stage_state["evidence_ids"] = list(dict.fromkeys(stage_state["evidence_ids"] + evidence_ids))
    if legal_ids:
        stage_state["legal_basis_ids"] = list(dict.fromkeys(stage_state["legal_basis_ids"] + legal_ids))
    message = {"id": str(uuid4()), "stage": stage, "speaker": "我方代理人", "kind": "user", "content": repair_text(payload.content), "evidence_ids": evidence_ids, "legal_basis_ids": legal_ids, "created_at": _now()}
    state["messages"].append(message)
    stage_state["facts"].append(repair_text(payload.content))
    stage_state["our_responses"].append(repair_text(payload.content))
    if stage != "裁判预测":
        reply = _simulate_reply(state, stage)
        stage_state["opponent_defences"].append(reply.content)
        if reply.disputed_issue:
            stage_state["disputed_issues"].append(reply.disputed_issue)
        stage_state["pending_verifications"] = list(dict.fromkeys(stage_state["pending_verifications"] + reply.pending_verifications))[:8]
        state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": reply.speaker, "kind": "simulated", "content": reply.content, "evidence_ids": reply.cited_evidence_ids, "legal_basis_ids": reply.legal_basis_ids, "confidence": reply.confidence, "created_at": _now(), "simulated": True})
    _refresh_stage_metrics(stage_state)
    if payload.action == "complete_stage":
        missing = _stage_missing(stage_state)
        if missing:
            state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "系统", "kind": "blocked", "content": "本阶段不能完成：" + "；".join(missing), "created_at": _now()})
        elif stage == "裁判预测":
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
            state["messages"].append({"id": str(uuid4()), "stage": next_stage, "speaker": "系统", "kind": "stage_start", "content": f"进入{next_stage}。请围绕该阶段的争议焦点、证据和法律依据提交我方回应。", "created_at": _now()})
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
    state["messages"].append({"id": str(uuid4()), "stage": next_stage, "speaker": "系统", "kind": "stage_start", "content": f"进入{next_stage}：{STAGE_FOCUS[next_stage]}", "created_at": _now()})


def submit_hearing_turn(run_id: str, payload: HearingTurnInput, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    state = _load_run(run_id, tenant_id)
    if state.get("status") != "active":
        raise ValueError("该庭审模拟已结束，不能继续提交。")
    started = time.monotonic()
    stage = HEARING_STAGES[state["active_stage_index"]]
    stage_state = state["stages"][stage]
    _record_agent_step(state, "hearing_orchestrator", "hearing_state", started, notes=[f"stage={stage}", f"action={payload.action}"])
    allowed_ids = _allowed_ids(state)
    evidence_ids = [item for item in payload.evidence_ids if item in allowed_ids]
    legal_ids = [item for item in payload.legal_basis_ids if item in allowed_ids]
    stage_state["evidence_ids"] = list(dict.fromkeys(stage_state.get("evidence_ids", []) + evidence_ids))
    stage_state["legal_basis_ids"] = list(dict.fromkeys(stage_state.get("legal_basis_ids", []) + legal_ids))
    user_message = {"id": str(uuid4()), "stage": stage, "speaker": "我方代理人", "kind": "user", "content": repair_text(payload.content), "evidence_ids": evidence_ids, "legal_basis_ids": legal_ids, "created_at": _now()}
    state["messages"].append(user_message)
    stage_state["facts"].append(user_message["content"])
    stage_state["our_responses"].append(user_message["content"])
    turn_messages = [{"role": "user", "content": user_message["content"]}]

    if payload.action == "procedure_omission":
        basis = repair_text(payload.omission_basis) or user_message["content"]
        if not basis:
            raise ValueError("程序省略必须记录程序依据或原因。")
        stage_state["procedure_omission"] = {"reason": basis, "recorded_at": _now(), "recorded_by": "我方代理人"}
        state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "系统", "kind": "procedure_omission", "content": f"程序省略已记录，仍需律师核验：{basis}", "created_at": _now()})
    elif stage != "胜诉概率报告":
        for reply in _simulate_replies(state, stage):
            if reply.speaker == "对方代理人":
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
        stage_state["pending_verifications"] = list(dict.fromkeys(stage_state["pending_verifications"] + ["部分候选资料缺少可追溯引文或来源链接，未检索到直接依据。"] ))
    _record_agent_step(state, "evidence_agent", "evidence_verification", citation_started, notes=[f"references={len(selected)}", f"unverified={len(missing_direct)}"])
    state_delta, risks = _evaluate_turn(state, stage, user_message["content"], stage_state)
    for risk in risks:
        state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "系统", "kind": "risk", "content": f"{risk['level']}风险：{risk['reason']} 建议：{risk['suggestion']}", "risk": risk, "created_at": _now()})

    next_step = "等待用户回应法官问题或补充证据"
    if payload.action in {"complete_stage", "procedure_omission"}:
        missing = _stage_missing_v2(stage_state)
        if missing:
            next_step = "本阶段待补充后才能进入下一阶段：" + "；".join(missing)
            state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "系统", "kind": "blocked", "content": next_step, "created_at": _now()})
        elif stage == "胜诉概率报告":
            report_started = time.monotonic()
            _record_agent_step(state, "report_agent", "rule_arbitration", report_started, notes=["final_aggregate"])
            state["final_report"] = _aggregate_final_report(state)
            stage_state["status"] = "completed"
            state["status"] = "completed"
            _checkpoint(state, tenant_id)
            next_step = "最终报告已生成；请复核引用、模拟评分假设和免责声明。"
        else:
            _advance_stage(state, tenant_id)
            next_step = f"进入{HEARING_STAGES[state['active_stage_index']]}。"

    turn_result = {"stage": stage, "messages": turn_messages, "state_delta": state_delta, "risks": risks, "next_step": next_step, "references": [{"doc_id": row["doc_id"], "citation": row["citation"], "source_url": row["source_url"], "source_kind": row["source_kind"]} for row in selected], "disclaimer": "非法律意见，仅供参考，建议咨询执业律师。"}
    state.setdefault("turn_results", []).append(turn_result)
    state["updated_at"] = _now()
    _persist_run(state, user_id=user_id, tenant_id=tenant_id)
    return _workflow_view(state)


def rollback_hearing_run(run_id: str, *, stage_index: int, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    state = _load_run(run_id, tenant_id)
    if stage_index < 0 or stage_index >= len(HEARING_STAGES):
        raise ValueError("无效的回滚阶段。")
    stage = HEARING_STAGES[stage_index]
    if state["stages"][stage]["checkpoint_version"] <= 0:
        raise ValueError("该阶段尚无可回滚检查点。")
    state["active_stage_index"] = stage_index
    state["status"] = "active"
    state["final_report"] = None
    for later in HEARING_STAGES[stage_index + 1:]:
        state["stages"][later]["status"] = "pending"
    state["stages"][stage]["status"] = "active"
    state["messages"].append({"id": str(uuid4()), "stage": stage, "speaker": "系统", "kind": "rollback", "content": f"已回滚至{stage}的最近检查点；后续阶段需要重新确认。", "created_at": _now()})
    _persist_run(state, user_id=user_id, tenant_id=tenant_id)
    return _workflow_view(state)
