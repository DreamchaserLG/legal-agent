from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator
from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text
from app.service.hybrid_retrieval_service import hybrid_search


WORKFLOW_VERSION = "ontario-civil-trial-1.0"
DISCLAIMER = (
    "这是安大略民事审判训练模拟。判决仅依据本次模拟中获准进入记录的证言和证物；"
    "律师表现分与司法结论分开，不是胜诉概率，也不是法律意见。"
)

PHASE_LABELS = {
    "case_management": "庭前与开庭",
    "opening": "开庭陈述",
    "plaintiff_evidence": "原告举证",
    "defendant_evidence": "被告举证",
    "reply_evidence": "回复证据审查",
    "closing": "结案陈词",
    "judgment": "判决与训练评分",
}

ROLE_LABELS = {
    "court_clerk": "书记员",
    "plaintiff_counsel": "原告律师",
    "defendant_counsel": "被告律师",
    "plaintiff_witness": "原告证人",
    "defendant_witness": "被告证人",
    "expert_witness": "专家证人",
    "evidence_officer": "证据核验代理",
    "legal_research_agent": "法律研究代理",
    "trial_judge": "审判法官",
    "evaluation_agent": "训练评估代理",
    "system": "系统",
}

ROLE_TOOL_ALLOWLIST = {
    "court_clerk": {"workflow_state", "session_control", "exhibit_registry", "witness_registry"},
    "plaintiff_counsel": {"record_read", "authority_read", "question_witness", "tender_exhibit", "raise_objection"},
    "defendant_counsel": {"record_read", "authority_read", "question_witness", "tender_exhibit", "raise_objection"},
    "plaintiff_witness": {"personal_record_read", "answer_question"},
    "defendant_witness": {"personal_record_read", "answer_question"},
    "expert_witness": {"expert_report_read", "answer_question"},
    "evidence_officer": {"exhibit_registry", "source_validation", "foundation_check"},
    "legal_research_agent": {"hybrid_search", "authority_read"},
    "trial_judge": {"transcript_read", "authority_read", "rule_objection", "control_hearing", "build_judgment"},
    "evaluation_agent": {"transcript_read", "score_advocacy", "build_report"},
}

ROLE_BOUNDARIES = [
    {"role": "court_clerk", "goal": "维护庭次、证人和证物编号", "stop": "不得评价证明力或决定争议事实"},
    {"role": "plaintiff_counsel", "goal": "陈述原告诉求、询问己方证人、盘问对方并提出异议", "stop": "不得替证人作答或决定异议"},
    {"role": "defendant_counsel", "goal": "回应请求、询问己方证人、盘问对方并提出异议", "stop": "不得替证人作答或决定异议"},
    {"role": "plaintiff_witness", "goal": "只按个人证词记录回答问题", "stop": "不得检索网页、引用法律或代替律师辩论"},
    {"role": "defendant_witness", "goal": "只按个人证词记录回答问题", "stop": "不得检索网页、引用法律或代替律师辩论"},
    {"role": "expert_witness", "goal": "在获准的专业范围和报告内容内回答", "stop": "不得越出专业资格或补造报告意见"},
    {"role": "evidence_officer", "goal": "核对来源、基础证人、真实性和传闻风险", "stop": "只给核验结果，不作准入裁定"},
    {"role": "legal_research_agent", "goal": "检索并登记可追溯法律依据", "stop": "不得生成证言或决定案件事实"},
    {"role": "trial_judge", "goal": "主持程序、裁定异议并按优势证据标准作出判决", "stop": "不得替任何一方补证或把训练分用于判决"},
    {"role": "evaluation_agent", "goal": "对双方庭审表现评分并提出修正动作", "stop": "不得改变证据裁定或司法结论"},
]

PROCEDURAL_AUTHORITIES = [
    {
        "authority_id": "AUTH-R52",
        "authority_type": "law",
        "title": "Ontario Rules of Civil Procedure - Rule 52",
        "citation": "R.R.O. 1990, Reg. 194, rr. 52.01-52.10",
        "source_url": "https://www.ontario.ca/laws/regulation/900194",
        "excerpt": "Trial procedure, order of presentation, adjournment and related trial controls.",
        "source_status": "official",
    },
    {
        "authority_id": "AUTH-R53",
        "authority_type": "law",
        "title": "Ontario Rules of Civil Procedure - Rule 53",
        "citation": "R.R.O. 1990, Reg. 194, rr. 53.01-53.08",
        "source_url": "https://www.ontario.ca/laws/regulation/900194",
        "excerpt": "Oral evidence, examination-in-chief, cross-examination, re-examination and expert reports.",
        "source_status": "official",
    },
    {
        "authority_id": "AUTH-EA35",
        "authority_type": "law",
        "title": "Ontario Evidence Act - business records",
        "citation": "R.S.O. 1990, c. E.23, s. 35",
        "source_url": "https://www.ontario.ca/laws/statute/90e23",
        "excerpt": "Conditions governing the admission of records made in the usual and ordinary course of business.",
        "source_status": "official",
    },
]


class TrialIssueInput(BaseModel):
    description: str = Field(min_length=4, max_length=1200)
    burden_side: Literal["plaintiff", "defendant"] = "plaintiff"


