from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.common_service import json_text, repair_text


SUMMARY_VERSION = 1
ALLOWED_RESPONSE_STYLES = {"concise", "balanced", "detailed"}
MAX_FOCUS_AREAS = 8

_FOCUS_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("证据完整性", ("证据", "材料", "举证", "evidence", "proof", "record")),
    ("法律依据与引用", ("法条", "判例", "引用", "法律依据", "statute", "authority", "citation", "precedent")),
    ("事实与时间线", ("事实", "时间", "经过", "日期", "timeline", "fact", "date")),
    ("程序与管辖", ("程序", "管辖", "期限", "送达", "procedure", "jurisdiction", "deadline", "service")),
    ("损失与救济", ("损失", "赔偿", "金额", "救济", "damage", "remedy", "quantum")),
    ("主体与关系", ("当事人", "主体", "身份", "关系", "party", "identity", "relationship")),
    ("合同与履行", ("合同", "约定", "履行", "违约", "contract", "performance", "breach")),
)


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


def _clean_list(values: Any, *, limit: int = MAX_FOCUS_AREAS, item_limit: int = 80) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values if isinstance(values, list) else []:
        item = repair_text(value)[:item_limit]
        key = item.casefold()
        if not item or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def _categorize_learning_items(values: list[str]) -> list[str]:
    text_value = " ".join(repair_text(value).lower() for value in values)
    categories = [label for label, markers in _FOCUS_RULES if any(marker in text_value for marker in markers)]
    return categories or (["论证完整性"] if text_value.strip() else [])


