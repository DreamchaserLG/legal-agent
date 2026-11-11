from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.engine import Connection

from app.core.config import settings
from app.core.database import engine, is_sqlite
from app.service.case_packet_service import (
    bind_external_research,
    build_case_packet,
    merge_human_submission,
    packet_reference_ids,
)
from app.service.common_service import json_text, repair_text
from app.service.legal_research_tool_service import (
    execute_research_tool,
    merge_evidence,
    verify_evidence_pool,
)
from app.service.llm_service import LLMServiceError, create_structured_response, is_llm_configured
from app.service.legal_safety_service import build_role_instruction, enforce_grounded_legal_text
from app.service.module_service import normalize_module
from app.service.multi_agent_service import ensure_multi_agent_tables
from app.service.user_preference_service import (
    build_review_observation,
    ensure_user_preference_tables,
    record_preference_observation,
    set_preference_tracking,
)


INTERACTIVE_GRAPH_VERSION = "human-agent-legal-graph-2.0"
ACTIVE_RUN_STATUSES = {"queued", "running", "waiting_for_human"}
TERMINAL_RUN_STATUSES = {"completed", "failed", "cancelled"}
_WORKER_THREAD: threading.Thread | None = None
_WORKER_STOP = threading.Event()


class InteractiveAgentRunCreate(BaseModel):
    query: str = Field(min_length=1, max_length=20000)
    initial_statement: str = Field(default="", max_length=12000)
    human_side: Literal["claimant", "respondent"] = "claimant"
    intent: Literal[
        "auto", "statute_lookup", "case_lookup", "single_question", "risk_analysis",
        "hearing_simulation", "cross_jurisdiction", "complex_research",
    ] = "complex_research"
    module: str = "canada"
    filters: dict[str, str] = Field(default_factory=dict)
    case_acl: list[str] = Field(default_factory=list, max_length=500)
    max_rounds: int = Field(default=2, ge=1, le=3)
    max_steps: int = Field(default=12, ge=4, le=24)
    timeout_seconds: int = Field(default=120, ge=15, le=300)
    token_budget: int = Field(default=10000, ge=1000, le=30000)
    preference_history_enabled: bool = False
    # Backward-compatible input for clients released before the user-memory
    # feature was separated from the Agent layer.
    review_memory_enabled: bool = False


class HumanSubmissionCreate(BaseModel):
    submission_id: str = Field(default="", max_length=80)
    statement: str = Field(min_length=1, max_length=12000)
    fact_refs: list[str] = Field(default_factory=list, max_length=100)
    evidence_refs: list[str] = Field(default_factory=list, max_length=100)
    round_no: int | None = Field(default=None, ge=1, le=3)


class ResearchAction(BaseModel):
    tool: Literal[
        "search_case_law", "search_statute", "search_legal_graph",
        "search_approved_web", "fetch_source_snapshot",
    ]
    query: str = Field(min_length=1, max_length=800)
    reason: str = Field(default="", max_length=400)


class ResearchPlan(BaseModel):
    focus: str = Field(default="", max_length=600)
    actions: list[ResearchAction] = Field(default_factory=list, max_length=6)
    known_gaps: list[str] = Field(default_factory=list, max_length=8)
    stop_research: bool = False
    stop_reason: str = Field(default="", max_length=400)


class AgentArgument(BaseModel):
    argument_id: str = Field(default="", max_length=80)
    claim: str = Field(min_length=1, max_length=1600)
    fact_refs: list[str] = Field(default_factory=list, max_length=40)
    evidence_refs: list[str] = Field(default_factory=list, max_length=40)
    uncertainty: str = Field(default="", max_length=600)


class AdvocateOpinion(BaseModel):
    position_summary: str = Field(default="", max_length=2000)
    arguments: list[AgentArgument] = Field(default_factory=list, max_length=8)
    challenges_to_human: list[str] = Field(default_factory=list, max_length=8)
    missing_facts: list[str] = Field(default_factory=list, max_length=8)
    stop_reason: str = Field(default="", max_length=600)


class JudgeFinding(BaseModel):
    issue: str = Field(min_length=1, max_length=1000)
    human_position: str = Field(default="", max_length=1400)
    agent_position: str = Field(default="", max_length=1400)
    analysis: str = Field(default="", max_length=1800)
    fact_refs: list[str] = Field(default_factory=list, max_length=40)
    evidence_refs: list[str] = Field(default_factory=list, max_length=40)
    status: Literal["supported", "contested", "insufficient"] = "insufficient"


class JudgeDecision(BaseModel):
    conclusion: str = Field(default="", max_length=2400)
    findings: list[JudgeFinding] = Field(default_factory=list, max_length=8)
    uncertainties: list[str] = Field(default_factory=list, max_length=10)
    further_review: list[str] = Field(default_factory=list, max_length=10)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _safe_filters(filters: dict[str, str]) -> dict[str, str]:
    allowed = {"jurisdiction", "document_type", "court_level", "language", "date_from", "date_to"}
    return {key: repair_text(value) for key, value in filters.items() if key in allowed and repair_text(value)}


def _add_missing_sqlite_columns(conn) -> None:
    existing = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(multi_agent_runs)").fetchall()}
    definitions = {
        "human_side": "TEXT NOT NULL DEFAULT 'claimant'",
        "agent_side": "TEXT NOT NULL DEFAULT 'respondent'",
        "module": "TEXT NOT NULL DEFAULT 'canada'",
        "current_stage": "TEXT NOT NULL DEFAULT 'case_intake'",
        "current_round": "INTEGER NOT NULL DEFAULT 1",
        "max_rounds": "INTEGER NOT NULL DEFAULT 2",
        "request_json": "TEXT NOT NULL DEFAULT '{}'",
        "result_json": "TEXT NOT NULL DEFAULT '{}'",
        "cancel_requested": "INTEGER NOT NULL DEFAULT 0",
        "claimed_by": "TEXT",
        "last_error": "TEXT",
        "updated_at": "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "started_at": "TIMESTAMP NULL",
        "finished_at": "TIMESTAMP NULL",
        "last_heartbeat": "TIMESTAMP NULL",
    }
    for column, definition in definitions.items():
        if column not in existing:
            conn.exec_driver_sql(f"ALTER TABLE multi_agent_runs ADD COLUMN {column} {definition}")