class TrialWitnessInput(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    side: Literal["plaintiff", "defendant"]
    kind: Literal["fact", "expert"] = "fact"
    role_description: str = Field(min_length=2, max_length=500)
    testimony_points: list[str] = Field(min_length=2, max_length=10)
    issue_indexes: list[int] = Field(default_factory=lambda: [1], max_length=8)
    credibility_risks: list[str] = Field(default_factory=list, max_length=6)
    expert_field: str = Field(default="", max_length=300)
    report_title: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_expert(self) -> "TrialWitnessInput":
        if self.kind == "expert" and (not repair_text(self.expert_field) or not repair_text(self.report_title)):
            raise ValueError("专家证人必须提供专业领域和报告名称。")
        return self


class TrialExhibitInput(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    description: str = Field(min_length=4, max_length=1200)
    proponent: Literal["plaintiff", "defendant"]
    foundation_witness: str = Field(min_length=2, max_length=120)
    issue_indexes: list[int] = Field(default_factory=lambda: [1], max_length=8)
    authenticity: Literal["admitted", "disputed", "unknown"] = "admitted"
    hearsay_risk: bool = False
    business_record: bool = False
    source_url: str = Field(default="", max_length=1000)


class CivilTrialRunCreate(BaseModel):
    case_title: str = Field(min_length=3, max_length=300)
    case_summary: str = Field(min_length=20, max_length=12000)
    claim: str = Field(min_length=10, max_length=6000)
    defence: str = Field(min_length=10, max_length=6000)
    requested_relief: str = Field(min_length=3, max_length=2000)
    issues: list[TrialIssueInput] = Field(min_length=1, max_length=8)
    witnesses: list[TrialWitnessInput] = Field(min_length=2, max_length=10)
    exhibits: list[TrialExhibitInput] = Field(min_length=1, max_length=20)
    practice_side: Literal["plaintiff", "defendant", "observer"] = "plaintiff"

    @model_validator(mode="after")
    def validate_trial_record(self) -> "CivilTrialRunCreate":
        sides = {item.side for item in self.witnesses}
        if sides != {"plaintiff", "defendant"}:
            raise ValueError("原告和被告均至少需要一名证人。")
        names = [repair_text(item.name).casefold() for item in self.witnesses]
        if len(names) != len(set(names)):
            raise ValueError("证人姓名不能重复。")
        known = set(names)
        missing = sorted(
            item.foundation_witness for item in self.exhibits
            if repair_text(item.foundation_witness).casefold() not in known
        )
        if missing:
            raise ValueError("证物的基础证人不存在：" + "、".join(missing))
        max_issue = len(self.issues)
        for witness in self.witnesses:
            if any(index < 1 or index > max_issue for index in witness.issue_indexes):
                raise ValueError(f"证人 {witness.name} 引用了不存在的争点序号。")
        for exhibit in self.exhibits:
            if any(index < 1 or index > max_issue for index in exhibit.issue_indexes):
                raise ValueError(f"证物 {exhibit.title} 引用了不存在的争点序号。")
        return self


class CivilTrialActionInput(BaseModel):
    action: Literal["advance", "adjourn", "resume"] = "advance"
    content: str = Field(default="", max_length=6000)
    reason: str = Field(default="", max_length=1200)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return repair_text(value)


def _assert_tool(role: str, tool: str) -> None:
    if tool not in ROLE_TOOL_ALLOWLIST.get(role, set()):
        raise PermissionError(f"{role} is not permitted to call {tool}")


def _trace(state: dict[str, Any], role: str, tool: str, *, notes: list[str] | None = None) -> None:
    started = time.monotonic()
    _assert_tool(role, tool)
    state.setdefault("agent_trace", []).append({
        "role": role,
        "tool": tool,
        "status": "ok",
        "duration_ms": int((time.monotonic() - started) * 1000),
        "notes": notes or [],
        "created_at": _now(),
    })


def _authority_candidates(query: str) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(hybrid_search, query, module="canada", source_filter="case", limit=4, filters={"jurisdiction": "Ontario"}),
            executor.submit(hybrid_search, query, module="canada", source_filter="law", limit=4, filters={"jurisdiction": "Ontario"}),
        ]
        results = []
        for future in futures:
            try:
                results.append(future.result())
            except Exception:
                results.append({"items": []})
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        for item in result.get("items") or []:
            source_key = f"{item.get('source_table')}:{item.get('source_id')}:{item.get('chunk_id')}"
            if source_key in seen:
                continue
            seen.add(source_key)
            metadata = item.get("metadata") or {}
            candidates.append({
                "authority_id": f"AUTH-RAG-{len(candidates) + 1:03d}",
                "authority_type": _clean(item.get("source_kind")) or "unknown",
                "title": _clean(item.get("title")) or "未命名资料",
                "citation": _clean(item.get("citation") or metadata.get("citation")),
                "source_url": _clean(item.get("source_url")),
                "excerpt": _clean(item.get("excerpt"))[:900],
                "source_status": "retrieved_candidate",
            })
    return candidates[:8]


def ensure_civil_trial_tables() -> None:
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS civil_trial_runs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER,
                status TEXT NOT NULL, agenda_index INTEGER NOT NULL,
                case_title TEXT NOT NULL, state_json TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_civil_trial_runs_tenant_updated ON civil_trial_runs (tenant_id, updated_at DESC)",
        ]
    else:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS civil_trial_runs (
                id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, user_id BIGINT NULL,
                status TEXT NOT NULL, agenda_index INTEGER NOT NULL,
                case_title TEXT NOT NULL, state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_civil_trial_runs_tenant_updated ON civil_trial_runs (tenant_id, updated_at DESC)",
        ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _persist(state: dict[str, Any], *, user_id: int | None, tenant_id: str, is_new: bool = False) -> None:
    ensure_civil_trial_tables()
    params = {
        "id": state["id"], "tenant": tenant_id, "user": user_id, "status": state["status"],
        "agenda": state["agenda_index"], "title": state["case_title"],
        "state": json.dumps(state, ensure_ascii=False), "created": state["created_at"], "updated": state["updated_at"],
    }
    with engine.begin() as conn:
        if is_new and is_sqlite():
            sql = """INSERT INTO civil_trial_runs (id,tenant_id,user_id,status,agenda_index,case_title,state_json,created_at,updated_at) VALUES (:id,:tenant,:user,:status,:agenda,:title,:state,:created,:updated)"""
        elif is_new:
            sql = """INSERT INTO civil_trial_runs (id,tenant_id,user_id,status,agenda_index,case_title,state_json,created_at,updated_at) VALUES (CAST(:id AS uuid),:tenant,:user,:status,:agenda,:title,CAST(:state AS jsonb),:created,:updated)"""
        elif is_sqlite():
            sql = """UPDATE civil_trial_runs SET status=:status,agenda_index=:agenda,state_json=:state,updated_at=:updated WHERE id=:id AND tenant_id=:tenant"""
        else:
            sql = """UPDATE civil_trial_runs SET status=:status,agenda_index=:agenda,state_json=CAST(:state AS jsonb),updated_at=:updated WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant"""
        conn.execute(text(sql), params)


def _load(run_id: str, tenant_id: str) -> dict[str, Any]:
    ensure_civil_trial_tables()
    sql = "SELECT state_json FROM civil_trial_runs WHERE id=:id AND tenant_id=:tenant" if is_sqlite() else "SELECT state_json FROM civil_trial_runs WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant"
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"id": run_id, "tenant": tenant_id}).mappings().first()
    if not row:
        raise LookupError("未找到民事审判模拟记录或无权访问。")
    value = row["state_json"]
    return json.loads(value) if isinstance(value, str) else dict(value)


