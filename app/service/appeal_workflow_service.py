from __future__ import annotations

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field
from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.llm_service import LLMServiceError, create_structured_response, is_llm_configured


WORKFLOW_VERSION = "ontario-civil-appeal-1.0"
DISCLAIMER = (
    "这是安大略民事上诉训练模拟。评分只反映本次陈述对案卷、法律依据和程序要求的覆盖情况，"
    "不代表法院意见、案件结果或胜诉概率。"
)

APPEAL_STAGES = [
    "record_review",
    "appellant_main",
    "judge_question_appellant",
    "appellant_answer",
    "respondent_main",
    "judge_question_respondent",
    "respondent_answer",
    "appellant_reply",
    "panel_report",
]

STAGE_DEFINITIONS: dict[str, dict[str, str]] = {
    "record_review": {"label": "案卷审查", "speaker": "court_clerk", "focus": "确认上诉问题、案卷范围、请求裁定和候选法律依据。"},
    "appellant_main": {"label": "上诉方主要陈述", "speaker": "appellant_counsel", "focus": "说明可审查错误、审查标准、案卷依据和请求裁定。"},
    "judge_question_appellant": {"label": "合议庭向上诉方提问", "speaker": "judge_panel", "focus": "针对审查标准、案卷支持或决定性影响提出问题。"},
    "appellant_answer": {"label": "上诉方回答", "speaker": "appellant_counsel", "focus": "直接回答合议庭问题，不引入案卷外事实。"},
    "respondent_main": {"label": "被上诉方主要陈述", "speaker": "respondent_counsel", "focus": "回应上诉理由，说明原决定为何应受尊重或维持。"},
    "judge_question_respondent": {"label": "合议庭向被上诉方提问", "speaker": "judge_panel", "focus": "检验被上诉方对错误、影响和救济的回应。"},
    "respondent_answer": {"label": "被上诉方回答", "speaker": "respondent_counsel", "focus": "直接回答合议庭问题并回到案卷和法律依据。"},
    "appellant_reply": {"label": "上诉方答复", "speaker": "appellant_counsel", "focus": "只回应被上诉方提出的新点，不重做主要陈述。"},
    "panel_report": {"label": "合议庭训练报告", "speaker": "evaluation_service", "focus": "汇总双方表现、引用缺口和可执行修正方案。"},
}

ROLE_TOOL_ALLOWLIST = {
    "court_clerk": {"workflow_state", "record_read", "authority_read"},
    "appellant_counsel": {"record_read", "authority_read", "transcript_read"},
    "respondent_counsel": {"record_read", "authority_read", "transcript_read"},
    "judge_panel": {"record_read", "authority_read", "transcript_read", "question_issue"},
    "verification_service": {"reference_validation"},
    "evaluation_service": {"score_turn", "build_report"},
}

COUNSEL_ROLES = {"appellant_counsel", "respondent_counsel"}
QUESTION_STAGES = {"judge_question_appellant", "judge_question_respondent"}
ANSWER_STAGES = {"appellant_answer", "respondent_answer"}


class AppealRunCreate(BaseModel):
    case_title: str = Field(min_length=3, max_length=300)
    case_summary: str = Field(min_length=20, max_length=20000)
    lower_court_decision: str = Field(min_length=10, max_length=12000)
    grounds_of_appeal: str = Field(min_length=10, max_length=12000)
    requested_order: str = Field(min_length=2, max_length=2000)
    record_materials: str = Field(default="", max_length=20000)
    practice_role: Literal["appellant", "respondent", "observer"] = "appellant"


class AppealAdvanceInput(BaseModel):
    content: str = Field(default="", max_length=12000)
    issue_ids: list[str] = Field(default_factory=list, max_length=12)
    record_ids: list[str] = Field(default_factory=list, max_length=24)
    authority_ids: list[str] = Field(default_factory=list, max_length=24)


class AppealClaim(BaseModel):
    statement: str = Field(min_length=1, max_length=1600)
    record_ids: list[str] = Field(default_factory=list, max_length=12)
    authority_ids: list[str] = Field(default_factory=list, max_length=12)


class AppealAgentTurn(BaseModel):
    speaker_role: Literal["appellant_counsel", "respondent_counsel", "judge_panel"]
    content: str = Field(min_length=1, max_length=5000)
    issue_ids: list[str] = Field(default_factory=list, max_length=12)
    claims: list[AppealClaim] = Field(default_factory=list, max_length=10)
    record_ids: list[str] = Field(default_factory=list, max_length=24)
    authority_ids: list[str] = Field(default_factory=list, max_length=24)
    responds_to: list[str] = Field(default_factory=list, max_length=8)
    question_ids: list[str] = Field(default_factory=list, max_length=8)
    requested_order: str = Field(default="", max_length=1200)
    stop_reason: Literal["turn_complete", "insufficient_record", "insufficient_authority"] = "turn_complete"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return repair_text(value)