def ensure_legal_agent_runtime_tables() -> None:
    ensure_multi_agent_tables()
    if is_sqlite():
        with engine.begin() as conn:
            _add_missing_sqlite_columns(conn)
            statements = [
                """
                CREATE TABLE IF NOT EXISTS multi_agent_run_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    event_type TEXT NOT NULL,
                    actor_type TEXT NOT NULL DEFAULT 'system',
                    actor_name TEXT NOT NULL DEFAULT 'supervisor',
                    stage TEXT NOT NULL DEFAULT '',
                    round_no INTEGER NOT NULL DEFAULT 0,
                    summary TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """,
                "CREATE INDEX IF NOT EXISTS idx_multi_agent_events_run_id ON multi_agent_run_events (run_id, id)",
                """
                CREATE TABLE IF NOT EXISTS multi_agent_human_submissions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER NOT NULL,
                    submission_id TEXT NOT NULL,
                    round_no INTEGER NOT NULL,
                    side TEXT NOT NULL,
                    statement TEXT NOT NULL,
                    fact_refs_json TEXT NOT NULL DEFAULT '[]',
                    evidence_refs_json TEXT NOT NULL DEFAULT '[]',
                    created_by INTEGER,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(run_id, submission_id),
                    UNIQUE(run_id, round_no)
                )
                """,
            ]
            for statement in statements:
                conn.execute(text(statement))
        ensure_user_preference_tables()
        return

    statements = [
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS human_side VARCHAR(20) NOT NULL DEFAULT 'claimant'",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS agent_side VARCHAR(20) NOT NULL DEFAULT 'respondent'",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS module VARCHAR(40) NOT NULL DEFAULT 'canada'",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS current_stage VARCHAR(60) NOT NULL DEFAULT 'case_intake'",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS current_round INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS max_rounds INTEGER NOT NULL DEFAULT 2",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS request_json JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS result_json JSONB NOT NULL DEFAULT '{}'::jsonb",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS cancel_requested BOOLEAN NOT NULL DEFAULT FALSE",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS claimed_by VARCHAR(120)",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS last_error TEXT",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS started_at TIMESTAMP NULL",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS finished_at TIMESTAMP NULL",
        "ALTER TABLE multi_agent_runs ADD COLUMN IF NOT EXISTS last_heartbeat TIMESTAMP NULL",
        """
        CREATE TABLE IF NOT EXISTS multi_agent_run_events (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT NOT NULL REFERENCES multi_agent_runs(id) ON DELETE CASCADE,
            event_type VARCHAR(60) NOT NULL,
            actor_type VARCHAR(30) NOT NULL DEFAULT 'system',
            actor_name VARCHAR(80) NOT NULL DEFAULT 'supervisor',
            stage VARCHAR(60) NOT NULL DEFAULT '',
            round_no INTEGER NOT NULL DEFAULT 0,
            summary TEXT NOT NULL DEFAULT '',
            payload_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_multi_agent_events_run_id ON multi_agent_run_events (run_id, id)",
        """
        CREATE TABLE IF NOT EXISTS multi_agent_human_submissions (
            id BIGSERIAL PRIMARY KEY,
            run_id BIGINT NOT NULL REFERENCES multi_agent_runs(id) ON DELETE CASCADE,
            submission_id VARCHAR(80) NOT NULL,
            round_no INTEGER NOT NULL,
            side VARCHAR(20) NOT NULL,
            statement TEXT NOT NULL,
            fact_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            evidence_refs_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            created_by BIGINT,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(run_id, submission_id),
            UNIQUE(run_id, round_no)
        )
        """,
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    ensure_user_preference_tables()


def _append_event(
    run_id: int,
    event_type: str,
    summary: str,
    *,
    actor_type: str = "system",
    actor_name: str = "supervisor",
    stage: str = "",
    round_no: int = 0,
    payload: dict[str, Any] | None = None,
    connection: Connection | None = None,
) -> int:
    with (nullcontext(connection) if connection is not None else engine.begin()) as conn:
        if is_sqlite():
            row = conn.execute(
                text(
                    """
                    INSERT INTO multi_agent_run_events
                        (run_id,event_type,actor_type,actor_name,stage,round_no,summary,payload_json)
                    VALUES (:run_id,:event_type,:actor_type,:actor_name,:stage,:round_no,:summary,:payload)
                    RETURNING id
                    """
                ),
                {
                    "run_id": run_id, "event_type": event_type, "actor_type": actor_type,
                    "actor_name": actor_name, "stage": stage, "round_no": round_no,
                    "summary": repair_text(summary), "payload": json_text(payload or {}),
                },
            ).mappings().first()
        else:
            row = conn.execute(
                text(
                    """
                    INSERT INTO multi_agent_run_events
                        (run_id,event_type,actor_type,actor_name,stage,round_no,summary,payload_json)
                    VALUES (:run_id,:event_type,:actor_type,:actor_name,:stage,:round_no,:summary,CAST(:payload AS jsonb))
                    RETURNING id
                    """
                ),
                {
                    "run_id": run_id, "event_type": event_type, "actor_type": actor_type,
                    "actor_name": actor_name, "stage": stage, "round_no": round_no,
                    "summary": repair_text(summary), "payload": json_text(payload or {}),
                },
            ).mappings().first()
    return int(row["id"]) if row else 0


def _load_run_row(run_id: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM multi_agent_runs WHERE id = :run_id"), {"run_id": run_id}).mappings().first()
    return dict(row) if row else None


def _serialize_submission(row: dict[str, Any]) -> dict[str, Any]:
    payload = dict(row)
    payload["fact_refs"] = _decode_json(payload.pop("fact_refs_json", []), [])
    payload["evidence_refs"] = _decode_json(payload.pop("evidence_refs_json", []), [])
    return payload


def _list_submissions(run_id: int) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT * FROM multi_agent_human_submissions WHERE run_id=:run_id ORDER BY round_no,id"),
            {"run_id": run_id},
        ).mappings().all()
    return [_serialize_submission(dict(row)) for row in rows]


def _submission_for_round(run_id: int, round_no: int) -> dict[str, Any] | None:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT * FROM multi_agent_human_submissions "
                "WHERE run_id=:run_id AND round_no=:round_no ORDER BY id DESC LIMIT 1"
            ),
            {"run_id": run_id, "round_no": round_no},
        ).mappings().first()
    return _serialize_submission(dict(row)) if row else None


def _serialize_run(row: dict[str, Any], *, include_submissions: bool = True) -> dict[str, Any]:
    request_payload = _decode_json(row.get("request_json"), {})
    state = _decode_json(row.get("state_json"), {})
    result = _decode_json(row.get("result_json"), {})
    payload = {
        "id": int(row["id"]),
        "graph_version": repair_text(row.get("graph_version")),
        "status": repair_text(row.get("status")),
        "intent": repair_text(row.get("intent")),
        "query": repair_text(row.get("query_text")),
        "human_side": repair_text(row.get("human_side")) or "claimant",
        "agent_side": repair_text(row.get("agent_side")) or "respondent",
        "module": repair_text(row.get("module")) or "canada",
        "current_stage": repair_text(row.get("current_stage")),
        "current_round": int(row.get("current_round") or 1),
        "max_rounds": int(row.get("max_rounds") or 1),
        "cancel_requested": bool(row.get("cancel_requested")),
        "request": request_payload,
        "case_packet": state.get("case_packet") or {},
        "evidence_pool": state.get("evidence_pool") or [],
        "agent_rounds": state.get("agent_rounds") or [],
        "verification": state.get("verification") or [],
        "result": result,
        "last_error": repair_text(row.get("last_error")),
        "duration_ms": int(row.get("duration_ms") or 0),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "started_at": row.get("started_at"),
        "finished_at": row.get("finished_at"),
        "review_required": True,
        "disclaimer": "系统输出仅用于法律研究与模拟审理，必须由具备相应法域资格的律师复核，不构成法律意见。",
    }
    if include_submissions:
        payload["human_submissions"] = _list_submissions(int(row["id"]))
    return payload


def _write_run(
    run_id: int,
    *,
    status: str,
    current_stage: str,
    current_round: int,
    state: dict[str, Any],
    result: dict[str, Any] | None = None,
    last_error: str = "",
    finished: bool = False,
) -> None:
    duration_ms = int(state.get("duration_ms") or 0)
    params = {
        "run_id": run_id,
        "status": status,
        "current_stage": current_stage,
        "current_round": current_round,
        "state_json": json_text(state),
        "result_json": json_text(result or {}),
        "last_error": repair_text(last_error),
        "duration_ms": duration_ms,
    }
    state_assignment = "state_json=:state_json, result_json=:result_json" if is_sqlite() else "state_json=CAST(:state_json AS jsonb), result_json=CAST(:result_json AS jsonb)"
    finished_assignment = ", finished_at=CURRENT_TIMESTAMP" if finished else ""
    with engine.begin() as conn:
        conn.execute(
            text(
                f"""
                UPDATE multi_agent_runs
                SET status=:status, current_stage=:current_stage, current_round=:current_round,
                    {state_assignment}, last_error=:last_error, duration_ms=:duration_ms,
                    updated_at=CURRENT_TIMESTAMP, last_heartbeat=CURRENT_TIMESTAMP
                    {finished_assignment}
                WHERE id=:run_id
                """
            ),
            params,
        )


def enqueue_legal_agent_run(
    payload: InteractiveAgentRunCreate,
    *,
    user_id: int,
    tenant_id: str,
) -> dict[str, Any]:
    ensure_legal_agent_runtime_tables()
    query = repair_text(payload.query)
    if not query:
        raise ValueError("query is required")
    tenant = repair_text(tenant_id) or f"user:{user_id}"
    agent_side = "respondent" if payload.human_side == "claimant" else "claimant"
    request_payload = payload.model_dump(mode="json")
    request_payload["query"] = query
    request_payload["filters"] = _safe_filters(payload.filters)
    request_payload["case_acl"] = [repair_text(value) for value in payload.case_acl if repair_text(value)]
    state = {
        "case_packet": {},
        "evidence_pool": [],
        "agent_rounds": [],
        "verification": [],
        "processed_submission_ids": [],
        "duration_ms": 0,
    }
    with engine.begin() as conn:
        if is_sqlite():
            row = conn.execute(
                text(
                    """
                    INSERT INTO multi_agent_runs
                        (graph_version,tenant_id,user_id,intent,query_text,status,state_json,duration_ms,
                         human_side,agent_side,module,current_stage,current_round,max_rounds,request_json,result_json)
                    VALUES (:version,:tenant,:user_id,:intent,:query,'queued',:state,0,
                            :human_side,:agent_side,:module,'case_intake',1,:max_rounds,:request,'{}')
                    RETURNING id
                    """
                ),
                {
                    "version": INTERACTIVE_GRAPH_VERSION, "tenant": tenant, "user_id": user_id,
                    "intent": payload.intent, "query": query, "state": json_text(state),
                    "human_side": payload.human_side, "agent_side": agent_side,
                    "module": normalize_module(payload.module), "max_rounds": payload.max_rounds,
                    "request": json_text(request_payload),
                },
            ).mappings().first()
        else:
            row = conn.execute(
                text(
                    """
                    INSERT INTO multi_agent_runs
                        (graph_version,tenant_id,user_id,intent,query_text,status,state_json,duration_ms,
                         human_side,agent_side,module,current_stage,current_round,max_rounds,request_json,result_json)
                    VALUES (:version,:tenant,:user_id,:intent,:query,'queued',CAST(:state AS jsonb),0,
                            :human_side,:agent_side,:module,'case_intake',1,:max_rounds,
                            CAST(:request AS jsonb),'{}'::jsonb)
                    RETURNING id
                    """
                ),
                {
                    "version": INTERACTIVE_GRAPH_VERSION, "tenant": tenant, "user_id": user_id,
                    "intent": payload.intent, "query": query, "state": json_text(state),
                    "human_side": payload.human_side, "agent_side": agent_side,
                    "module": normalize_module(payload.module), "max_rounds": payload.max_rounds,
                    "request": json_text(request_payload),
                },
            ).mappings().first()
        run_id = int(row["id"])
        _append_event(
            run_id, "run_created", "人机审理任务已创建。", stage="case_intake",
            payload={"human_side": payload.human_side, "agent_side": agent_side, "max_rounds": payload.max_rounds},
            connection=conn,
        )
        initial_statement = repair_text(payload.initial_statement)
        if initial_statement:
            submission_id = f"initial-{uuid4().hex[:12]}"
            conn.execute(
                text(
                    """
                    INSERT INTO multi_agent_human_submissions
                        (run_id,submission_id,round_no,side,statement,created_by)
                    VALUES (:run_id,:submission_id,1,:side,:statement,:created_by)
                    """
                ),
                {
                    "run_id": run_id, "submission_id": submission_id,
                    "side": payload.human_side, "statement": initial_statement, "created_by": user_id,
                },
            )
            conn.execute(
                text("UPDATE multi_agent_runs SET current_stage='human_submission_received' WHERE id=:run_id"),
                {"run_id": run_id},
            )
            _append_event(
                run_id, "human_submission_received", "已收到真人一方第 1 轮提交。",
                actor_type="human", actor_name=payload.human_side, stage="human_turn", round_no=1,
                payload={"submission_id": submission_id, "fact_ref_count": 0, "evidence_ref_count": 0},
                connection=conn,
            )
    if payload.preference_history_enabled or payload.review_memory_enabled:
        set_preference_tracking(tenant_id=tenant, user_id=user_id, enabled=True)
    return get_legal_agent_run(run_id, user_id=user_id, tenant_id=tenant)


def _owned_run(run_id: int, *, user_id: int, tenant_id: str) -> dict[str, Any]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM multi_agent_runs WHERE id=:run_id AND tenant_id=:tenant AND user_id=:user_id"),
            {"run_id": run_id, "tenant": repair_text(tenant_id), "user_id": user_id},
        ).mappings().first()
    if not row:
        raise ValueError("Agent run not found or access denied.")
    return dict(row)


def get_legal_agent_run(run_id: int, *, user_id: int, tenant_id: str) -> dict[str, Any]:
    ensure_legal_agent_runtime_tables()
    return _serialize_run(_owned_run(run_id, user_id=user_id, tenant_id=tenant_id))


def list_legal_agent_events(
    run_id: int,
    *,
    user_id: int,
    tenant_id: str,
    after_seq: int = 0,
) -> dict[str, Any]:
    _owned_run(run_id, user_id=user_id, tenant_id=tenant_id)
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT * FROM multi_agent_run_events
                WHERE run_id=:run_id AND id>:after_seq
                ORDER BY id ASC LIMIT 250
                """
            ),
            {"run_id": run_id, "after_seq": max(0, int(after_seq))},
        ).mappings().all()
    events = []
    for row in rows:
        item = dict(row)
        item["seq"] = int(item["id"])
        item["payload"] = _decode_json(item.pop("payload_json", {}), {})
        events.append(item)
    return {"run_id": run_id, "events": events, "last_seq": events[-1]["seq"] if events else int(after_seq)}