def _event(
    state: dict[str, Any], role: str, event_type: str, content: str, *,
    phase: str, witness_id: str = "", exhibit_id: str = "", tags: list[str] | None = None,
) -> dict[str, Any]:
    item = {
        "event_id": f"EV-{len(state['transcript']) + 1:04d}",
        "session_no": state["sessions"][-1]["session_no"],
        "phase": phase,
        "phase_label": PHASE_LABELS[phase],
        "role": role,
        "role_label": ROLE_LABELS[role],
        "event_type": event_type,
        "content": _clean(content),
        "witness_id": witness_id,
        "exhibit_id": exhibit_id,
        "tags": tags or [],
        "created_at": _now(),
    }
    state["transcript"].append(item)
    return item


def _build_record(payload: CivilTrialRunCreate) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    issues = [
        {"issue_id": f"I-{index:03d}", "description": _clean(item.description), "burden_side": item.burden_side}
        for index, item in enumerate(payload.issues, start=1)
    ]
    witness_name_map: dict[str, str] = {}
    witnesses: list[dict[str, Any]] = []
    side_counts = {"plaintiff": 0, "defendant": 0}
    for item in payload.witnesses:
        side_counts[item.side] += 1
        witness_id = f"W-{'P' if item.side == 'plaintiff' else 'D'}-{side_counts[item.side]:02d}"
        witness_name_map[_clean(item.name).casefold()] = witness_id
        witnesses.append({
            "witness_id": witness_id,
            "name": _clean(item.name),
            "side": item.side,
            "kind": item.kind,
            "role_description": _clean(item.role_description),
            "testimony_points": [_clean(value) for value in item.testimony_points],
            "issue_ids": [f"I-{value:03d}" for value in item.issue_indexes],
            "credibility_risks": [_clean(value) for value in item.credibility_risks],
            "expert_field": _clean(item.expert_field),
            "report_title": _clean(item.report_title),
            "status": "waiting",
            "credibility_score": round(max(0.45, 0.9 - (0.09 * len(item.credibility_risks))), 2),
            "testimony_event_ids": [],
        })
    exhibits: list[dict[str, Any]] = []
    exhibit_counts = {"plaintiff": 0, "defendant": 0}
    for item in payload.exhibits:
        exhibit_counts[item.proponent] += 1
        exhibit_id = f"EX-{'P' if item.proponent == 'plaintiff' else 'D'}-{exhibit_counts[item.proponent]:02d}"
        exhibits.append({
            "exhibit_id": exhibit_id,
            "title": _clean(item.title),
            "description": _clean(item.description),
            "proponent": item.proponent,
            "foundation_witness_id": witness_name_map[_clean(item.foundation_witness).casefold()],
            "issue_ids": [f"I-{value:03d}" for value in item.issue_indexes],
            "authenticity": item.authenticity,
            "hearsay_risk": item.hearsay_risk,
            "business_record": item.business_record,
            "source_url": _clean(item.source_url),
            "status": "proposed",
            "limitation": "",
            "objection_id": "",
        })
    return issues, witnesses, exhibits