def build_review_observation(
    *,
    run_id: int,
    request_payload: dict[str, Any],
    row: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Build a de-identified, deterministic observation from one completed review.

    Raw submissions, case facts and private strategy are intentionally excluded.
    """
    submissions = result.get("human_submissions") or []
    statement_lengths = [len(repair_text(item.get("statement"))) for item in submissions]
    evidence_bindings = sum(len(item.get("evidence_refs") or []) for item in submissions)
    fact_bindings = sum(len(item.get("fact_refs") or []) for item in submissions)
    judge = result.get("judge") or {}
    learning_items = list(judge.get("uncertainties") or []) + list(judge.get("further_review") or [])
    return {
        "schema_version": SUMMARY_VERSION,
        "source_type": "legal_review",
        "source_id": str(run_id),
        "observed_at": repair_text(result.get("completed_at")) or _utc_now(),
        "module": repair_text(request_payload.get("module") or row.get("module") or "unknown")[:40],
        "human_side": repair_text(row.get("human_side"))[:20],
        "submission_count": len(submissions),
        "statement_char_total": sum(statement_lengths),
        "evidence_binding_count": evidence_bindings,
        "fact_binding_count": fact_bindings,
        "focus_categories": _categorize_learning_items(learning_items),
    }


def aggregate_preference_summary(observations: list[dict[str, Any]]) -> dict[str, Any]:
    completed = len(observations)
    submissions = sum(max(0, int(item.get("submission_count") or 0)) for item in observations)
    char_total = sum(max(0, int(item.get("statement_char_total") or 0)) for item in observations)
    evidence_total = sum(max(0, int(item.get("evidence_binding_count") or 0)) for item in observations)
    fact_total = sum(max(0, int(item.get("fact_binding_count") or 0)) for item in observations)
    average_chars = round(char_total / submissions) if submissions else 0
    if average_chars == 0:
        communication_style = "暂无足够数据"
    elif average_chars < 180:
        communication_style = "简洁表达"
    elif average_chars <= 600:
        communication_style = "均衡表达"
    else:
        communication_style = "详细论述"

    module_counts = Counter(repair_text(item.get("module")) for item in observations if repair_text(item.get("module")))
    focus_counts: Counter[str] = Counter()
    for item in observations:
        focus_counts.update(_clean_list(item.get("focus_categories") or [], item_limit=40))

    suggestions: list[str] = []
    if completed:
        if evidence_total < submissions:
            suggestions.append("陈述时可更多绑定已核验的证据编号。")
        if fact_total < submissions:
            suggestions.append("可将主张与事实编号逐项对应，方便复核。")
        if average_chars > 800:
            suggestions.append("优先给出结论与关键依据，再补充细节。")
        elif average_chars and average_chars < 120:
            suggestions.append("可补充请求、事实依据和希望回应的问题。")
        if focus_counts:
            suggestions.append(f"近期复盘较常涉及“{focus_counts.most_common(1)[0][0]}”，建议提交前重点检查。")

    last_activity = max((repair_text(item.get("observed_at")) for item in observations), default="") or None
    return {
        "summary_version": SUMMARY_VERSION,
        "completed_reviews": completed,
        "submission_count": submissions,
        "last_activity_at": last_activity,
        "communication_style": communication_style,
        "average_statement_chars": average_chars,
        "evidence_bindings": evidence_total,
        "fact_bindings": fact_total,
        "preferred_modules": [
            {"value": value, "count": count} for value, count in module_counts.most_common(5)
        ],
        "common_focus_areas": [
            {"label": label, "count": count} for label, count in focus_counts.most_common(5)
        ],
        "suggestions": suggestions[:4],
    }


def ensure_user_preference_tables() -> None:
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS user_preference_profiles (
                tenant_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                tracking_enabled INTEGER NOT NULL DEFAULT 0,
                explicit_json TEXT NOT NULL DEFAULT '{}',
                summary_json TEXT NOT NULL DEFAULT '{}',
                observation_count INTEGER NOT NULL DEFAULT 0,
                summary_version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, user_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_preference_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL,
                observation_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (tenant_id, user_id, source_type, source_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_user_preference_observations_owner ON user_preference_observations (tenant_id, user_id, id)",
        ]
    else:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS user_preference_profiles (
                tenant_id TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                tracking_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                explicit_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                summary_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                observation_count INTEGER NOT NULL DEFAULT 0,
                summary_version INTEGER NOT NULL DEFAULT 1,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (tenant_id, user_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS user_preference_observations (
                id BIGSERIAL PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id BIGINT NOT NULL,
                source_type VARCHAR(40) NOT NULL,
                source_id VARCHAR(120) NOT NULL,
                observation_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (tenant_id, user_id, source_type, source_id)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_user_preference_observations_owner ON user_preference_observations (tenant_id, user_id, id)",
        ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def _owner(tenant_id: str, user_id: int) -> dict[str, Any]:
    return {"tenant": repair_text(tenant_id) or f"user:{user_id}", "user_id": int(user_id)}


def _load_observations(conn, owner: dict[str, Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            """
            SELECT observation_json FROM user_preference_observations
            WHERE tenant_id=:tenant AND user_id=:user_id
            ORDER BY id ASC
            """
        ),
        owner,
    ).mappings().all()
    return [_decode_json(row.get("observation_json"), {}) for row in rows]


def _save_summary(conn, owner: dict[str, Any], summary: dict[str, Any]) -> None:
    params = {**owner, "summary": json_text(summary), "count": int(summary.get("completed_reviews") or 0)}
    if is_sqlite():
        conn.execute(
            text(
                """
                INSERT INTO user_preference_profiles
                    (tenant_id,user_id,tracking_enabled,explicit_json,summary_json,observation_count,summary_version,updated_at)
                VALUES (:tenant,:user_id,1,'{}',:summary,:count,1,CURRENT_TIMESTAMP)
                ON CONFLICT(tenant_id,user_id) DO UPDATE SET
                    summary_json=:summary,observation_count=:count,summary_version=1,updated_at=CURRENT_TIMESTAMP
                """
            ),
            params,
        )
    else:
        conn.execute(
            text(
                """
                INSERT INTO user_preference_profiles
                    (tenant_id,user_id,tracking_enabled,explicit_json,summary_json,observation_count,summary_version,updated_at)
                VALUES (:tenant,:user_id,TRUE,'{}'::jsonb,CAST(:summary AS jsonb),:count,1,CURRENT_TIMESTAMP)
                ON CONFLICT(tenant_id,user_id) DO UPDATE SET
                    summary_json=CAST(:summary AS jsonb),observation_count=:count,summary_version=1,updated_at=CURRENT_TIMESTAMP
                """
            ),
            params,
        )


def record_preference_observation(
    *, tenant_id: str, user_id: int, observation: dict[str, Any]
) -> dict[str, Any]:
    ensure_user_preference_tables()
    owner = _owner(tenant_id, user_id)
    source_type = repair_text(observation.get("source_type"))[:40]
    source_id = repair_text(observation.get("source_id"))[:120]
    if not source_type or not source_id:
        raise ValueError("source_type and source_id are required")
    with engine.begin() as conn:
        profile = conn.execute(
            text("SELECT tracking_enabled FROM user_preference_profiles WHERE tenant_id=:tenant AND user_id=:user_id"),
            owner,
        ).mappings().first()
        if not profile or bool(profile.get("tracking_enabled")):
            payload = {**owner, "source_type": source_type, "source_id": source_id, "observation": json_text(observation)}
            if is_sqlite():
                conn.execute(
                    text(
                        """
                        INSERT INTO user_preference_observations
                            (tenant_id,user_id,source_type,source_id,observation_json)
                        VALUES (:tenant,:user_id,:source_type,:source_id,:observation)
                        ON CONFLICT(tenant_id,user_id,source_type,source_id) DO UPDATE SET
                            observation_json=:observation
                        """
                    ),
                    payload,
                )
            else:
                conn.execute(
                    text(
                        """
                        INSERT INTO user_preference_observations
                            (tenant_id,user_id,source_type,source_id,observation_json)
                        VALUES (:tenant,:user_id,:source_type,:source_id,CAST(:observation AS jsonb))
                        ON CONFLICT(tenant_id,user_id,source_type,source_id) DO UPDATE SET
                            observation_json=CAST(:observation AS jsonb)
                        """
                    ),
                    payload,
                )
            observations = _load_observations(conn, owner)
            _save_summary(conn, owner, aggregate_preference_summary(observations))
    return get_user_preference_summary(tenant_id=tenant_id, user_id=user_id)


def set_preference_tracking(*, tenant_id: str, user_id: int, enabled: bool) -> dict[str, Any]:
    ensure_user_preference_tables()
    owner = _owner(tenant_id, user_id)
    params = {**owner, "enabled": 1 if enabled else 0}
    with engine.begin() as conn:
        if is_sqlite():
            conn.execute(
                text(
                    """
                    INSERT INTO user_preference_profiles
                        (tenant_id,user_id,tracking_enabled,explicit_json,summary_json,observation_count,summary_version,updated_at)
                    VALUES (:tenant,:user_id,:enabled,'{}','{}',0,1,CURRENT_TIMESTAMP)
                    ON CONFLICT(tenant_id,user_id) DO UPDATE SET tracking_enabled=:enabled,updated_at=CURRENT_TIMESTAMP
                    """
                ),
                params,
            )
        else:
            conn.execute(
                text(
                    """
                    INSERT INTO user_preference_profiles
                        (tenant_id,user_id,tracking_enabled,explicit_json,summary_json,observation_count,summary_version,updated_at)
                    VALUES (:tenant,:user_id,:enabled,'{}'::jsonb,'{}'::jsonb,0,1,CURRENT_TIMESTAMP)
                    ON CONFLICT(tenant_id,user_id) DO UPDATE SET tracking_enabled=:enabled,updated_at=CURRENT_TIMESTAMP
                    """
                ),
                {**owner, "enabled": bool(enabled)},
            )
    return get_user_preference_summary(tenant_id=tenant_id, user_id=user_id)


def update_explicit_preferences(
    *,
    tenant_id: str,
    user_id: int,
    response_style: str = "balanced",
    focus_areas: list[str] | None = None,
    note: str = "",
) -> dict[str, Any]:
    ensure_user_preference_tables()
    style = repair_text(response_style).lower()
    if style not in ALLOWED_RESPONSE_STYLES:
        raise ValueError("response_style must be concise, balanced, or detailed")
    explicit = {
        "response_style": style,
        "focus_areas": _clean_list(focus_areas or []),
        "note": re.sub(r"\s+", " ", repair_text(note)).strip()[:500],
    }
    owner = _owner(tenant_id, user_id)
    params = {**owner, "explicit": json_text(explicit)}
    with engine.begin() as conn:
        if is_sqlite():
            conn.execute(
                text(
                    """
                    INSERT INTO user_preference_profiles
                        (tenant_id,user_id,tracking_enabled,explicit_json,summary_json,observation_count,summary_version,updated_at)
                    VALUES (:tenant,:user_id,0,:explicit,'{}',0,1,CURRENT_TIMESTAMP)
                    ON CONFLICT(tenant_id,user_id) DO UPDATE SET explicit_json=:explicit,updated_at=CURRENT_TIMESTAMP
                    """
                ),
                params,
            )
        else:
            conn.execute(
                text(
                    """
                    INSERT INTO user_preference_profiles
                        (tenant_id,user_id,tracking_enabled,explicit_json,summary_json,observation_count,summary_version,updated_at)
                    VALUES (:tenant,:user_id,FALSE,CAST(:explicit AS jsonb),'{}'::jsonb,0,1,CURRENT_TIMESTAMP)
                    ON CONFLICT(tenant_id,user_id) DO UPDATE SET explicit_json=CAST(:explicit AS jsonb),updated_at=CURRENT_TIMESTAMP
                    """
                ),
                params,
            )
    return get_user_preference_summary(tenant_id=tenant_id, user_id=user_id)


def get_user_preference_summary(*, tenant_id: str, user_id: int) -> dict[str, Any]:
    ensure_user_preference_tables()
    owner = _owner(tenant_id, user_id)
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT * FROM user_preference_profiles WHERE tenant_id=:tenant AND user_id=:user_id"), owner
        ).mappings().first()
        recent_rows = conn.execute(
            text(
                """
                SELECT source_type,source_id,observation_json,created_at
                FROM user_preference_observations
                WHERE tenant_id=:tenant AND user_id=:user_id
                ORDER BY id DESC LIMIT 10
                """
            ),
            owner,
        ).mappings().all()
    profile = dict(row) if row else {}
    explicit_preferences = {
        "response_style": "balanced",
        "focus_areas": [],
        "note": "",
        **_decode_json(profile.get("explicit_json"), {}),
    }
    summary = _decode_json(profile.get("summary_json"), {})
    if not summary:
        summary = aggregate_preference_summary([])
    recent_activity = []
    for item in recent_rows:
        observation = _decode_json(item.get("observation_json"), {})
        recent_activity.append(
            {
                "source_type": item.get("source_type"),
                "source_id": item.get("source_id"),
                "observed_at": observation.get("observed_at") or item.get("created_at"),
                "module": observation.get("module"),
                "submission_count": int(observation.get("submission_count") or 0),
                "focus_categories": observation.get("focus_categories") or [],
            }
        )
    return {
        "tracking_enabled": bool(profile.get("tracking_enabled")),
        "explicit_preferences": explicit_preferences,
        "summary": summary,
        "recent_activity": recent_activity,
        "updated_at": profile.get("updated_at"),
        "privacy": {
            "used_by_agents": False,
            "stores_raw_case_text": False,
            "description": "仅保存去标识化的交互统计与复盘类别，不进入代理或法官上下文。",
        },
    }


def clear_user_preference_history(*, tenant_id: str, user_id: int) -> dict[str, Any]:
    ensure_user_preference_tables()
    owner = _owner(tenant_id, user_id)
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM user_preference_observations WHERE tenant_id=:tenant AND user_id=:user_id"), owner
        )
        conn.execute(
            text("DELETE FROM user_preference_profiles WHERE tenant_id=:tenant AND user_id=:user_id"), owner
        )
    return get_user_preference_summary(tenant_id=tenant_id, user_id=user_id)