def submit_human_turn(
    run_id: int,
    payload: HumanSubmissionCreate,
    *,
    user_id: int,
    tenant_id: str,
    allow_queued: bool = False,
) -> dict[str, Any]:
    ensure_legal_agent_runtime_tables()
    row = _owned_run(run_id, user_id=user_id, tenant_id=tenant_id)
    submission_id = repair_text(payload.submission_id) or f"submission-{uuid4().hex[:16]}"
    with engine.connect() as conn:
        existing = conn.execute(
            text(
                "SELECT id FROM multi_agent_human_submissions "
                "WHERE run_id=:run_id AND submission_id=:submission_id"
            ),
            {"run_id": run_id, "submission_id": submission_id},
        ).mappings().first()
    if existing:
        return get_legal_agent_run(run_id, user_id=user_id, tenant_id=tenant_id)
    status = repair_text(row.get("status"))
    allowed_statuses = {"waiting_for_human"}
    if allow_queued:
        allowed_statuses.add("queued")
    if status not in allowed_statuses:
        raise ValueError(f"Run is not accepting a human submission while status={status}.")
    expected_round = int(row.get("current_round") or 1)
    round_no = int(payload.round_no or expected_round)
    if round_no != expected_round:
        raise ValueError(f"Submission round must be {expected_round}.")
    with engine.connect() as conn:
        occupied_round = conn.execute(
            text(
                "SELECT submission_id FROM multi_agent_human_submissions "
                "WHERE run_id=:run_id AND round_no=:round_no"
            ),
            {"run_id": run_id, "round_no": round_no},
        ).mappings().first()
    if occupied_round:
        raise ValueError(f"Round {round_no} already has a human submission.")
    params = {
        "run_id": run_id,
        "submission_id": submission_id,
        "round_no": round_no,
        "side": repair_text(row.get("human_side")) or "claimant",
        "statement": repair_text(payload.statement),
        "fact_refs": json_text([repair_text(value) for value in payload.fact_refs if repair_text(value)]),
        "evidence_refs": json_text([repair_text(value) for value in payload.evidence_refs if repair_text(value)]),
        "created_by": user_id,
    }
    if not params["statement"]:
        raise ValueError("statement is required")
    with engine.begin() as conn:
        if is_sqlite():
            inserted = conn.execute(
                text(
                    """
                    INSERT INTO multi_agent_human_submissions
                        (run_id,submission_id,round_no,side,statement,fact_refs_json,evidence_refs_json,created_by)
                    VALUES (:run_id,:submission_id,:round_no,:side,:statement,:fact_refs,:evidence_refs,:created_by)
                    ON CONFLICT(run_id,submission_id) DO NOTHING RETURNING id
                    """
                ),
                params,
            ).mappings().first()
        else:
            inserted = conn.execute(
                text(
                    """
                    INSERT INTO multi_agent_human_submissions
                        (run_id,submission_id,round_no,side,statement,fact_refs_json,evidence_refs_json,created_by)
                    VALUES (:run_id,:submission_id,:round_no,:side,:statement,
                            CAST(:fact_refs AS jsonb),CAST(:evidence_refs AS jsonb),:created_by)
                    ON CONFLICT(run_id,submission_id) DO NOTHING RETURNING id
                    """
                ),
                params,
            ).mappings().first()
        if inserted:
            conn.execute(
                text(
                    """
                    UPDATE multi_agent_runs
                    SET status='queued', current_stage='human_submission_received', claimed_by=NULL,
                        updated_at=CURRENT_TIMESTAMP, last_error=NULL
                    WHERE id=:run_id
                    """
                ),
                {"run_id": run_id},
            )
    if inserted:
        _append_event(
            run_id, "human_submission_received", f"已收到真人一方第 {round_no} 轮提交。",
            actor_type="human", actor_name=repair_text(row.get("human_side")),
            stage="human_turn", round_no=round_no,
            payload={
                "submission_id": submission_id,
                "fact_ref_count": len(payload.fact_refs),
                "evidence_ref_count": len(payload.evidence_refs),
            },
        )
    return get_legal_agent_run(run_id, user_id=user_id, tenant_id=tenant_id)