def _build_agenda(witnesses: list[dict[str, Any]], exhibits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    agenda: list[dict[str, Any]] = [
        {"phase": "case_management", "task": "call_case", "role": "court_clerk"},
        {"phase": "opening", "task": "opening", "role": "plaintiff_counsel", "side": "plaintiff"},
        {"phase": "opening", "task": "opening", "role": "defendant_counsel", "side": "defendant"},
    ]
    for side in ("plaintiff", "defendant"):
        phase = f"{side}_evidence"
        for witness in [item for item in witnesses if item["side"] == side]:
            witness_id = witness["witness_id"]
            agenda.append({"phase": phase, "task": "call_witness", "role": "court_clerk", "witness_id": witness_id})
            if witness["kind"] == "expert":
                agenda.append({"phase": phase, "task": "qualify_expert", "role": f"{side}_counsel", "witness_id": witness_id})
            foundation_exhibits = [item for item in exhibits if item["foundation_witness_id"] == witness_id]
            for point_index, _point in enumerate(witness["testimony_points"]):
                task = {"phase": phase, "task": "direct_round", "role": f"{side}_counsel", "witness_id": witness_id, "point_index": point_index}
                if point_index < len(foundation_exhibits):
                    task["exhibit_id"] = foundation_exhibits[point_index]["exhibit_id"]
                agenda.append(task)
            cross_rounds = max(1, min(2, len(witness["credibility_risks"]) or 1))
            for risk_index in range(cross_rounds):
                agenda.append({"phase": phase, "task": "cross_round", "role": f"{'defendant' if side == 'plaintiff' else 'plaintiff'}_counsel", "witness_id": witness_id, "risk_index": risk_index})
            agenda.append({"phase": phase, "task": "reexamination", "role": f"{side}_counsel", "witness_id": witness_id})
            agenda.append({"phase": phase, "task": "release_witness", "role": "trial_judge", "witness_id": witness_id})
        agenda.append({"phase": phase, "task": "side_rests", "role": f"{side}_counsel", "side": side})
        agenda.append({"phase": phase, "task": "session_break", "role": "trial_judge", "reason": f"{ROLE_LABELS[f'{side}_counsel']}举证结束"})
    agenda.extend([
        {"phase": "reply_evidence", "task": "reply_review", "role": "trial_judge"},
        {"phase": "closing", "task": "closing", "role": "plaintiff_counsel", "side": "plaintiff"},
        {"phase": "closing", "task": "closing", "role": "defendant_counsel", "side": "defendant"},
        {"phase": "closing", "task": "reply_closing", "role": "plaintiff_counsel", "side": "plaintiff"},
        {"phase": "closing", "task": "session_break", "role": "trial_judge", "reason": "法庭保留判决"},
        {"phase": "judgment", "task": "judgment", "role": "trial_judge"},
    ])
    return agenda


def _find(state: dict[str, Any], collection: str, key: str, value: str) -> dict[str, Any]:
    for item in state[collection]:
        if item[key] == value:
            return item
    raise ValueError(f"Unknown {key}: {value}")


def _witness_role(witness: dict[str, Any]) -> str:
    if witness["kind"] == "expert":
        return "expert_witness"
    return f"{witness['side']}_witness"


def _counsel_text(state: dict[str, Any], task: dict[str, Any], override: str) -> str:
    if override and state["practice_side"] != "observer" and task.get("side", task["role"].removesuffix("_counsel")) == state["practice_side"]:
        return _clean(override)
    if task["task"] == "opening":
        return state["claim"] if task["side"] == "plaintiff" else state["defence"]
    if task["task"] == "closing":
        side = task["side"]
        admitted = [item["exhibit_id"] for item in state["exhibits"] if item["proponent"] == side and item["status"] in {"admitted", "limited"}]
        witnesses = [item["name"] for item in state["witnesses"] if item["side"] == side and item["status"] == "completed"]
        position = state["claim"] if side == "plaintiff" else state["defence"]
        return f"本方依据已记录的证言（{'、'.join(witnesses)}）和证物（{'、'.join(admitted) or '无获准证物'}）主张：{position}"
    if task["task"] == "reply_closing":
        return "原告仅回应被告结案陈词中关于证据缺口的意见，不提出新事实，并维持已陈述的救济请求。"
    return ""


def _rule_exhibit(state: dict[str, Any], exhibit: dict[str, Any], witness: dict[str, Any], phase: str) -> None:
    _trace(state, "evidence_officer", "foundation_check", notes=[exhibit["exhibit_id"], witness["witness_id"]])
    _trace(state, "evidence_officer", "source_validation", notes=[exhibit["authenticity"]])
    _trace(state, "evidence_officer", "exhibit_registry", notes=[exhibit["exhibit_id"]])
    opposing = "defendant_counsel" if exhibit["proponent"] == "plaintiff" else "plaintiff_counsel"
    grounds: list[str] = []
    if exhibit["foundation_witness_id"] != witness["witness_id"]:
        grounds.append("缺少适格基础证人")
    if exhibit["authenticity"] in {"disputed", "unknown"}:
        grounds.append("真实性基础不足")
    if exhibit["hearsay_risk"] and not exhibit["business_record"]:
        grounds.append("传闻证据")
    if exhibit["hearsay_risk"] and exhibit["business_record"]:
        grounds.append("传闻及商业记录条件")
    objection_id = f"OBJ-{len(state['objections']) + 1:03d}"
    if grounds:
        _trace(state, opposing, "raise_objection", notes=[exhibit["exhibit_id"], *grounds])
        _event(state, opposing, "objection", f"对 {exhibit['exhibit_id']} 提出异议：{'；'.join(grounds)}。", phase=phase, exhibit_id=exhibit["exhibit_id"])
    if "缺少适格基础证人" in grounds or "真实性基础不足" in grounds:
        ruling, status, limitation = "异议成立，证物不准入。", "excluded", "不得用于事实认定"
    elif "传闻证据" in grounds:
        ruling, status, limitation = "异议部分成立，证物仅用于证明通知或沟通过程，不用于证明其中陈述为真。", "limited", "不得用于证明所载陈述为真"
    else:
        ruling, status, limitation = "异议驳回，证物准入。" if grounds else "基础已经建立，证物准入。", "admitted", ""
    _trace(state, "trial_judge", "rule_objection", notes=[exhibit["exhibit_id"], status])
    _event(state, "trial_judge", "evidence_ruling", f"就 {exhibit['exhibit_id']}：{ruling}", phase=phase, exhibit_id=exhibit["exhibit_id"], tags=[status])
    exhibit["status"] = status
    exhibit["limitation"] = limitation
    if grounds:
        exhibit["objection_id"] = objection_id
        state["objections"].append({
            "objection_id": objection_id, "session_no": state["sessions"][-1]["session_no"],
            "raised_by": opposing, "exhibit_id": exhibit["exhibit_id"], "grounds": grounds,
            "ruling": status, "reasons": ruling,
        })


def _execute_task(state: dict[str, Any], task: dict[str, Any], override: str) -> None:
    phase = task["phase"]
    role = task["role"]
    name = ROLE_LABELS[role]
    if task["task"] == "call_case":
        _trace(state, "court_clerk", "workflow_state")
        _trace(state, "court_clerk", "session_control")
        _event(state, role, "case_called", f"安大略高等法院民事审判现开庭审理：{state['case_title']}。本次记录采用优势证据标准。", phase=phase)
    elif task["task"] == "opening":
        _trace(state, role, "record_read")
        _trace(state, role, "authority_read")
        _event(state, role, "opening_statement", _counsel_text(state, task, override), phase=phase)
    elif task["task"] == "call_witness":
        witness = _find(state, "witnesses", "witness_id", task["witness_id"])
        _trace(state, "court_clerk", "witness_registry", notes=[witness["witness_id"]])
        witness["status"] = "testifying"
        _event(state, role, "witness_called", f"传唤 {witness['name']}（{witness['role_description']}）。证人确认将如实作证。", phase=phase, witness_id=witness["witness_id"])
    elif task["task"] == "qualify_expert":
        witness = _find(state, "witnesses", "witness_id", task["witness_id"])
        opposing = "defendant_counsel" if witness["side"] == "plaintiff" else "plaintiff_counsel"
        _trace(state, role, "question_witness", notes=["expert_qualification"])
        _trace(state, "expert_witness", "expert_report_read", notes=[witness["report_title"]])
        _event(state, role, "qualification_request", f"申请将 {witness['name']} 认定为 {witness['expert_field']} 专家，意见范围限于《{witness['report_title']}》。", phase=phase, witness_id=witness["witness_id"])
        _trace(state, opposing, "question_witness", notes=["expert_voir_dire"])
        _event(state, opposing, "qualification_voir_dire", f"对专家资格不作全面反对，但要求其不得评价法律结论或超出报告范围。", phase=phase, witness_id=witness["witness_id"])
        _trace(state, "trial_judge", "control_hearing", notes=["expert_qualified"])
        _event(state, "trial_judge", "qualification_ruling", f"准许 {witness['name']} 在 {witness['expert_field']} 范围内提供意见；报告外意见不进入记录。", phase=phase, witness_id=witness["witness_id"])
    elif task["task"] == "direct_round":
        witness = _find(state, "witnesses", "witness_id", task["witness_id"])
        point = witness["testimony_points"][task["point_index"]]
        question = override if override and state["practice_side"] == witness["side"] else f"请说明与你的职责有关的第 {task['point_index'] + 1} 项亲历事实。"
        _trace(state, role, "record_read", notes=[witness["witness_id"]])
        _trace(state, role, "question_witness", notes=["direct", witness["witness_id"]])
        _event(state, role, "direct_question", question, phase=phase, witness_id=witness["witness_id"])
        witness_role = _witness_role(witness)
        _trace(state, witness_role, "expert_report_read" if witness["kind"] == "expert" else "personal_record_read", notes=[witness["witness_id"]])
        _trace(state, witness_role, "answer_question", notes=["direct"])
        answer = point if witness["kind"] == "fact" else f"根据《{witness['report_title']}》，{point}"
        testimony = _event(state, witness_role, "direct_answer", answer, phase=phase, witness_id=witness["witness_id"], tags=witness["issue_ids"])
        witness["testimony_event_ids"].append(testimony["event_id"])
        if task.get("exhibit_id"):
            exhibit = _find(state, "exhibits", "exhibit_id", task["exhibit_id"])
            _trace(state, role, "tender_exhibit", notes=[exhibit["exhibit_id"]])
            _event(state, role, "exhibit_tendered", f"通过 {witness['name']} 提交 {exhibit['exhibit_id']}《{exhibit['title']}》：{exhibit['description']}", phase=phase, witness_id=witness["witness_id"], exhibit_id=exhibit["exhibit_id"])
            _rule_exhibit(state, exhibit, witness, phase)
    elif task["task"] == "cross_round":
        witness = _find(state, "witnesses", "witness_id", task["witness_id"])
        risks = witness["credibility_risks"] or ["该证言没有独立文件完整印证"]
        risk = risks[min(task["risk_index"], len(risks) - 1)]
        question = override if override and state["practice_side"] == role.removesuffix("_counsel") else f"你同意以下情况会限制证言的可靠性吗：{risk}？"
        _trace(state, role, "record_read", notes=[witness["witness_id"]])
        _trace(state, role, "question_witness", notes=["cross", witness["witness_id"]])
        _event(state, role, "cross_question", question, phase=phase, witness_id=witness["witness_id"])
        witness_role = _witness_role(witness)
        _trace(state, witness_role, "answer_question", notes=["cross"])
        answer = f"我承认这一限制：{risk}。我只能确认自己亲历或报告中已经记录的部分。"
        testimony = _event(state, witness_role, "cross_answer", answer, phase=phase, witness_id=witness["witness_id"], tags=["credibility_challenge"])
        witness["testimony_event_ids"].append(testimony["event_id"])
    elif task["task"] == "reexamination":
        witness = _find(state, "witnesses", "witness_id", task["witness_id"])
        _trace(state, role, "question_witness", notes=["reexamination_limited"])
        _event(state, role, "reexamination_question", "只针对交叉询问提出的可靠性限制：哪些内容来自你本人观察或已提交报告？", phase=phase, witness_id=witness["witness_id"])
        witness_role = _witness_role(witness)
        _trace(state, witness_role, "answer_question", notes=["reexamination"])
        answer = "我的回答限于前述亲历事实和已经确认的文件；对未亲历部分不作推测。"
        testimony = _event(state, witness_role, "reexamination_answer", answer, phase=phase, witness_id=witness["witness_id"], tags=["no_new_matter"])
        witness["testimony_event_ids"].append(testimony["event_id"])
    elif task["task"] == "release_witness":
        witness = _find(state, "witnesses", "witness_id", task["witness_id"])
        _trace(state, "trial_judge", "control_hearing", notes=[witness["witness_id"]])
        witness["status"] = "completed"
        _event(state, role, "witness_released", f"{witness['name']} 的主询问、交叉询问和复询已经完成，证人退庭。", phase=phase, witness_id=witness["witness_id"])
    elif task["task"] == "side_rests":
        _trace(state, role, "record_read", notes=["side_rests"])
        _event(state, role, "side_rests", f"{name}举证完毕。", phase=phase)
    elif task["task"] == "reply_review":
        _trace(state, "trial_judge", "transcript_read")
        _trace(state, "trial_judge", "control_hearing", notes=["reply_evidence"])
        _event(state, role, "reply_evidence_ruling", "被告证据未提出无法预见的新事项，不准原告借回复证据重开本案；原告可在结案陈词中回应证明力。", phase=phase)
    elif task["task"] in {"closing", "reply_closing"}:
        _trace(state, role, "record_read", notes=[task["task"]])
        _trace(state, role, "authority_read", notes=[task["task"]])
        _event(state, role, task["task"], _counsel_text(state, task, override), phase=phase)
    elif task["task"] == "judgment":
        _build_judgment_and_scores(state)
    else:
        raise ValueError(f"Unsupported trial task: {task['task']}")


def _advocacy_score(state: dict[str, Any], side: str) -> dict[str, Any]:
    counsel = f"{side}_counsel"
    events = [item for item in state["transcript"] if item["role"] == counsel]
    side_exhibits = [item for item in state["exhibits"] if item["proponent"] == side]
    admitted = [item for item in side_exhibits if item["status"] in {"admitted", "limited"}]
    witness_events = [item for item in events if item["event_type"] in {"direct_question", "cross_question", "reexamination_question"}]
    objections = [item for item in state["objections"] if item["raised_by"] == counsel]
    sustained = [item for item in objections if item["ruling"] in {"excluded", "limited"}]
    metrics = {
        "theory_consistency": 15 if any(item["event_type"] == "opening_statement" for item in events) else 5,
        "evidence_foundation": round(20 * len(admitted) / max(1, len(side_exhibits))),
        "examination_quality": min(20, 8 + (2 * len(witness_events))),
        "objection_handling": min(15, 8 + (3 * len(sustained)) + len(objections)),
        "legal_application": 15 if state["authorities"] else 6,
        "closing_record_fidelity": 15 if any(item["event_type"] == "closing" and "EX-" in item["content"] for item in events) else 10,
    }
    corrections = []
    excluded = [item["exhibit_id"] for item in side_exhibits if item["status"] == "excluded"]
    if excluded:
        corrections.append(f"在提交 {'、'.join(excluded)} 前先补足真实性和基础证人，避免证物被排除。")
    limited = [item["exhibit_id"] for item in side_exhibits if item["status"] == "limited"]
    if limited:
        corrections.append(f"不要把 {'、'.join(limited)} 用于其受限目的之外；改由有亲历知识的证人证明核心事实。")
    if not objections:
        corrections.append("盘问时应及时识别传闻、缺乏基础或超出专家范围的问题，并说明具体异议理由。")
    return {"side": side, "metrics": metrics, "total": sum(metrics.values()), "corrections": corrections}


def _build_judgment_and_scores(state: dict[str, Any]) -> None:
    _trace(state, "trial_judge", "transcript_read")
    _trace(state, "trial_judge", "authority_read")
    _trace(state, "trial_judge", "build_judgment")
    findings = []
    for issue in state["issues"]:
        support: dict[str, float] = {"plaintiff": 0.0, "defendant": 0.0}
        accepted: list[str] = []
        accepted_by_side: dict[str, list[str]] = {"plaintiff": [], "defendant": []}
        rejected: list[str] = []
        for exhibit in state["exhibits"]:
            if issue["issue_id"] not in exhibit["issue_ids"]:
                continue
            if exhibit["status"] == "admitted":
                support[exhibit["proponent"]] += 1.0
                accepted.append(exhibit["exhibit_id"])
                accepted_by_side[exhibit["proponent"]].append(exhibit["exhibit_id"])
            elif exhibit["status"] == "limited":
                support[exhibit["proponent"]] += 0.35
                accepted.append(f"{exhibit['exhibit_id']}（限缩用途）")
                accepted_by_side[exhibit["proponent"]].append(f"{exhibit['exhibit_id']}（限缩用途）")
            else:
                rejected.append(exhibit["exhibit_id"])
        credibility = []
        for witness in state["witnesses"]:
            if issue["issue_id"] not in witness["issue_ids"] or witness["status"] != "completed":
                continue
            support[witness["side"]] += witness["credibility_score"]
            credibility.append({
                "witness_id": witness["witness_id"], "name": witness["name"],
                "score": witness["credibility_score"],
                "reason": "按亲历范围采信" if not witness["credibility_risks"] else "结合交叉询问暴露的限制后部分采信",
            })
        burden = issue["burden_side"]
        other = "defendant" if burden == "plaintiff" else "plaintiff"
        met = support[burden] > support[other] and support[burden] >= 1.0
        burden_witnesses = [item["name"] for item in credibility if _find(state, "witnesses", "witness_id", item["witness_id"])["side"] == burden]
        burden_record = "、".join(accepted_by_side[burden] + burden_witnesses) or "现有口头证言"
        opposing_record = "、".join(accepted_by_side[other]) or "对方口头证言"
        reasons = (
            f"法庭在交叉询问后采信 {burden_record} 的相关部分。{opposing_record} "
            + ("不足以动摇该证据链，承担证明责任的一方已按优势证据标准完成证明。" if met else "形成同等或更有说服力的反证，承担证明责任的一方未按优势证据标准完成证明。")
        )
        findings.append({
            "issue_id": issue["issue_id"], "description": issue["description"], "burden_side": burden,
            "burden_met": met, "finding_for": burden if met else other,
            "standard": "balance_of_probabilities",
            "accepted_evidence": accepted, "excluded_or_rejected_evidence": rejected,
            "witness_credibility": credibility,
            "reasons": reasons,
        })
    plaintiff_wins = sum(1 for item in findings if item["finding_for"] == "plaintiff")
    defendant_wins = len(findings) - plaintiff_wins
    if plaintiff_wins == len(findings):
        outcome = "原告诉请成立"
        order = state["requested_relief"]
    elif plaintiff_wins == 0:
        outcome = "原告诉请驳回"
        order = "驳回原告诉请；费用问题由双方另行提交书面意见。"
    else:
        outcome = "原告部分胜诉"
        order = f"仅就原告已完成证明责任的争点给予相应救济；原请求为：{state['requested_relief']}。具体金额另行核算。"
    state["judgment"] = {
        "outcome": outcome,
        "order": order,
        "issue_findings": findings,
        "evidence_rule": "只依据获准证物、完成主询问与交叉询问的证言，以及记录内法律依据。",
        "costs_direction": "双方可在收到判决后按法院指定期限提交费用意见。",
        "authorities": [item["authority_id"] for item in state["authorities"]],
    }
    _event(state, "trial_judge", "judgment", f"判决：{outcome}。{order}", phase="judgment", tags=[item["issue_id"] for item in findings])
    _trace(state, "evaluation_agent", "transcript_read")
    _trace(state, "evaluation_agent", "score_advocacy")
    scores = {side: _advocacy_score(state, side) for side in ("plaintiff", "defendant")}
    _trace(state, "evaluation_agent", "build_report")
    state["final_report"] = {
        "judicial_result": state["judgment"],
        "advocacy_scores": scores,
        "record_metrics": {
            "sessions": len(state["sessions"]),
            "witnesses_completed": sum(item["status"] == "completed" for item in state["witnesses"]),
            "testimony_events": sum(item["event_type"].endswith("answer") for item in state["transcript"]),
            "exhibits_admitted": sum(item["status"] == "admitted" for item in state["exhibits"]),
            "exhibits_limited": sum(item["status"] == "limited" for item in state["exhibits"]),
            "exhibits_excluded": sum(item["status"] == "excluded" for item in state["exhibits"]),
            "objections_ruled": len(state["objections"]),
            "role_boundary_violations": len(state["boundary_violations"]),
        },
        "score_separation": "律师训练分只评价庭审技能；判决按各争点的证明责任和获准记录作出，两者互不换算。",
        "probability": None,
        "display_probability": False,
        "disclaimer": DISCLAIMER,
    }


def _public_view(state: dict[str, Any]) -> dict[str, Any]:
    result = dict(state)
    agenda = state["agenda"]
    index = state["agenda_index"]
    result["active_task"] = agenda[index] if index < len(agenda) else None
    result["active_phase"] = agenda[index]["phase"] if index < len(agenda) else "judgment"
    result["active_phase_label"] = PHASE_LABELS[result["active_phase"]]
    result["progress"] = round(min(1.0, index / max(1, len(agenda))), 4)
    result["role_boundaries"] = ROLE_BOUNDARIES
    result["role_tool_allowlist"] = {role: sorted(tools) for role, tools in ROLE_TOOL_ALLOWLIST.items()}
    result["phase_nodes"] = [
        {
            "phase": phase,
            "label": label,
            "status": "completed" if phase in state["completed_phases"] else "active" if phase == result["active_phase"] and state["status"] != "completed" else "pending",
        }
        for phase, label in PHASE_LABELS.items()
    ]
    result["disclaimer"] = DISCLAIMER
    return result


def create_civil_trial_run(payload: CivilTrialRunCreate, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    issues, witnesses, exhibits = _build_record(payload)
    authorities = [dict(item) for item in PROCEDURAL_AUTHORITIES]
    retrieved = _authority_candidates(" ".join([payload.case_title, payload.claim, payload.defence]))
    existing_urls = {item["source_url"] for item in authorities}
    authorities.extend(item for item in retrieved if item["source_url"] not in existing_urls)
    now = _now()
    state: dict[str, Any] = {
        "id": str(uuid4()), "workflow_version": WORKFLOW_VERSION, "status": "active",
        "case_title": _clean(payload.case_title), "case_summary": _clean(payload.case_summary),
        "claim": _clean(payload.claim), "defence": _clean(payload.defence),
        "requested_relief": _clean(payload.requested_relief), "practice_side": payload.practice_side,
        "issues": issues, "witnesses": witnesses, "exhibits": exhibits, "authorities": authorities,
        "agenda": _build_agenda(witnesses, exhibits), "agenda_index": 0, "completed_phases": [],
        "sessions": [{"session_no": 1, "status": "active", "opened_at": now, "closed_at": "", "reason": "开庭"}],
        "transcript": [], "objections": [], "agent_trace": [], "boundary_violations": [],
        "judgment": None, "final_report": None, "created_at": now, "updated_at": now,
    }
    _trace(state, "legal_research_agent", "hybrid_search", notes=[f"candidates={len(retrieved)}"])
    _trace(state, "legal_research_agent", "authority_read", notes=[f"authorities={len(authorities)}"])
    _trace(state, "court_clerk", "exhibit_registry", notes=[f"exhibits={len(exhibits)}"])
    _trace(state, "court_clerk", "witness_registry", notes=[f"witnesses={len(witnesses)}"])
    _persist(state, user_id=user_id, tenant_id=tenant_id, is_new=True)
    return _public_view(state)


def get_civil_trial_run(run_id: str, *, tenant_id: str) -> dict[str, Any]:
    return _public_view(_load(run_id, tenant_id))


def _adjourn(state: dict[str, Any], reason: str) -> None:
    if state["status"] != "active":
        raise ValueError("只有进行中的庭审可以休庭。")
    session = state["sessions"][-1]
    session["status"] = "adjourned"
    session["closed_at"] = _now()
    session["reason"] = _clean(reason) or "按审判日程休庭"
    _trace(state, "trial_judge", "control_hearing", notes=["adjourn"])
    _trace(state, "court_clerk", "session_control", notes=["adjourn"])
    _event(state, "trial_judge", "adjournment", f"法庭休庭：{session['reason']}。续庭后从当前证据记录继续。", phase=state["agenda"][state["agenda_index"]]["phase"])
    state["status"] = "adjourned"


def _resume(state: dict[str, Any]) -> None:
    if state["status"] != "adjourned":
        raise ValueError("当前记录不在休庭状态。")
    session_no = len(state["sessions"]) + 1
    state["sessions"].append({"session_no": session_no, "status": "active", "opened_at": _now(), "closed_at": "", "reason": "续庭"})
    state["status"] = "active"
    _trace(state, "court_clerk", "session_control", notes=["resume", f"session={session_no}"])
    phase = state["agenda"][state["agenda_index"]]["phase"]
    _event(state, "court_clerk", "session_resumed", f"第 {session_no} 庭次续庭，书记员确认此前证言、证物编号和异议裁定继续有效。", phase=phase)


def perform_civil_trial_action(
    run_id: str, payload: CivilTrialActionInput, *, user_id: int | None, tenant_id: str,
) -> dict[str, Any]:
    state = _load(run_id, tenant_id)
    if state["status"] == "completed":
        raise ValueError("庭审已经完成，不能继续推进。")
    if payload.action == "resume":
        _resume(state)
    elif payload.action == "adjourn":
        _adjourn(state, payload.reason)
    else:
        if state["status"] == "adjourned":
            raise ValueError("当前处于休庭状态，必须先续庭。")
        if state["agenda_index"] >= len(state["agenda"]):
            raise ValueError("庭审议程已经完成。")
        task = state["agenda"][state["agenda_index"]]
        current_phase = task["phase"]
        if task["task"] == "session_break":
            state["agenda_index"] += 1
            next_phase = state["agenda"][state["agenda_index"]]["phase"] if state["agenda_index"] < len(state["agenda"]) else None
            if next_phase != current_phase and current_phase not in state["completed_phases"]:
                state["completed_phases"].append(current_phase)
            _adjourn(state, task["reason"])
        else:
            _execute_task(state, task, payload.content)
            state["agenda_index"] += 1
            next_phase = state["agenda"][state["agenda_index"]]["phase"] if state["agenda_index"] < len(state["agenda"]) else None
            if next_phase != current_phase and current_phase not in state["completed_phases"]:
                state["completed_phases"].append(current_phase)
            if task["task"] == "judgment":
                state["status"] = "completed"
                state["sessions"][-1]["status"] = "completed"
                state["sessions"][-1]["closed_at"] = _now()
                if "judgment" not in state["completed_phases"]:
                    state["completed_phases"].append("judgment")
    state["updated_at"] = _now()
    _persist(state, user_id=user_id, tenant_id=tenant_id)
    return _public_view(state)