def _split_items(value: str, limit: int) -> list[str]:
    parts = [_clean(item) for item in re.split(r"[\n；;]+", value) if _clean(item)]
    return parts[:limit]


def _expected_standard(value: str) -> tuple[str, str]:
    lowered = value.lower()
    if any(
        term in lowered
        for term in (
            "法律错误",
            "法律问题",
            "法律测试",
            "法律适用",
            "适用法律",
            "法律原则",
            "错误解释",
            "解释法律",
            "error of law",
            "incorrect test",
            "wrong legal test",
            "correctness",
        )
    ):
        return "question_of_law", "correctness"
    if any(term in lowered for term in ("事实认定", "证人可信", "证据权重", "finding of fact", "credibility", "factual finding")):
        return "question_of_fact", "palpable_and_overriding_error"
    return "mixed_fact_and_law", "palpable_and_overriding_error"


def _build_issues(grounds: str) -> list[dict[str, str]]:
    values = _split_items(grounds, 8) or [_clean(grounds)]
    issues = []
    for index, value in enumerate(values, start=1):
        question_type, standard = _expected_standard(value)
        issues.append({
            "issue_id": f"I-{index:03d}",
            "description": value,
            "question_type": question_type,
            "expected_standard": standard,
        })
    return issues


def _build_record(payload: AppealRunCreate) -> list[dict[str, str]]:
    values = [
        ("case_summary", "案情摘要", payload.case_summary),
        ("lower_court_decision", "原审决定", payload.lower_court_decision),
        ("grounds_of_appeal", "上诉理由", payload.grounds_of_appeal),
        ("requested_order", "请求裁定", payload.requested_order),
    ]
    for index, material in enumerate(_split_items(payload.record_materials, 30), start=1):
        values.append(("record_material", f"案卷材料 {index}", material))
    return [
        {"record_id": f"R-{index:03d}", "record_type": kind, "title": title, "content": _clean(content)}
        for index, (kind, title, content) in enumerate(values, start=1)
    ]


def _authority_candidates(query: str) -> list[dict[str, Any]]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        case_future = executor.submit(hybrid_search, query, module="canada", source_filter="case", limit=6, filters={"jurisdiction": "Ontario"})
        law_future = executor.submit(hybrid_search, query, module="canada", source_filter="law", limit=6, filters={"jurisdiction": "Ontario"})
        results = []
        for future in (case_future, law_future):
            try:
                results.append(future.result())
            except Exception:
                results.append({"items": []})
    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    for result in results:
        for item in result.get("items") or []:
            source_key = f"{item.get('source_table')}:{item.get('source_id')}:{item.get('chunk_id')}"
            if source_key in seen:
                continue
            seen.add(source_key)
            metadata = item.get("metadata") or {}
            output.append({
                "authority_id": f"A-{len(output) + 1:03d}",
                "source_key": source_key,
                "authority_type": _clean(item.get("source_kind")) or "unknown",
                "title": _clean(item.get("title")) or "未命名资料",
                "citation": _clean(item.get("citation") or metadata.get("citation")),
                "court": _clean(item.get("court_level") or metadata.get("court")),
                "decision_date": _clean(item.get("published_at")),
                "source_url": _clean(item.get("source_url")),
                "excerpt": _clean(item.get("excerpt"))[:900],
                "score": round(float(item.get("score") or 0), 4),
            })
    return output[:12]