def cancel_legal_agent_run(run_id: int, *, user_id: int, tenant_id: str) -> dict[str, Any]:
    row = _owned_run(run_id, user_id=user_id, tenant_id=tenant_id)
    if repair_text(row.get("status")) in TERMINAL_RUN_STATUSES:
        return _serialize_run(row)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE multi_agent_runs
                SET cancel_requested=:cancel, status=CASE WHEN status IN ('queued','waiting_for_human') THEN 'cancelled' ELSE status END,
                    current_stage='cancel_requested', updated_at=CURRENT_TIMESTAMP,
                    finished_at=CASE WHEN status IN ('queued','waiting_for_human') THEN CURRENT_TIMESTAMP ELSE finished_at END
                WHERE id=:run_id
                """
            ),
            {"run_id": run_id, "cancel": 1 if is_sqlite() else True},
        )
    _append_event(run_id, "run_cancelled", "用户已请求停止运行。", stage="cancel_requested")
    return get_legal_agent_run(run_id, user_id=user_id, tenant_id=tenant_id)


def _compact_packet(packet: dict[str, Any]) -> dict[str, Any]:
    facts = []
    for group in ("asserted_facts", "supported_facts"):
        for item in packet.get(group) or []:
            facts.append(
                {
                    "fact_id": item.get("fact_id"),
                    "fact_type": item.get("fact_type"),
                    "content": item.get("content"),
                    "status": item.get("status"),
                    "source_id": item.get("source_id"),
                }
            )
    return {
        "revision": packet.get("revision"),
        "all_case_facts": facts,
        "disputed_or_missing_facts": [
            {
                "issue_id": item.get("issue_id"),
                "description": item.get("description"),
                "status": item.get("status"),
                "related_fact_ids": item.get("related_fact_ids") or [],
            }
            for item in packet.get("disputed_or_missing_facts") or []
        ],
        "legal_domains": packet.get("legal_domains") or [],
        "research_queries": packet.get("research_queries") or {},
    }


def _fallback_research_plan(packet: dict[str, Any], statement: str, evidence_count: int) -> ResearchPlan:
    queries = packet.get("research_queries") or {}
    case_query = repair_text(queries.get("case_query")) or repair_text(statement)[:600]
    rule_query = repair_text(queries.get("rule_query")) or repair_text(statement)[:600]
    actions = [
        ResearchAction(tool="search_case_law", query=case_query, reason="核对相似事实模式与裁判处理。"),
        ResearchAction(tool="search_statute", query=rule_query, reason="核对适用法规及构成要件。"),
        ResearchAction(tool="search_legal_graph", query=case_query, reason="沿已核验的案例、法规和争点关系补充全局资料。"),
    ]
    if evidence_count < 2 and getattr(settings, "remote_search_enabled", True):
        actions.append(ResearchAction(tool="search_approved_web", query=case_query, reason="本地材料不足时补查受控公开法律来源。"))
    return ResearchPlan(focus="围绕真人本轮陈述核对事实缺口、适用规则和相似案例。", actions=actions)


def _plan_research(
    packet: dict[str, Any],
    statement: str,
    *,
    agent_side: str,
    round_no: int,
    evidence_pool: list[dict[str, Any]],
) -> tuple[ResearchPlan, str]:
    fallback = _fallback_research_plan(packet, statement, len(evidence_pool))
    if not is_llm_configured():
        return fallback, "deterministic_fallback"
    user_input = json_text(
        {
            "agent_side": agent_side,
            "round_no": round_no,
            "case_packet": _compact_packet(packet),
            "human_submission": repair_text(statement),
            "existing_evidence": [
                {"evidence_id": item.get("evidence_id"), "title": item.get("title"), "citation": item.get("citation")}
                for item in evidence_pool[:12]
            ],
        }
    )
    try:
        response = create_structured_response(
            "interactive_advocate_research_plan_v1",
            ResearchPlan.model_json_schema(),
            (
                "You are the single AI legal advocate in a human-versus-agent Canadian legal simulation. "
                "Choose up to six narrow research actions that materially test the human submission. "
                "Use only search_case_law, search_statute, search_legal_graph, search_approved_web, and fetch_source_snapshot. "
                "Use graph search for relationship-based discovery, but treat graph paths only as candidates requiring source evidence. "
                "Prefer local case and statute search; use approved web search only for a real gap. "
                "Never add facts to the current case and do not reveal hidden chain-of-thought. Return concise action reasons."
            ),
            user_input,
        )
        plan = ResearchPlan.model_validate(response.get("data") or {})
        if not plan.actions:
            return fallback, "empty_model_plan_fallback"
        return plan, repair_text(response.get("model")) or "configured_model"
    except (LLMServiceError, ValidationError, TypeError, ValueError, KeyError):
        return fallback, "model_plan_fallback"


def _fallback_opinion(
    packet: dict[str, Any],
    evidence_pool: list[dict[str, Any]],
    *,
    agent_side: str,
) -> AdvocateOpinion:
    facts = packet.get("asserted_facts") or []
    issues = packet.get("disputed_or_missing_facts") or []
    evidence = evidence_pool[:4]
    fact_refs = [item.get("fact_id") for item in facts[:3] if item.get("fact_id")]
    evidence_refs = [item.get("evidence_id") for item in evidence[:3] if item.get("evidence_id")]
    issue = repair_text(issues[0].get("description")) if issues else "现有事实能否满足相应法律要件"
    claim = (
        f"从{'主张方' if agent_side == 'claimant' else '相对方'}立场看，当前首先需要围绕“{issue}”逐项核对事实与证据；"
        "现有检索材料只能提供法律背景和类案对照，不能替代本案事实证明。"
    )
    return AdvocateOpinion(
        position_summary=claim,
        arguments=[
            AgentArgument(
                argument_id="ARG-1",
                claim=claim,
                fact_refs=fact_refs,
                evidence_refs=evidence_refs,
                uncertainty="如关键时间线、原始文件或证据真实性尚未确认，本观点可能改变。",
            )
        ],
        challenges_to_human=["请说明关键陈述对应的原始证据及形成时间。"],
        missing_facts=[repair_text(item.get("description")) for item in issues[:3]],
        stop_reason="已达到本轮受控研究范围，等待真人回应或中立裁判复核。",
    )


def _generate_advocate_opinion(
    packet: dict[str, Any],
    statement: str,
    evidence_pool: list[dict[str, Any]],
    *,
    agent_side: str,
    round_no: int,
) -> tuple[dict[str, Any], str]:
    fallback = _fallback_opinion(packet, evidence_pool, agent_side=agent_side)
    if not is_llm_configured():
        opinion = fallback
        model_name = "deterministic_fallback"
    else:
        user_input = json_text(
            {
                "agent_side": agent_side,
                "round_no": round_no,
                "case_packet": _compact_packet(packet),
                "human_submission": repair_text(statement),
                "evidence_pool": [
                    {
                        "evidence_id": item.get("evidence_id"), "title": item.get("title"),
                        "citation": item.get("citation"), "excerpt": repair_text(item.get("excerpt"))[:900],
                        "verification_status": item.get("verification_status"),
                    }
                    for item in evidence_pool[:16]
                ],
            }
        )
        try:
            response = create_structured_response(
                "interactive_advocate_opinion_v1",
                AdvocateOpinion.model_json_schema(),
                build_role_instruction("opponent", (
                    "Act only for the configured AI side. Respond to the human side using every material fact in the CasePacket. "
                    "Each argument must cite exact fact_refs and/or evidence_refs from the supplied IDs. "
                    "Treat external cases as analogy, never as current-case facts. Identify uncertainty and missing facts. "
                    "Do not invent the human position, sources, quotations, outcomes, or IDs."
                )),
                user_input,
            )
            opinion = AdvocateOpinion.model_validate(response.get("data") or {})
            model_name = repair_text(response.get("model")) or "configured_model"
        except (LLMServiceError, ValidationError, TypeError, ValueError, KeyError):
            opinion = fallback
            model_name = "model_opinion_fallback"
    refs = packet_reference_ids(packet)
    evidence_ids = {
        repair_text(item.get("evidence_id")) for item in evidence_pool if repair_text(item.get("evidence_id"))
    }
    data = opinion.model_dump(mode="json")
    blocked_citations: list[str] = []
    for key in ("position_summary", "stop_reason"):
        guarded = enforce_grounded_legal_text(data.get(key), evidence_pool)
        data[key] = guarded["safe_text"]
        blocked_citations.extend(guarded["unverified"])
    for index, argument in enumerate(data.get("arguments") or [], start=1):
        argument["argument_id"] = repair_text(argument.get("argument_id")) or f"ARG-{round_no}-{index}"
        argument["fact_refs"] = [value for value in argument.get("fact_refs") or [] if value in refs["fact_ids"]]
        argument["evidence_refs"] = [value for value in argument.get("evidence_refs") or [] if value in evidence_ids]
        argument["grounding_status"] = "grounded" if argument["fact_refs"] or argument["evidence_refs"] else "unsupported"
        guarded = enforce_grounded_legal_text(argument.get("claim"), evidence_pool)
        argument["claim"] = guarded["safe_text"]
        blocked_citations.extend(guarded["unverified"])
    data["citation_guard"] = {
        "passed": not blocked_citations,
        "blocked_count": len(blocked_citations),
        "unverified_citations": list(dict.fromkeys(blocked_citations)),
    }
    data["model_name"] = model_name
    data["case_packet_revision"] = packet.get("revision")
    return data, model_name


def _run_advocate_round(
    run_id: int,
    request_payload: dict[str, Any],
    state: dict[str, Any],
    submission: dict[str, Any],
    *,
    agent_side: str,
    round_no: int,
) -> dict[str, Any]:
    packet = state["case_packet"]
    evidence_pool = state.get("evidence_pool") or []
    round_started = time.monotonic()
    tool_calls = 0
    research_cycles: list[dict[str, Any]] = []
    max_tool_calls = min(6, max(1, int(request_payload.get("max_steps") or 12) - 4), max(1, int(request_payload.get("token_budget") or 10000) // 1500))
    timeout_seconds = max(15, int(request_payload.get("timeout_seconds") or 120))
    plan_model = ""
    for cycle_no in range(1, 4):
        if tool_calls >= max_tool_calls or time.monotonic() - round_started >= timeout_seconds:
            break
        plan, plan_model = _plan_research(
            packet,
            submission["statement"],
            agent_side=agent_side,
            round_no=round_no,
            evidence_pool=evidence_pool,
        )
        research_cycles.append(plan.model_dump(mode="json"))
        _append_event(
            run_id, "research_plan_created", f"代理 Agent 已制定第 {round_no} 轮第 {cycle_no} 次研究计划。",
            actor_type="agent", actor_name="advocate_agent", stage="agent_research", round_no=round_no,
            payload={
                "cycle": cycle_no, "focus": plan.focus,
                "actions": [{"tool": action.tool, "query": action.query, "reason": action.reason} for action in plan.actions],
                "known_gaps": plan.known_gaps, "stop_research": plan.stop_research,
                "stop_reason": plan.stop_reason, "model": plan_model,
            },
        )
        if plan.stop_research and evidence_pool:
            break
        cycle_added = 0
        for action in plan.actions[: max_tool_calls - tool_calls]:
            row = _load_run_row(run_id)
            if row and bool(row.get("cancel_requested")):
                break
            if time.monotonic() - round_started >= timeout_seconds:
                break
            tool_calls += 1
            _append_event(
                run_id, "tool_called", f"代理 Agent 调用 {action.tool}。",
                actor_type="tool", actor_name=action.tool, stage="agent_research", round_no=round_no,
                payload={"query": action.query, "purpose": action.reason, "cycle": cycle_no},
            )
            started = time.monotonic()
            try:
                tool_result = execute_research_tool(
                    agent="advocate_agent",
                    tool_name=action.tool,
                    query=action.query,
                    module=normalize_module(request_payload.get("module") or "canada"),
                    filters=_safe_filters(request_payload.get("filters") or {}),
                    case_acl=list(request_payload.get("case_acl") or []),
                    limit=6,
                )
                incoming = tool_result.get("evidence") or []
                before_ids = {item.get("evidence_id") for item in evidence_pool}
                evidence_pool = merge_evidence(evidence_pool, incoming)
                after_ids = {item.get("evidence_id") for item in evidence_pool}
                added = len(after_ids - before_ids)
                cycle_added += added
                _append_event(
                    run_id, "tool_completed", f"{action.tool} 返回 {len(incoming)} 条，证据池新增 {added} 条。",
                    actor_type="tool", actor_name=action.tool, stage="agent_research", round_no=round_no,
                    payload={
                        "status": tool_result.get("status"), "returned": len(incoming), "added": added,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                        "evidence_ids": [item.get("evidence_id") for item in incoming[:8]],
                        "graph_path_count": int(tool_result.get("path_count") or 0),
                        "graph_entry_entities": [
                            item.get("name") for item in (tool_result.get("entry_entities") or [])[:6]
                        ],
                    },
                )
                for item in incoming[:8]:
                    _append_event(
                        run_id, "evidence_added", repair_text(item.get("title")) or "新增法律资料",
                        actor_type="tool", actor_name=action.tool, stage="evidence_pool", round_no=round_no,
                        payload={
                            "evidence_id": item.get("evidence_id"), "citation": item.get("citation"),
                            "source_url": item.get("source_url"), "verification_status": item.get("verification_status"),
                            "retrieval_channels": item.get("retrieval_channels") or [],
                            "context_chunk_count": len(item.get("context_chunk_ids") or []),
                            "graph_path": item.get("graph_path") or {},
                        },
                    )
            except Exception as exc:
                _append_event(
                    run_id, "tool_completed", f"{action.tool} 本轮未完成，流程继续使用已核验材料。",
                    actor_type="tool", actor_name=action.tool, stage="agent_research", round_no=round_no,
                    payload={"status": "error", "error": repair_text(str(exc))[:300]},
                )
        packet = bind_external_research(packet, evidence_pool)
        if plan_model in {"deterministic_fallback", "model_plan_fallback", "empty_model_plan_fallback"} or cycle_added == 0:
            break
    packet = bind_external_research(packet, evidence_pool)
    opinion, model_name = _generate_advocate_opinion(
        packet,
        submission["statement"],
        evidence_pool,
        agent_side=agent_side,
        round_no=round_no,
    )
    round_result = {
        "round_no": round_no,
        "side": agent_side,
        "human_submission_id": submission["submission_id"],
        "research_cycles": research_cycles,
        "tool_call_count": tool_calls,
        "opinion": opinion,
        "model_name": model_name,
        "completed_at": _utc_now(),
    }
    state["case_packet"] = packet
    state["evidence_pool"] = evidence_pool
    state.setdefault("agent_rounds", []).append(round_result)
    _append_event(
        run_id, "agent_completed", f"代理 Agent 已完成第 {round_no} 轮回应。",
        actor_type="agent", actor_name="advocate_agent", stage="agent_response", round_no=round_no,
        payload={
            "argument_count": len(opinion.get("arguments") or []),
            "challenge_count": len(opinion.get("challenges_to_human") or []),
            "stop_reason": opinion.get("stop_reason"),
        },
    )
    return state


def _fallback_judge(packet: dict[str, Any], agent_rounds: list[dict[str, Any]], submissions: list[dict[str, Any]]) -> JudgeDecision:
    issues = packet.get("disputed_or_missing_facts") or []
    facts = packet.get("asserted_facts") or []
    evidence = packet.get("external_research") or []
    human_text = repair_text(submissions[-1].get("statement")) if submissions else ""
    agent_text = repair_text((agent_rounds[-1].get("opinion") or {}).get("position_summary")) if agent_rounds else ""
    findings = []
    for issue in issues[:4]:
        findings.append(
            JudgeFinding(
                issue=repair_text(issue.get("description")) or "待核实争点",
                human_position=human_text[:700],
                agent_position=agent_text[:700],
                analysis="现有材料可用于识别争点，但仍需按本案证据逐项证明，外部类案只提供类比参考。",
                fact_refs=[item.get("fact_id") for item in facts[:3] if item.get("fact_id")],
                evidence_refs=[item.get("evidence_id") for item in evidence[:3] if item.get("evidence_id")],
                status="contested" if facts and evidence else "insufficient",
            )
        )
    return JudgeDecision(
        conclusion="双方观点已经进入同一事实与证据框架，但现阶段结论仍需律师结合原始材料复核；系统不作确定性胜负判断。",
        findings=findings,
        uncertainties=[repair_text(item.get("description")) for item in issues[:5]],
        further_review=["核对关键事实对应的原始文件、形成时间和真实性。", "由执业律师复核适用法、程序期限及救济范围。"],
        confidence=min(0.65, 0.2 + len(evidence) * 0.05),
    )


def _generate_judge_decision(
    packet: dict[str, Any],
    evidence_pool: list[dict[str, Any]],
    agent_rounds: list[dict[str, Any]],
    submissions: list[dict[str, Any]],
    *,
    human_side: str,
    agent_side: str,
) -> tuple[dict[str, Any], str]:
    fallback = _fallback_judge(packet, agent_rounds, submissions)
    if not is_llm_configured():
        decision = fallback
        model_name = "deterministic_fallback"
    else:
        user_input = json_text(
            {
                "human_side": human_side,
                "agent_side": agent_side,
                "case_packet": _compact_packet(packet),
                "human_submissions": [
                    {"round_no": row.get("round_no"), "statement": row.get("statement"), "evidence_refs": row.get("evidence_refs")}
                    for row in submissions
                ],
                "agent_rounds": agent_rounds,
                "verified_evidence": [
                    {
                        "evidence_id": item.get("evidence_id"), "title": item.get("title"),
                        "citation": item.get("citation"), "excerpt": repair_text(item.get("excerpt"))[:700],
                        "verification_status": item.get("verification_status"),
                    }
                    for item in evidence_pool[:18]
                ],
            }
        )
        try:
            response = create_structured_response(
                "interactive_neutral_judge_decision_v1",
                JudgeDecision.model_json_schema(),
                build_role_instruction("adjudication_judge", (
                    "You are a neutral Canadian legal decision-support judge. Apply exactly the same proof and citation standard "
                    "to the human side and AI side. Use the complete supplied CasePacket, submissions, agent opinions, and verified evidence. "
                    "Do not infer user preferences, do not reward verbosity, and do not invent facts, authorities, quotations, or IDs. "
                    "External cases are analogy only. Present support, opposition, uncertainty, and missing evidence. "
                    "Never issue a definitive legal judgment; produce a review-required assessment."
                )),
                user_input,
            )
            decision = JudgeDecision.model_validate(response.get("data") or {})
            model_name = repair_text(response.get("model")) or "configured_model"
        except (LLMServiceError, ValidationError, TypeError, ValueError, KeyError):
            decision = fallback
            model_name = "model_judge_fallback"
    refs = packet_reference_ids(packet)
    evidence_ids = {repair_text(item.get("evidence_id")) for item in evidence_pool if repair_text(item.get("evidence_id"))}
    data = decision.model_dump(mode="json")
    blocked_citations: list[str] = []
    guarded = enforce_grounded_legal_text(data.get("conclusion"), evidence_pool)
    data["conclusion"] = guarded["safe_text"]
    blocked_citations.extend(guarded["unverified"])
    for finding in data.get("findings") or []:
        finding["fact_refs"] = [value for value in finding.get("fact_refs") or [] if value in refs["fact_ids"]]
        finding["evidence_refs"] = [value for value in finding.get("evidence_refs") or [] if value in evidence_ids]
        for key in ("issue", "human_position", "agent_position", "analysis"):
            guarded = enforce_grounded_legal_text(finding.get(key), evidence_pool)
            finding[key] = guarded["safe_text"]
            blocked_citations.extend(guarded["unverified"])
    data["citation_guard"] = {
        "passed": not blocked_citations,
        "blocked_count": len(blocked_citations),
        "unverified_citations": list(dict.fromkeys(blocked_citations)),
    }
    data["model_name"] = model_name
    data["case_packet_revision"] = packet.get("revision")
    return data, model_name


def _rule_gate(
    packet: dict[str, Any],
    evidence_pool: list[dict[str, Any]],
    verification: list[dict[str, Any]],
    agent_rounds: list[dict[str, Any]],
) -> dict[str, Any]:
    blockers: list[str] = []
    if not packet.get("asserted_facts") and not packet.get("supported_facts"):
        blockers.append("没有可追溯的本案事实。")
    verified_ids = {
        item["evidence_id"] for item in verification
        if item.get("verdict") != "needs_manual_citation_check" and item.get("evidence_id")
    }
    if not evidence_pool:
        blockers.append("没有检索到外部法律资料。")
    elif not verified_ids:
        blockers.append("检索资料缺少可核验引用、URL 或快照。")
    unsupported = []
    for round_item in agent_rounds:
        citation_guard = (round_item.get("opinion") or {}).get("citation_guard") or {}
        if citation_guard.get("blocked_count"):
            blockers.append("代理观点曾包含未被检索资料支持的法条或判例编号，已拦截并要求人工复核。")
        for argument in (round_item.get("opinion") or {}).get("arguments") or []:
            if argument.get("grounding_status") != "grounded":
                unsupported.append(argument.get("argument_id"))
    if unsupported:
        blockers.append(f"存在未绑定事实或证据的代理观点：{', '.join(filter(None, unsupported))}。")
    return {
        "passed": not blockers,
        "decision_status": "review_required" if not blockers else "insufficient_evidence",
        "blocked_reasons": blockers,
        "verified_evidence_count": len(verified_ids),
        "checked_at": _utc_now(),
        "rule": "No certain conclusion without traceable case facts and verified legal sources.",
    }


def _neutral_supplement_if_needed(
    run_id: int,
    request_payload: dict[str, Any],
    state: dict[str, Any],
    round_no: int,
) -> dict[str, Any]:
    verification = verify_evidence_pool(state.get("evidence_pool") or [])
    verified_count = sum(item.get("verdict") != "needs_manual_citation_check" for item in verification)
    if verified_count >= 2:
        state["verification"] = verification
        return state
    packet = state.get("case_packet") or {}
    query = repair_text((packet.get("research_queries") or {}).get("rule_query"))
    if not query:
        state["verification"] = verification
        return state
    _append_event(
        run_id, "tool_called", "中立法官发起一次补充法规检索。",
        actor_type="agent", actor_name="neutral_judge_agent", stage="neutral_research", round_no=round_no,
        payload={"query": query, "purpose": "补齐中立裁判所需的最低可核验法律依据。"},
    )
    try:
        result = execute_research_tool(
            agent="neutral_judge_agent",
            tool_name="search_statute",
            query=query,
            module=normalize_module(request_payload.get("module") or "canada"),
            filters=_safe_filters(request_payload.get("filters") or {}),
            case_acl=list(request_payload.get("case_acl") or []),
            limit=4,
        )
        state["evidence_pool"] = merge_evidence(state.get("evidence_pool") or [], result.get("evidence") or [])
        state["case_packet"] = bind_external_research(packet, state["evidence_pool"])
        _append_event(
            run_id, "tool_completed", f"中立补查返回 {len(result.get('evidence') or [])} 条资料。",
            actor_type="tool", actor_name="search_statute", stage="neutral_research", round_no=round_no,
            payload={"returned": len(result.get("evidence") or [])},
        )
    except Exception as exc:
        _append_event(
            run_id, "tool_completed", "中立补查未完成，裁判将明确保留不确定性。",
            actor_type="tool", actor_name="search_statute", stage="neutral_research", round_no=round_no,
            payload={"status": "error", "error": repair_text(str(exc))[:300]},
        )
    state["verification"] = verify_evidence_pool(state.get("evidence_pool") or [])
    return state


def _record_user_preference_history(row: dict[str, Any], result: dict[str, Any]) -> None:
    request_payload = _decode_json(row.get("request_json"), {})
    if not (
        request_payload.get("preference_history_enabled")
        or request_payload.get("review_memory_enabled")
    ):
        return
    tenant_id = repair_text(row.get("tenant_id"))
    user_id = int(row.get("user_id") or 0)
    if not user_id:
        return
    observation = build_review_observation(
        run_id=int(row["id"]),
        request_payload=request_payload,
        row=row,
        result=result,
    )
    record_preference_observation(
        tenant_id=tenant_id,
        user_id=user_id,
        observation=observation,
    )


def _finalize_run(row: dict[str, Any], request_payload: dict[str, Any], state: dict[str, Any]) -> None:
    run_id = int(row["id"])
    round_no = int(row.get("current_round") or 1)
    _append_event(run_id, "stage_started", "开始引用核验与中立评议。", stage="verification", round_no=round_no)
    state = _neutral_supplement_if_needed(run_id, request_payload, state, round_no)
    verification = verify_evidence_pool(state.get("evidence_pool") or [])
    state["verification"] = verification
    failures = [item for item in verification if item.get("verdict") == "needs_manual_citation_check"]
    if failures:
        _append_event(
            run_id, "verification_failed", f"有 {len(failures)} 条资料仍需人工核验引用。",
            actor_type="tool", actor_name="citation_verification", stage="verification", round_no=round_no,
            payload={"evidence_ids": [item.get("evidence_id") for item in failures]},
        )
    _append_event(
        run_id, "agent_started", "中立法官开始评议双方材料。",
        actor_type="agent", actor_name="neutral_judge_agent", stage="judicial_review", round_no=round_no,
    )
    submissions = _list_submissions(run_id)
    judge, judge_model = _generate_judge_decision(
        state.get("case_packet") or {},
        state.get("evidence_pool") or [],
        state.get("agent_rounds") or [],
        submissions,
        human_side=repair_text(row.get("human_side")),
        agent_side=repair_text(row.get("agent_side")),
    )
    gate = _rule_gate(
        state.get("case_packet") or {},
        state.get("evidence_pool") or [],
        verification,
        state.get("agent_rounds") or [],
    )
    event_type = "agent_completed" if gate["passed"] else "gate_blocked"
    _append_event(
        run_id, event_type,
        "中立评议完成，结论需人工复核。" if gate["passed"] else "规则闸门阻止确定性结论。",
        actor_type="agent", actor_name="neutral_judge_agent", stage="rule_gate", round_no=round_no,
        payload={"gate": gate, "model": judge_model, "finding_count": len(judge.get("findings") or [])},
    )
    result = {
        "human_side": repair_text(row.get("human_side")),
        "agent_side": repair_text(row.get("agent_side")),
        "judge": judge,
        "gate": gate,
        "human_submissions": submissions,
        "agent_rounds": state.get("agent_rounds") or [],
        "verification": verification,
        "completed_at": _utc_now(),
        "review_required": True,
    }
    _write_run(
        run_id,
        status="completed",
        current_stage="completed",
        current_round=round_no,
        state=state,
        result=result,
        finished=True,
    )
    _append_event(run_id, "run_completed", "人机审理流程已完成。", stage="completed", round_no=round_no)
    _record_user_preference_history(row, result)


def _process_claimed_run(row: dict[str, Any]) -> None:
    run_id = int(row["id"])
    started = time.monotonic()
    request_payload = _decode_json(row.get("request_json"), {})
    state = _decode_json(row.get("state_json"), {})
    state.setdefault("case_packet", {})
    state.setdefault("evidence_pool", [])
    state.setdefault("agent_rounds", [])
    state.setdefault("verification", [])
    state.setdefault("processed_submission_ids", [])
    round_no = int(row.get("current_round") or 1)
    if bool(row.get("cancel_requested")):
        _write_run(run_id, status="cancelled", current_stage="cancelled", current_round=round_no, state=state, finished=True)
        return
    if not state.get("case_packet"):
        _append_event(run_id, "stage_started", "开始建立共享事实包。", stage="case_intake", round_no=round_no)
        _append_event(
            run_id, "agent_started", "案情整理 Agent 正在提取可追溯事实与争议点。",
            actor_type="agent", actor_name="case_clerk_agent", stage="case_intake", round_no=round_no,
        )
        state["case_packet"] = build_case_packet(repair_text(row.get("query_text")), use_llm=True)
        _append_event(
            run_id, "agent_completed", "共享事实包已建立。",
            actor_type="agent", actor_name="case_clerk_agent", stage="case_intake", round_no=round_no,
            payload={
                "revision": state["case_packet"].get("revision"),
                "asserted_fact_count": len(state["case_packet"].get("asserted_facts") or []),
                "issue_count": len(state["case_packet"].get("disputed_or_missing_facts") or []),
            },
        )
        _write_run(run_id, status="running", current_stage="awaiting_human", current_round=round_no, state=state)
    submission = _submission_for_round(run_id, round_no)
    if not submission:
        state["duration_ms"] = int(state.get("duration_ms") or 0) + int((time.monotonic() - started) * 1000)
        _write_run(
            run_id, status="waiting_for_human", current_stage="waiting_for_human",
            current_round=round_no, state=state,
        )
        _append_event(
            run_id, "human_action_required", f"等待真人一方提交第 {round_no} 轮意见。",
            actor_type="human", actor_name=repair_text(row.get("human_side")),
            stage="waiting_for_human", round_no=round_no,
        )
        return
    processed = set(state.get("processed_submission_ids") or [])
    if submission["submission_id"] not in processed:
        state["case_packet"] = merge_human_submission(
            state["case_packet"],
            submission["statement"],
            submission_id=submission["submission_id"],
            evidence_refs=submission.get("evidence_refs") or [],
        )
        processed.add(submission["submission_id"])
        state["processed_submission_ids"] = sorted(processed)
    existing_round = next((item for item in state["agent_rounds"] if int(item.get("round_no") or 0) == round_no), None)
    if not existing_round:
        _append_event(
            run_id, "agent_started", f"单方代理 Agent 开始第 {round_no} 轮研究与回应。",
            actor_type="agent", actor_name="advocate_agent", stage="agent_research", round_no=round_no,
        )
        state = _run_advocate_round(
            run_id,
            request_payload,
            state,
            submission,
            agent_side=repair_text(row.get("agent_side")),
            round_no=round_no,
        )
        state["duration_ms"] = int(state.get("duration_ms") or 0) + int((time.monotonic() - started) * 1000)
        _write_run(run_id, status="running", current_stage="agent_response", current_round=round_no, state=state)
    refreshed = _load_run_row(run_id) or row
    if bool(refreshed.get("cancel_requested")):
        _write_run(run_id, status="cancelled", current_stage="cancelled", current_round=round_no, state=state, finished=True)
        return
    max_rounds = int(row.get("max_rounds") or request_payload.get("max_rounds") or 1)
    if round_no < max_rounds:
        next_round = round_no + 1
        _write_run(
            run_id, status="waiting_for_human", current_stage="waiting_for_human",
            current_round=next_round, state=state,
        )
        _append_event(
            run_id, "human_action_required", f"代理 Agent 已回应，等待真人一方提交第 {next_round} 轮意见。",
            actor_type="human", actor_name=repair_text(row.get("human_side")),
            stage="waiting_for_human", round_no=next_round,
        )
        return
    final_row = _load_run_row(run_id) or row
    _finalize_run(final_row, request_payload, state)


def _worker_identity() -> str:
    configured = repair_text(getattr(settings, "agent_worker_name", ""))
    return configured or f"agent-{socket.gethostname()}-{os.getpid()}"


def _claim_next_run(worker_name: str) -> dict[str, Any] | None:
    stale_seconds = max(120, int(getattr(settings, "agent_worker_stale_seconds", 900)))
    cutoff = datetime.utcnow() - timedelta(seconds=stale_seconds)
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE multi_agent_runs
                SET status='queued', claimed_by=NULL, last_error=COALESCE(last_error,'stale_worker_requeue'),
                    updated_at=CURRENT_TIMESTAMP
                WHERE graph_version=:version AND status='running'
                  AND COALESCE(last_heartbeat,started_at,created_at)<:cutoff
                """
            ),
            {"version": INTERACTIVE_GRAPH_VERSION, "cutoff": cutoff},
        )
        suffix = "" if is_sqlite() else " FOR UPDATE SKIP LOCKED"
        row = conn.execute(
            text(
                """
                SELECT * FROM multi_agent_runs
                WHERE graph_version=:version AND status='queued'
                ORDER BY created_at ASC LIMIT 1
                """ + suffix
            ),
            {"version": INTERACTIVE_GRAPH_VERSION},
        ).mappings().first()
        if not row:
            return None
        result = conn.execute(
            text(
                """
                UPDATE multi_agent_runs
                SET status='running', claimed_by=:worker, started_at=COALESCE(started_at,CURRENT_TIMESTAMP),
                    last_heartbeat=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                WHERE id=:run_id AND status='queued'
                """
            ),
            {"worker": worker_name, "run_id": row["id"]},
        )
        if result.rowcount != 1:
            return None
        _append_event(
            int(row["id"]), "run_started", "Worker 已认领任务并开始处理。",
            actor_name=worker_name[:80], stage=row.get("current_stage") or "case_intake",
            round_no=int(row.get("current_round") or 1), connection=conn,
        )
        claimed = conn.execute(text("SELECT * FROM multi_agent_runs WHERE id=:run_id"), {"run_id": row["id"]}).mappings().first()
    return dict(claimed) if claimed else None


