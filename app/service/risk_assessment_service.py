from __future__ import annotations

import json

from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text
from app.service.module_service import normalize_module


def _json_text(payload) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, default=str)


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [value]


def _risk_level(prediction: dict) -> str:
    evidence_status = repair_text(prediction.get("evidence_status")).lower()
    readiness = prediction.get("data_readiness") or {}
    evidence_quality = prediction.get("evidence_quality") or {}
    warnings = _as_list(prediction.get("reliability_warnings")) + _as_list(evidence_quality.get("warnings"))
    risk_points = _as_list(prediction.get("risk_points")) + _as_list(prediction.get("caveats"))
    confidence = float(prediction.get("confidence") or 0)

    if evidence_status == "insufficient" or readiness.get("status") in {"insufficient", "partial"}:
        return "high"
    if evidence_quality.get("status") in {"thin", "insufficient"}:
        return "high"
    if len(warnings) >= 2 or len(risk_points) >= 4 or confidence < 0.35:
        return "high"
    if len(warnings) == 1 or len(risk_points) >= 2 or confidence < 0.55:
        return "medium"
    return "low"


def ensure_risk_assessment_tables() -> None:
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS risk_assessment_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_run_id BIGINT NULL,
                agent_prediction_id BIGINT NULL,
                module_code VARCHAR(60) NOT NULL DEFAULT 'canada',
                input_text TEXT NOT NULL DEFAULT '',
                risk_level VARCHAR(30) NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                evidence_status VARCHAR(40) NOT NULL DEFAULT '',
                evidence_quality_status VARCHAR(40) NOT NULL DEFAULT '',
                jurisdiction TEXT NOT NULL DEFAULT '',
                requested_relief TEXT NOT NULL DEFAULT '',
                disputed_issues_json TEXT NOT NULL DEFAULT '[]',
                retrieved_evidence_json TEXT NOT NULL DEFAULT '[]',
                prediction_json TEXT NOT NULL DEFAULT '{}',
                label_status VARCHAR(30) NOT NULL DEFAULT 'unlabeled',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_risk_samples_module_created ON risk_assessment_samples(module_code, created_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS risk_feedback_labels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sample_id BIGINT NOT NULL,
                human_risk_level VARCHAR(30) NOT NULL DEFAULT '',
                human_outcome TEXT NOT NULL DEFAULT '',
                human_notes TEXT NOT NULL DEFAULT '',
                label_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
        ]
    else:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS risk_assessment_samples (
                id BIGSERIAL PRIMARY KEY,
                agent_run_id BIGINT NULL REFERENCES agent_runs(id) ON DELETE SET NULL,
                agent_prediction_id BIGINT NULL REFERENCES agent_predictions(id) ON DELETE SET NULL,
                module_code VARCHAR(60) NOT NULL DEFAULT 'canada',
                input_text TEXT NOT NULL DEFAULT '',
                risk_level VARCHAR(30) NOT NULL DEFAULT '',
                confidence NUMERIC(5,4) NOT NULL DEFAULT 0,
                evidence_status VARCHAR(40) NOT NULL DEFAULT '',
                evidence_quality_status VARCHAR(40) NOT NULL DEFAULT '',
                jurisdiction TEXT NOT NULL DEFAULT '',
                requested_relief TEXT NOT NULL DEFAULT '',
                disputed_issues_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                retrieved_evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                prediction_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                label_status VARCHAR(30) NOT NULL DEFAULT 'unlabeled',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_risk_samples_module_created ON risk_assessment_samples(module_code, created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_risk_samples_level ON risk_assessment_samples(risk_level, evidence_status)",
            "CREATE INDEX IF NOT EXISTS idx_risk_samples_prediction_gin ON risk_assessment_samples USING GIN(prediction_json)",
            """
            CREATE TABLE IF NOT EXISTS risk_feedback_labels (
                id BIGSERIAL PRIMARY KEY,
                sample_id BIGINT NOT NULL REFERENCES risk_assessment_samples(id) ON DELETE CASCADE,
                human_risk_level VARCHAR(30) NOT NULL DEFAULT '',
                human_outcome TEXT NOT NULL DEFAULT '',
                human_notes TEXT NOT NULL DEFAULT '',
                label_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_risk_labels_sample ON risk_feedback_labels(sample_id, created_at DESC)",
        ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def record_risk_assessment_sample(
    *,
    analysis_payload: dict,
    prediction: dict,
    agent_run_id: int | None = None,
    agent_prediction_id: int | None = None,
    db_conn=None,
) -> int | None:
    ensure_risk_assessment_tables()
    analysis = analysis_payload.get("analysis") or {}
    rag_context = analysis_payload.get("rag_context") or {}
    module_code = normalize_module(analysis_payload.get("module_code"))
    evidence_quality = prediction.get("evidence_quality") or {}
    params = {
        "agent_run_id": agent_run_id,
        "agent_prediction_id": agent_prediction_id,
        "module_code": module_code,
        "input_text": repair_text(analysis_payload.get("input_text")),
        "risk_level": _risk_level(prediction),
        "confidence": float(prediction.get("confidence") or 0),
        "evidence_status": repair_text(prediction.get("evidence_status")),
        "evidence_quality_status": repair_text(evidence_quality.get("status")),
        "jurisdiction": repair_text(prediction.get("jurisdiction") or analysis.get("jurisdiction")),
        "requested_relief": repair_text(prediction.get("requested_relief") or analysis.get("requested_relief")),
        "disputed_issues": _json_text(prediction.get("disputed_issues") or analysis.get("disputed_issues") or []),
        "retrieved_evidence": _json_text((rag_context.get("items") or [])[:12]),
        "prediction": _json_text(prediction),
    }
    if is_sqlite():
        sql = """
        INSERT INTO risk_assessment_samples (
            agent_run_id, agent_prediction_id, module_code, input_text, risk_level,
            confidence, evidence_status, evidence_quality_status, jurisdiction,
            requested_relief, disputed_issues_json, retrieved_evidence_json, prediction_json
        )
        VALUES (
            :agent_run_id, :agent_prediction_id, :module_code, :input_text, :risk_level,
            :confidence, :evidence_status, :evidence_quality_status, :jurisdiction,
            :requested_relief, :disputed_issues, :retrieved_evidence, :prediction
        )
        """
    else:
        sql = """
        INSERT INTO risk_assessment_samples (
            agent_run_id, agent_prediction_id, module_code, input_text, risk_level,
            confidence, evidence_status, evidence_quality_status, jurisdiction,
            requested_relief, disputed_issues_json, retrieved_evidence_json, prediction_json
        )
        VALUES (
            :agent_run_id, :agent_prediction_id, :module_code, :input_text, :risk_level,
            :confidence, :evidence_status, :evidence_quality_status, :jurisdiction,
            :requested_relief, CAST(:disputed_issues AS jsonb),
            CAST(:retrieved_evidence AS jsonb), CAST(:prediction AS jsonb)
        )
        RETURNING id
        """
    if db_conn is not None:
        row = db_conn.execute(text(sql), params).mappings().first() if not is_sqlite() else None
        return int(row["id"]) if row else None

    with engine.begin() as conn:
        row = conn.execute(text(sql), params).mappings().first() if not is_sqlite() else None
    return int(row["id"]) if row else None


def list_risk_training_samples(limit: int = 100) -> list[dict]:
    ensure_risk_assessment_tables()
    limit = max(1, min(int(limit or 100), 1000))
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, agent_run_id, agent_prediction_id, module_code, input_text,
                       risk_level, confidence, evidence_status, evidence_quality_status,
                       jurisdiction, requested_relief, disputed_issues_json,
                       retrieved_evidence_json, prediction_json, label_status, created_at
                FROM risk_assessment_samples
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            {"limit": limit},
        ).mappings().all()
    return [dict(row) for row in rows]


def record_risk_feedback_label(
    *,
    sample_id: int,
    human_risk_level: str,
    human_outcome: str = "",
    human_notes: str = "",
    label_json: dict | None = None,
) -> int | None:
    ensure_risk_assessment_tables()
    params = {
        "sample_id": int(sample_id),
        "human_risk_level": repair_text(human_risk_level),
        "human_outcome": repair_text(human_outcome),
        "human_notes": repair_text(human_notes),
        "label_json": _json_text(label_json or {}),
    }
    if is_sqlite():
        sql = """
        INSERT INTO risk_feedback_labels (
            sample_id, human_risk_level, human_outcome, human_notes, label_json, created_at
        )
        VALUES (
            :sample_id, :human_risk_level, :human_outcome, :human_notes, :label_json, CURRENT_TIMESTAMP
        )
        """
    else:
        sql = """
        INSERT INTO risk_feedback_labels (
            sample_id, human_risk_level, human_outcome, human_notes, label_json, created_at
        )
        VALUES (
            :sample_id, :human_risk_level, :human_outcome, :human_notes, CAST(:label_json AS jsonb), CURRENT_TIMESTAMP
        )
        RETURNING id
        """
    with engine.begin() as conn:
        row = conn.execute(text(sql), params).mappings().first() if not is_sqlite() else None
        conn.execute(
            text("UPDATE risk_assessment_samples SET label_status = 'labeled' WHERE id = :sample_id"),
            {"sample_id": int(sample_id)},
        )
    return int(row["id"]) if row else None