def ensure_appeal_workflow_tables() -> None:
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS appeal_runs (
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, user_id INTEGER,
                status TEXT NOT NULL, active_stage_index INTEGER NOT NULL,
                case_title TEXT NOT NULL, state_json TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_appeal_runs_tenant_updated ON appeal_runs (tenant_id, updated_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS appeal_checkpoints (
                id TEXT PRIMARY KEY, run_id TEXT NOT NULL, tenant_id TEXT NOT NULL,
                stage_index INTEGER NOT NULL, version INTEGER NOT NULL,
                state_json TEXT NOT NULL, created_at TIMESTAMP NOT NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_appeal_checkpoints_run_stage ON appeal_checkpoints (run_id, tenant_id, stage_index, version DESC)",
        ]
    else:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS appeal_runs (
                id UUID PRIMARY KEY, tenant_id TEXT NOT NULL, user_id BIGINT NULL,
                status TEXT NOT NULL, active_stage_index INTEGER NOT NULL,
                case_title TEXT NOT NULL, state_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_appeal_runs_tenant_updated ON appeal_runs (tenant_id, updated_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS appeal_checkpoints (
                id UUID PRIMARY KEY, run_id UUID NOT NULL REFERENCES appeal_runs(id) ON DELETE CASCADE,
                tenant_id TEXT NOT NULL, stage_index INTEGER NOT NULL, version INTEGER NOT NULL,
                state_json JSONB NOT NULL, created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_appeal_checkpoints_run_stage ON appeal_checkpoints (run_id, tenant_id, stage_index, version DESC)",
        ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _persist(state: dict[str, Any], *, user_id: int | None, tenant_id: str, is_new: bool = False) -> None:
    ensure_appeal_workflow_tables()
    state_json = json.dumps(state, ensure_ascii=False)
    params = {
        "id": state["id"], "tenant": tenant_id, "user": user_id, "status": state["status"],
        "stage": state["active_stage_index"], "title": state["case_title"], "state": state_json,
        "created": state["created_at"], "updated": state["updated_at"],
    }
    with engine.begin() as conn:
        if is_new:
            if is_sqlite():
                sql = """INSERT INTO appeal_runs (id,tenant_id,user_id,status,active_stage_index,case_title,state_json,created_at,updated_at) VALUES (:id,:tenant,:user,:status,:stage,:title,:state,:created,:updated)"""
            else:
                sql = """INSERT INTO appeal_runs (id,tenant_id,user_id,status,active_stage_index,case_title,state_json,created_at,updated_at) VALUES (CAST(:id AS uuid),:tenant,:user,:status,:stage,:title,CAST(:state AS jsonb),:created,:updated)"""
        else:
            sql = """UPDATE appeal_runs SET status=:status, active_stage_index=:stage, state_json=:state, updated_at=:updated WHERE id=:id AND tenant_id=:tenant""" if is_sqlite() else """UPDATE appeal_runs SET status=:status, active_stage_index=:stage, state_json=CAST(:state AS jsonb), updated_at=:updated WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant"""
        conn.execute(text(sql), params)


def _load(run_id: str, tenant_id: str) -> dict[str, Any]:
    ensure_appeal_workflow_tables()
    sql = "SELECT state_json FROM appeal_runs WHERE id=:id AND tenant_id=:tenant" if is_sqlite() else "SELECT state_json FROM appeal_runs WHERE id=CAST(:id AS uuid) AND tenant_id=:tenant"
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"id": run_id, "tenant": tenant_id}).mappings().first()
    if not row:
        raise LookupError("未找到上诉模拟记录或无权访问。")
    value = row["state_json"]
    return json.loads(value) if isinstance(value, str) else dict(value)


def _checkpoint(state: dict[str, Any], tenant_id: str) -> None:
    stage_index = int(state["active_stage_index"])
    version = 1 + sum(1 for item in state.get("checkpoints", []) if item["stage_index"] == stage_index)
    checkpoint_id = str(uuid4())
    snapshot = json.dumps(state, ensure_ascii=False)
    params = {"id": checkpoint_id, "run": state["id"], "tenant": tenant_id, "stage": stage_index, "version": version, "state": snapshot, "created": _now()}
    with engine.begin() as conn:
        if is_sqlite():
            conn.execute(text("""INSERT INTO appeal_checkpoints (id,run_id,tenant_id,stage_index,version,state_json,created_at) VALUES (:id,:run,:tenant,:stage,:version,:state,:created)"""), params)
        else:
            conn.execute(text("""INSERT INTO appeal_checkpoints (id,run_id,tenant_id,stage_index,version,state_json,created_at) VALUES (CAST(:id AS uuid),CAST(:run AS uuid),:tenant,:stage,:version,CAST(:state AS jsonb),:created)"""), params)
    state.setdefault("checkpoints", []).append({"checkpoint_id": checkpoint_id, "stage_index": stage_index, "version": version})


def _assert_tool(role: str, tool: str) -> None:
    if tool not in ROLE_TOOL_ALLOWLIST.get(role, set()):
        raise PermissionError(f"{role} is not permitted to call {tool}")


def _trace(state: dict[str, Any], role: str, tool: str, started: float, *, status: str = "ok", notes: list[str] | None = None) -> None:
    _assert_tool(role, tool)
    state.setdefault("agent_trace", []).append({
        "role": role,
        "tool": tool,
        "status": status,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "notes": notes or [],
        "created_at": _now(),
    })


def _lookup_ids(state: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    return (
        {item["issue_id"] for item in state["issues"]},
        {item["record_id"] for item in state["record"]},
        {item["authority_id"] for item in state["authorities"]},
    )


def _latest_question_ids(state: dict[str, Any]) -> list[str]:
    for turn in reversed(state.get("turns") or []):
        if turn.get("speaker_role") == "judge_panel" and turn.get("question_ids"):
            return list(turn["question_ids"])
    return []


def _validate_turn(state: dict[str, Any], stage: str, turn: AppealAgentTurn) -> dict[str, Any]:
    expected_role = STAGE_DEFINITIONS[stage]["speaker"]
    if turn.speaker_role != expected_role:
        raise ValueError(f"角色越界：{stage} 只能由 {expected_role} 发言。")
    issue_ids, record_ids, authority_ids = _lookup_ids(state)
    invalid_issues = sorted(set(turn.issue_ids) - issue_ids)
    referenced_records = set(turn.record_ids)
    referenced_authorities = set(turn.authority_ids)
    for claim in turn.claims:
        referenced_records.update(claim.record_ids)
        referenced_authorities.update(claim.authority_ids)
    invalid_records = sorted(referenced_records - record_ids)
    invalid_authorities = sorted(referenced_authorities - authority_ids)
    known_questions = {question for item in state.get("turns") or [] for question in item.get("question_ids") or []}
    invalid_responses = sorted(set(turn.responds_to) - known_questions)
    if invalid_issues or invalid_records or invalid_authorities or invalid_responses:
        raise ValueError(
            "引用越界："
            + "; ".join(filter(None, [
                f"未知争点 {invalid_issues}" if invalid_issues else "",
                f"案卷外记录 {invalid_records}" if invalid_records else "",
                f"未知法律依据 {invalid_authorities}" if invalid_authorities else "",
                f"未知法官问题 {invalid_responses}" if invalid_responses else "",
            ]))
        )
    if turn.speaker_role == "judge_panel" and (turn.claims or turn.requested_order):
        raise ValueError("角色越界：合议庭只能提问，不能替任何一方提交主张或请求裁定。")
    if stage in ANSWER_STAGES and not turn.responds_to:
        raise ValueError("回答阶段必须明确对应已有的合议庭问题。")
    if stage == "appellant_reply":
        respondent_issues = {
            issue_id
            for item in state.get("turns") or []
            if item.get("speaker_role") == "respondent_counsel"
            for issue_id in item.get("issue_ids") or []
        }
        new_reply_issues = sorted(set(turn.issue_ids) - respondent_issues)
        if new_reply_issues:
            raise ValueError(f"答复越界：上诉方不能在答复阶段新增争点 {new_reply_issues}。")
    claim_checks = []
    for index, claim in enumerate(turn.claims, start=1):
        if claim.record_ids and claim.authority_ids:
            status = "record_and_authority"
        elif claim.record_ids:
            status = "record_only"
        elif claim.authority_ids:
            status = "authority_only"
        else:
            status = "unsupported"
        claim_checks.append({"claim_index": index, "status": status, "statement": claim.statement})
    return {
        "valid": True,
        "role_compliant": True,
        "invalid_reference_count": 0,
        "claim_checks": claim_checks,
        "record_traceable_claims": sum(item["status"] in {"record_and_authority", "record_only"} for item in claim_checks),
        "authority_traceable_claims": sum(item["status"] in {"record_and_authority", "authority_only"} for item in claim_checks),
    }


def _role_context(state: dict[str, Any], stage: str, role: str) -> dict[str, Any]:
    if role not in {"appellant_counsel", "respondent_counsel", "judge_panel"}:
        raise PermissionError(f"没有为 {role} 配置生成上下文。")
    return {
        "stage": stage,
        "stage_focus": STAGE_DEFINITIONS[stage]["focus"],
        "role": role,
        "case_title": state["case_title"],
        "issues": state["issues"],
        "record": state["record"],
        "authorities": state["authorities"],
        "requested_order": state["requested_order"],
        "latest_turns": state.get("turns", [])[-4:],
        "allowed_issue_ids": [item["issue_id"] for item in state["issues"]],
        "allowed_record_ids": [item["record_id"] for item in state["record"]],
        "allowed_authority_ids": [item["authority_id"] for item in state["authorities"]],
    }


def _fallback_turn(state: dict[str, Any], stage: str, role: str) -> AppealAgentTurn:
    issue_ids = [item["issue_id"] for item in state["issues"][:2]]
    if stage == "appellant_reply":
        respondent_issues = list(dict.fromkeys(
            issue_id
            for item in state.get("turns") or []
            if item.get("speaker_role") == "respondent_counsel"
            for issue_id in item.get("issue_ids") or []
        ))
        issue_ids = respondent_issues[:2]
    record_ids = [item["record_id"] for item in state["record"][:3]]
    authority_ids = [item["authority_id"] for item in state["authorities"][:2]]
    standards = sorted({item["expected_standard"] for item in state["issues"]})
    standard_text = "、".join("正确性标准" if item == "correctness" else "明显且具有决定性的错误标准" for item in standards)
    if role == "judge_panel":
        question_id = f"Q-{sum(len(item.get('question_ids') or []) for item in state.get('turns') or []) + 1:03d}"
        target = "上诉方" if stage == "judge_question_appellant" else "被上诉方"
        content = f"请{target}说明：你方主张适用何种审查标准，具体对应哪一项案卷记录，该错误为何会影响原审结果？"
        return AppealAgentTurn(speaker_role="judge_panel", content=content, issue_ids=issue_ids[:1], record_ids=[], authority_ids=[], question_ids=[question_id])
    if role == "appellant_counsel":
        if stage == "appellant_reply":
            content = "上诉方仅答复被上诉方关于审查标准和决定性影响的意见，并维持原请求裁定；本答复不增加新的上诉理由。"
        elif stage == "appellant_answer":
            content = f"上诉方直接回答合议庭问题：相关争点应按{standard_text}审查，所主张错误可由已标识案卷材料核对，并可能影响原决定的关键推理。"
        else:
            content = f"上诉方主张原审在已列争点上存在可审查错误，应适用{standard_text}；案卷材料和候选法律依据支持法院按请求裁定给予救济。"
    else:
        if stage == "respondent_answer":
            content = f"被上诉方直接回答合议庭问题：在{standard_text}下，上诉方尚未从案卷中指出足以改变结果的错误，原决定应受尊重。"
        else:
            content = f"被上诉方主张上诉方未能在{standard_text}下证明可干预错误；案卷整体支持原审推理，应驳回上诉或限制救济范围。"
    claims = [AppealClaim(statement=content, record_ids=record_ids, authority_ids=authority_ids)]
    return AppealAgentTurn(
        speaker_role=role,
        content=content,
        issue_ids=issue_ids,
        claims=claims,
        record_ids=record_ids,
        authority_ids=authority_ids,
        responds_to=_latest_question_ids(state) if stage in ANSWER_STAGES else [],
        requested_order=state["requested_order"] if role == "appellant_counsel" else "维持原决定或驳回上诉",
        stop_reason="turn_complete" if authority_ids else "insufficient_authority",
    )


def _generate_agent_turn(state: dict[str, Any], stage: str, role: str) -> AppealAgentTurn:
    context = _role_context(state, stage, role)
    if not is_llm_configured():
        return _fallback_turn(state, stage, role)
    role_rule = {
        "appellant_counsel": "你是上诉方代理人，只能依据给定案卷主张可审查错误，不得新增事实。答复阶段必须直接回应法官问题；reply 阶段不得提出新上诉理由。",
        "respondent_counsel": "你是被上诉方代理人，只能依据给定案卷回应上诉理由，不得新增事实或替上诉方完善主张。",
        "judge_panel": "你是安大略民事上诉合议庭，只提出中立问题，不替任何一方主张，不形成裁判结论。",
    }[role]
    instructions = (
        role_rule
        + " 所有 issue_id、record_id、authority_id 和 responds_to 必须来自输入允许列表。"
        + " 每个事实或法律主张分别写入 claims，并绑定实际使用的记录和法律依据。"
        + " 找不到依据时明确说明不足，不得编造。只返回符合 JSON Schema 的中文结果。"
    )
    try:
        raw = create_structured_response(
            "ontario_civil_appeal_turn_v1",
            AppealAgentTurn.model_json_schema(),
            instructions,
            json.dumps(context, ensure_ascii=False),
        )
        turn = AppealAgentTurn.model_validate(raw["data"])
        _validate_turn(state, stage, turn)
        return turn
    except (LLMServiceError, ValueError, KeyError, TypeError):
        return _fallback_turn(state, stage, role)


def _user_turn(state: dict[str, Any], stage: str, payload: AppealAdvanceInput, role: str) -> AppealAgentTurn:
    content = _clean(payload.content)
    if not content:
        raise ValueError("当前为练习方发言阶段，请提交陈述内容。")
    claim = AppealClaim(statement=content, record_ids=payload.record_ids, authority_ids=payload.authority_ids)
    return AppealAgentTurn(
        speaker_role=role,
        content=content,
        issue_ids=payload.issue_ids,
        claims=[claim],
        record_ids=payload.record_ids,
        authority_ids=payload.authority_ids,
        responds_to=_latest_question_ids(state) if stage in ANSWER_STAGES else [],
        requested_order=state["requested_order"] if role == "appellant_counsel" else "维持原决定或驳回上诉",
        stop_reason="turn_complete",
    )


def _term_overlap(content: str, values: list[str]) -> bool:
    lowered = content.lower()
    for value in values:
        terms = re.findall(r"[a-z][a-z0-9-]{3,}|[\u4e00-\u9fff]{2,6}", value.lower())
        if any(term in lowered for term in terms[:8]):
            return True
    return False


def _score_turn(state: dict[str, Any], stage: str, turn: AppealAgentTurn, verification: dict[str, Any]) -> dict[str, Any]:
    if turn.speaker_role not in COUNSEL_ROLES:
        return {"scored": False, "reason": "合议庭提问不计入当事方表现分。"}
    content = turn.content.lower()
    issue_lookup = {item["issue_id"]: item for item in state["issues"]}
    selected_issues = [issue_lookup[item] for item in turn.issue_ids if item in issue_lookup]
    issue_framing = 15 if selected_issues and _term_overlap(content, [item["description"] for item in selected_issues]) else 8 if selected_issues else 2
    expected = {item["expected_standard"] for item in selected_issues} or {item["expected_standard"] for item in state["issues"]}
    mentions_correctness = any(term in content for term in ("correctness", "正确性", "正确标准"))
    mentions_deference = any(term in content for term in ("palpable", "overriding", "明显且具有决定性", "明显和决定性", "尊重原审"))
    standard_matches = ("correctness" not in expected or mentions_correctness) and ("palpable_and_overriding_error" not in expected or mentions_deference)
    standard_score = 20 if standard_matches else 8 if mentions_correctness or mentions_deference else 0
    claim_total = max(len(turn.claims), 1)
    record_score = round(20 * verification["record_traceable_claims"] / claim_total)
    authority_score = round(20 * verification["authority_traceable_claims"] / claim_total)
    if stage in ANSWER_STAGES:
        responsiveness = 15 if turn.responds_to else 3
    elif stage == "appellant_reply":
        responsiveness = 13 if any(item["speaker_role"] == "respondent_counsel" for item in state.get("turns") or []) else 4
    else:
        responsiveness = 10
    requested_values = [state["requested_order"]] if turn.speaker_role == "appellant_counsel" else ["维持", "驳回", "dismiss", "affirm"]
    remedy_scope = 10 if _term_overlap(content, requested_values) or _term_overlap(turn.requested_order, requested_values) else 4
    unsupported = sum(item["status"] == "unsupported" for item in verification["claim_checks"])
    penalty = min(20, unsupported * 10)
    total = max(0, issue_framing + standard_score + record_score + authority_score + responsiveness + remedy_scope - penalty)
    return {
        "scored": True,
        "role": turn.speaker_role,
        "stage": stage,
        "dimensions": {
            "issue_framing": issue_framing,
            "standard_of_review": standard_score,
            "record_support": record_score,
            "authority_application": authority_score,
            "bench_responsiveness": responsiveness,
            "remedy_and_reply_scope": remedy_scope,
        },
        "penalty": penalty,
        "total": total,
        "maximum": 100,
        "notes": ["存在未绑定案卷或法律依据的主张。"] if unsupported else [],
    }


def _practice_role_name(state: dict[str, Any]) -> str:
    return {"appellant": "appellant_counsel", "respondent": "respondent_counsel"}.get(state["practice_role"], "")


def _requires_user_input(state: dict[str, Any], stage: str) -> bool:
    return STAGE_DEFINITIONS[stage]["speaker"] == _practice_role_name(state)


def _stage_nodes(state: dict[str, Any]) -> list[dict[str, Any]]:
    active = int(state["active_stage_index"])
    nodes = []
    for index, stage in enumerate(APPEAL_STAGES):
        status = "completed" if index < active else "active" if index == active and state["status"] == "active" else "pending"
        if state["status"] == "completed" and index == len(APPEAL_STAGES) - 1:
            status = "completed"
        nodes.append({"stage": stage, "label": STAGE_DEFINITIONS[stage]["label"], "index": index, "status": status})
    return nodes


def _build_report(state: dict[str, Any]) -> dict[str, Any]:
    scores = [item for item in state.get("scores") or [] if item.get("scored")]
    role_scores: dict[str, list[dict[str, Any]]] = {"appellant_counsel": [], "respondent_counsel": []}
    for score in scores:
        role_scores.setdefault(score["role"], []).append(score)
    performance = {}
    for role, values in role_scores.items():
        performance[role] = round(sum(item["total"] for item in values) / len(values), 1) if values else 0.0
    claim_checks = [check for turn in state.get("turns") or [] for check in (turn.get("verification") or {}).get("claim_checks", [])]
    claim_total = len(claim_checks)
    record_traceability = round(sum(item["status"] in {"record_and_authority", "record_only"} for item in claim_checks) / claim_total, 3) if claim_total else 0.0
    authority_traceability = round(sum(item["status"] in {"record_and_authority", "authority_only"} for item in claim_checks) / claim_total, 3) if claim_total else 0.0
    issue_ids = {issue for turn in state.get("turns") or [] for issue in turn.get("issue_ids") or [] if turn.get("speaker_role") in COUNSEL_ROLES}
    issue_coverage = round(len(issue_ids) / max(len(state["issues"]), 1), 3)
    actions = []
    if record_traceability < 1:
        actions.append("为每个事实主张绑定具体案卷编号，并说明该记录如何支持所述事实。")
    if authority_traceability < 1:
        actions.append("为每个法律主张补充可核验判例或法规，并解释规则如何适用于本案。")
    if issue_coverage < 1:
        actions.append("逐项处理尚未覆盖的上诉问题，避免只围绕最有利争点陈述。")
    for role, values in role_scores.items():
        if values and sum(item["dimensions"]["standard_of_review"] for item in values) / len(values) < 14:
            label = "上诉方" if role == "appellant_counsel" else "被上诉方"
            actions.append(f"{label}需要先区分法律问题、事实问题和混合问题，再说明相应审查标准。")
    if not state["authorities"]:
        actions.append("当前没有检索到候选法律依据，报告只能评价陈述结构，不能评价法律支持程度。")
    return {
        "status": "training_feedback_only",
        "performance_scores": performance,
        "record_traceability": record_traceability,
        "authority_traceability": authority_traceability,
        "issue_coverage": issue_coverage,
        "role_compliance": 1.0,
        "invalid_reference_count": 0,
        "improvement_actions": list(dict.fromkeys(actions)) or ["当前结构完整，下一步应由安大略执业律师核验引用内容和适用时点。"],
        "unresolved_issues": [item for item in state["issues"] if item["issue_id"] not in issue_ids],
        "probability": None,
        "display_probability": False,
        "disclaimer": DISCLAIMER,
    }


def _view(state: dict[str, Any]) -> dict[str, Any]:
    index = min(int(state["active_stage_index"]), len(APPEAL_STAGES) - 1)
    stage = APPEAL_STAGES[index]
    return {
        **state,
        "active_stage": stage,
        "active_stage_label": STAGE_DEFINITIONS[stage]["label"],
        "active_stage_focus": STAGE_DEFINITIONS[stage]["focus"],
        "expected_speaker": STAGE_DEFINITIONS[stage]["speaker"],
        "requires_user_input": state["status"] == "active" and _requires_user_input(state, stage),
        "stage_nodes": _stage_nodes(state),
        "disclaimer": DISCLAIMER,
    }


def create_appeal_run(payload: AppealRunCreate, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    started = time.monotonic()
    query = " ".join([payload.case_summary, payload.grounds_of_appeal, payload.lower_court_decision])
    authorities = _authority_candidates(query)
    now = _now()
    state = {
        "id": str(uuid4()),
        "workflow_version": WORKFLOW_VERSION,
        "jurisdiction": "Ontario",
        "appeal_type": "civil",
        "status": "active",
        "active_stage_index": 0,
        "case_title": _clean(payload.case_title),
        "case_summary": _clean(payload.case_summary),
        "lower_court_decision": _clean(payload.lower_court_decision),
        "grounds_of_appeal": _clean(payload.grounds_of_appeal),
        "requested_order": _clean(payload.requested_order),
        "practice_role": payload.practice_role,
        "issues": _build_issues(payload.grounds_of_appeal),
        "record": _build_record(payload),
        "authorities": authorities,
        "turns": [],
        "scores": [],
        "messages": [{
            "message_id": str(uuid4()), "stage": "record_review", "speaker_role": "court_clerk",
            "content": "上诉模拟已建立。书记员将先核对案卷范围、上诉问题、请求裁定和候选法律依据。",
            "kind": "system", "created_at": now,
        }],
        "agent_trace": [],
        "checkpoints": [],
        "final_report": None,
        "created_at": now,
        "updated_at": now,
    }
    _trace(state, "court_clerk", "workflow_state", started, notes=[f"issues={len(state['issues'])}", f"record={len(state['record'])}", f"authorities={len(authorities)}"])
    _persist(state, user_id=user_id, tenant_id=tenant_id, is_new=True)
    return _view(state)


def get_appeal_run(run_id: str, *, tenant_id: str) -> dict[str, Any]:
    return _view(_load(run_id, tenant_id))


def advance_appeal_run(run_id: str, payload: AppealAdvanceInput, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    state = _load(run_id, tenant_id)
    if state["status"] != "active":
        raise ValueError("该上诉模拟已经完成，不能继续推进。")
    stage_index = int(state["active_stage_index"])
    stage = APPEAL_STAGES[stage_index]
    definition = STAGE_DEFINITIONS[stage]
    _checkpoint(state, tenant_id)

    if stage == "record_review":
        started = time.monotonic()
        warnings = []
        if not state["authorities"]:
            warnings.append("未检索到候选法律依据，后续法律依据评分将为零。")
        if not any(item["record_type"] == "record_material" for item in state["record"]):
            warnings.append("未提供额外案卷材料，当前仅使用案情摘要、原审决定、上诉理由和请求裁定。")
        standards = sorted({item["expected_standard"] for item in state["issues"]})
        content = f"案卷审查完成：识别 {len(state['issues'])} 项上诉问题、{len(state['record'])} 项案卷记录和 {len(state['authorities'])} 项候选法律依据。预期审查标准：{', '.join(standards)}。"
        state["messages"].append({"message_id": str(uuid4()), "stage": stage, "speaker_role": "court_clerk", "content": content, "kind": "review", "warnings": warnings, "created_at": _now()})
        _trace(state, "court_clerk", "record_read", started, notes=warnings)
        if state["authorities"]:
            _trace(state, "court_clerk", "authority_read", started, notes=[f"authorities={len(state['authorities'])}"])
    elif stage == "panel_report":
        started = time.monotonic()
        _assert_tool("evaluation_service", "build_report")
        state["final_report"] = _build_report(state)
        state["messages"].append({"message_id": str(uuid4()), "stage": stage, "speaker_role": "evaluation_service", "content": "训练报告已经生成。报告只评价陈述质量和材料覆盖，不预测案件结果。", "kind": "report", "created_at": _now()})
        _trace(state, "evaluation_service", "build_report", started)
        state["status"] = "completed"
    else:
        role = definition["speaker"]
        started = time.monotonic()
        if _requires_user_input(state, stage):
            turn = _user_turn(state, stage, payload, role)
            source = "user"
        else:
            turn = _generate_agent_turn(state, stage, role)
            source = "agent"
        if turn.record_ids or role == "judge_panel":
            _trace(state, role, "record_read", started, notes=[f"records={len(turn.record_ids)}", f"stage={stage}"])
        if turn.authority_ids or (role == "judge_panel" and state["authorities"]):
            _trace(state, role, "authority_read", started, notes=[f"authorities={len(turn.authority_ids)}", f"stage={stage}"])
        verification_started = time.monotonic()
        verification = _validate_turn(state, stage, turn)
        _trace(state, "verification_service", "reference_validation", verification_started, notes=[f"claims={len(turn.claims)}", "invalid_references=0"])
        score_started = time.monotonic()
        score = _score_turn(state, stage, turn, verification)
        _trace(state, "evaluation_service", "score_turn", score_started, notes=[f"stage={stage}", f"scored={score.get('scored')}"])
        turn_payload = turn.model_dump()
        turn_payload.update({"turn_id": str(uuid4()), "stage": stage, "source": source, "verification": verification, "score": score, "created_at": _now()})
        state["turns"].append(turn_payload)
        if score.get("scored"):
            state["scores"].append(score)
        state["messages"].append({
            "message_id": str(uuid4()), "stage": stage, "speaker_role": role,
            "content": turn.content, "kind": "submission" if role in COUNSEL_ROLES else "question",
            "record_ids": turn.record_ids, "authority_ids": turn.authority_ids,
            "score": score, "source": source, "created_at": _now(),
        })
        _trace(state, role, "question_issue" if role == "judge_panel" else "transcript_read", started, notes=[f"stage={stage}", f"source={source}"])

    if state["status"] == "active":
        state["active_stage_index"] += 1
    state["updated_at"] = _now()
    _persist(state, user_id=user_id, tenant_id=tenant_id)
    return _view(state)


def rollback_appeal_run(run_id: str, *, stage_index: int, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    if stage_index < 0 or stage_index >= len(APPEAL_STAGES):
        raise ValueError("无效的回滚阶段。")
    ensure_appeal_workflow_tables()
    sql = """SELECT state_json FROM appeal_checkpoints WHERE run_id=:run AND tenant_id=:tenant AND stage_index=:stage ORDER BY version DESC LIMIT 1""" if is_sqlite() else """SELECT state_json FROM appeal_checkpoints WHERE run_id=CAST(:run AS uuid) AND tenant_id=:tenant AND stage_index=:stage ORDER BY version DESC LIMIT 1"""
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"run": run_id, "tenant": tenant_id, "stage": stage_index}).mappings().first()
    if not row:
        raise ValueError("该阶段没有可用检查点。")
    value = row["state_json"]
    state = json.loads(value) if isinstance(value, str) else dict(value)
    state["status"] = "active"
    state["active_stage_index"] = stage_index
    state["final_report"] = None
    state["updated_at"] = _now()
    state.setdefault("messages", []).append({
        "message_id": str(uuid4()), "stage": APPEAL_STAGES[stage_index], "speaker_role": "court_clerk",
        "content": f"已回滚到{STAGE_DEFINITIONS[APPEAL_STAGES[stage_index]]['label']}，该阶段及后续内容需要重新完成。",
        "kind": "rollback", "created_at": _now(),
    })
    _persist(state, user_id=user_id, tenant_id=tenant_id)
    return _view(state)