def process_next_legal_agent_run(worker_name: str | None = None) -> bool:
    ensure_legal_agent_runtime_tables()
    resolved_worker = repair_text(worker_name) or _worker_identity()
    row = _claim_next_run(resolved_worker)
    if not row:
        return False
    run_id = int(row["id"])
    try:
        _process_claimed_run(row)
    except Exception as exc:
        state = _decode_json(row.get("state_json"), {})
        _write_run(
            run_id,
            status="failed",
            current_stage="failed",
            current_round=int(row.get("current_round") or 1),
            state=state,
            last_error=repair_text(str(exc))[:1200],
            finished=True,
        )
        _append_event(
            run_id, "run_failed", "运行失败，已保留全部已完成事件供审计。",
            stage="failed", round_no=int(row.get("current_round") or 1),
            payload={"error": repair_text(str(exc))[:500]},
        )
    return True


def run_legal_agent_worker_forever(
    *,
    worker_name: str | None = None,
    stop_event: threading.Event | None = None,
    poll_seconds: float | None = None,
) -> None:
    ensure_legal_agent_runtime_tables()
    stop = stop_event or threading.Event()
    wait_seconds = max(0.25, float(poll_seconds or getattr(settings, "agent_worker_poll_seconds", 0.8)))
    while not stop.is_set():
        try:
            if process_next_legal_agent_run(worker_name):
                continue
        except OperationalError as exc:
            logging.getLogger(__name__).warning("Legal agent worker database poll failed (%s)", type(exc.orig).__name__)
            stop.wait(max(wait_seconds, 5.0))
            continue
        stop.wait(wait_seconds)


def schedule_legal_agent_worker() -> bool:
    global _WORKER_THREAD
    if not getattr(settings, "agent_worker_in_process_enabled", True):
        return False
    if _WORKER_THREAD and _WORKER_THREAD.is_alive():
        return True
    _WORKER_STOP.clear()
    _WORKER_THREAD = threading.Thread(
        target=run_legal_agent_worker_forever,
        kwargs={"worker_name": _worker_identity(), "stop_event": _WORKER_STOP},
        name="legal-agent-worker",
        daemon=True,
    )
    _WORKER_THREAD.start()
    return True


def stop_legal_agent_worker() -> None:
    _WORKER_STOP.set()
