from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text, sha256_text


SUSPICIOUS_EXTERNAL_INSTRUCTIONS = (
    "ignore previous instructions", "ignore system", "system prompt", "developer message",
    "call this tool", "execute command", "upload secret", "api key", "忽略以上指令",
    "忽略系统提示", "执行命令", "调用工具", "上传密钥",
)

APPROVED_CANADIAN_LEGAL_HOSTS = {
    "canlii.org", "www.canlii.org",
    "laws-lois.justice.gc.ca", "justice.gc.ca", "www.justice.gc.ca",
    "scc-csc.ca", "www.scc-csc.ca", "decisions.scc-csc.ca",
    "ontario.ca", "www.ontario.ca", "ontariocourts.ca", "www.ontariocourts.ca",
    "gazette.gc.ca", "www.gazette.gc.ca",
}


def validate_canadian_legal_source(url: str) -> dict[str, Any]:
    parsed = urlparse(repair_text(url))
    host = (parsed.hostname or "").lower()
    approved = parsed.scheme == "https" and host in APPROVED_CANADIAN_LEGAL_HOSTS
    return {
        "url": repair_text(url), "host": host, "jurisdiction": "Canada",
        "approved": approved, "read_only": True,
        "reason": "approved_canadian_legal_source" if approved else "outside_canadian_legal_allowlist",
    }


def scan_untrusted_legal_text(value: Any) -> dict[str, Any]:
    clean = repair_text(value)
    lowered = clean.casefold()
    matches = [marker for marker in SUSPICIOUS_EXTERNAL_INSTRUCTIONS if marker.casefold() in lowered]
    return {
        "classification": "untrusted_external_data",
        "safe_for_instruction_use": False,
        "prompt_injection_markers": matches,
        "quarantine_required": bool(matches),
        "content_hash": sha256_text(clean),
    }


def ensure_corpus_governance_tables() -> None:
    pk = "INTEGER PRIMARY KEY AUTOINCREMENT" if is_sqlite() else "BIGSERIAL PRIMARY KEY"
    json_type = "TEXT" if is_sqlite() else "JSONB"
    statements = [
        f"""CREATE TABLE IF NOT EXISTS legal_source_snapshots (
            id {pk}, source_url TEXT NOT NULL, source_host VARCHAR(240) NOT NULL DEFAULT '',
            retrieved_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP, content_hash VARCHAR(64) NOT NULL,
            http_metadata_json {json_type} NOT NULL, storage_ref TEXT NOT NULL DEFAULT '',
            security_status VARCHAR(40) NOT NULL DEFAULT 'pending', immutable INTEGER NOT NULL DEFAULT 1,
            UNIQUE(source_url, content_hash))""",
        f"""CREATE TABLE IF NOT EXISTS legal_corpus_changesets (
            id {pk}, base_corpus_version VARCHAR(80) NOT NULL, candidate_corpus_version VARCHAR(80) NOT NULL UNIQUE,
            status VARCHAR(40) NOT NULL DEFAULT 'draft', manifest_json {json_type} NOT NULL,
            diff_json {json_type} NOT NULL, builder_vendor VARCHAR(120) NOT NULL DEFAULT '',
            builder_model VARCHAR(160) NOT NULL DEFAULT '', created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            promoted_at TIMESTAMP NULL)""",
        f"""CREATE TABLE IF NOT EXISTS legal_corpus_reviews (
            id {pk}, changeset_id BIGINT NOT NULL, review_round INTEGER NOT NULL DEFAULT 1,
            reviewer_vendor VARCHAR(120) NOT NULL, reviewer_model VARCHAR(160) NOT NULL,
            decision VARCHAR(30) NOT NULL, findings_json {json_type} NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(changeset_id, review_round, reviewer_vendor))""",
        f"""CREATE TABLE IF NOT EXISTS legal_corpus_releases (
            id {pk}, corpus_version VARCHAR(80) NOT NULL UNIQUE, index_version VARCHAR(80) NOT NULL,
            changeset_id BIGINT NOT NULL, status VARCHAR(30) NOT NULL DEFAULT 'staged',
            rollback_to_version VARCHAR(80) NOT NULL DEFAULT '', activated_at TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def build_structured_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    old = previous or {}
    old_hash = repair_text(old.get("content_hash"))
    new_hash = repair_text(current.get("content_hash")) or sha256_text(repair_text(current.get("text")))
    old_status = repair_text(old.get("authority_status") or "unknown")
    new_status = repair_text(current.get("authority_status") or "unknown")
    change_type = "unchanged" if old_hash and old_hash == new_hash else "updated" if old else "added"
    if new_status in {"repealed", "revoked", "superseded", "expired"}:
        change_type = "deactivated"
    return {
        "jurisdiction": "Canada",
        "change_type": change_type,
        "old_hash": old_hash,
        "new_hash": new_hash,
        "authority_status_before": old_status,
        "authority_status_after": new_status,
        "effective_from": repair_text(current.get("effective_from")),
        "effective_to": repair_text(current.get("effective_to")),
        "superseded_by": repair_text(current.get("superseded_by")),
        "requires_reindex": old_hash != new_hash or old_status != new_status,
        "security": scan_untrusted_legal_text(current.get("text")),
    }


def create_changeset(
    *, base_version: str, candidate_version: str, manifest: dict[str, Any], diffs: list[dict[str, Any]],
    builder_vendor: str, builder_model: str,
) -> int:
    ensure_corpus_governance_tables()
    if repair_text(manifest.get("jurisdiction") or "Canada").casefold() != "canada":
        raise ValueError("Only Canadian legal corpus changesets are accepted")
    source_urls = manifest.get("source_urls") or []
    rejected_sources = [url for url in source_urls if not validate_canadian_legal_source(url)["approved"]]
    if rejected_sources:
        raise PermissionError(f"Changeset contains non-approved Canadian legal sources: {rejected_sources[:3]}")
    if any((row.get("security") or {}).get("quarantine_required") for row in diffs):
        status = "quarantined"
    else:
        status = "awaiting_independent_review"
    params = {
        "base": repair_text(base_version), "candidate": repair_text(candidate_version), "status": status,
        "manifest": json.dumps(manifest, ensure_ascii=False), "diff": json.dumps(diffs, ensure_ascii=False),
        "vendor": repair_text(builder_vendor), "model": repair_text(builder_model),
    }
    manifest_expr = ":manifest" if is_sqlite() else "CAST(:manifest AS jsonb)"
    diff_expr = ":diff" if is_sqlite() else "CAST(:diff AS jsonb)"
    with engine.begin() as conn:
        conn.execute(text(f"""INSERT INTO legal_corpus_changesets
            (base_corpus_version,candidate_corpus_version,status,manifest_json,diff_json,builder_vendor,builder_model)
            VALUES (:base,:candidate,:status,{manifest_expr},{diff_expr},:vendor,:model)"""), params)
        return int(conn.execute(text("SELECT id FROM legal_corpus_changesets WHERE candidate_corpus_version=:candidate"), params).scalar())


def record_independent_review(
    changeset_id: int, *, reviewer_vendor: str, reviewer_model: str,
    decision: str, findings: list[dict[str, Any]], review_round: int = 1,
) -> dict[str, Any]:
    ensure_corpus_governance_tables()
    clean_decision = repair_text(decision).lower()
    if clean_decision not in {"approved", "changes_requested", "rejected"}:
        raise ValueError("Unsupported review decision")
    with engine.begin() as conn:
        row = conn.execute(text("SELECT builder_vendor,status FROM legal_corpus_changesets WHERE id=:id"), {"id": changeset_id}).mappings().first()
        if not row:
            raise ValueError("Unknown changeset")
        if repair_text(row.get("builder_vendor")).casefold() == repair_text(reviewer_vendor).casefold():
            raise ValueError("Reviewer vendor must be independent from builder vendor")
        payload = json.dumps(findings, ensure_ascii=False)
        payload_expr = ":findings" if is_sqlite() else "CAST(:findings AS jsonb)"
        conn.execute(text(f"""INSERT INTO legal_corpus_reviews
            (changeset_id,review_round,reviewer_vendor,reviewer_model,decision,findings_json)
            VALUES (:id,:round,:vendor,:model,:decision,{payload_expr})"""), {
                "id": changeset_id, "round": max(1, int(review_round)), "vendor": repair_text(reviewer_vendor),
                "model": repair_text(reviewer_model), "decision": clean_decision, "findings": payload,
            })
        next_status = "review_approved" if clean_decision == "approved" else clean_decision
        conn.execute(text("UPDATE legal_corpus_changesets SET status=:status WHERE id=:id"), {"status": next_status, "id": changeset_id})
    return {"changeset_id": changeset_id, "decision": clean_decision, "status": next_status, "reviewed_at": datetime.now(timezone.utc).isoformat()}


def stage_approved_release(changeset_id: int, *, index_version: str) -> dict[str, Any]:
    """Stage an immutable release; activation remains an explicit deployment action."""
    ensure_corpus_governance_tables()
    with engine.begin() as conn:
        changeset = conn.execute(text("SELECT * FROM legal_corpus_changesets WHERE id=:id"), {"id": changeset_id}).mappings().first()
        if not changeset or repair_text(changeset.get("status")) != "review_approved":
            raise PermissionError("Only an independently approved changeset may be staged")
        current = conn.execute(text("SELECT corpus_version FROM legal_corpus_releases WHERE status='active' ORDER BY id DESC LIMIT 1")).scalar() or ""
        version = repair_text(changeset.get("candidate_corpus_version"))
        conn.execute(text("""INSERT INTO legal_corpus_releases
            (corpus_version,index_version,changeset_id,status,rollback_to_version)
            VALUES (:version,:index_version,:changeset_id,'staged',:rollback)"""), {
                "version": version, "index_version": repair_text(index_version),
                "changeset_id": changeset_id, "rollback": repair_text(current),
            })
    return {"status": "staged", "corpus_version": version, "index_version": repair_text(index_version), "rollback_to_version": repair_text(current)}


def activate_staged_release(corpus_version: str) -> dict[str, Any]:
    """Atomically switch the active corpus pointer without deleting the prior release."""
    ensure_corpus_governance_tables()
    version = repair_text(corpus_version)
    with engine.begin() as conn:
        release = conn.execute(text("SELECT id,status FROM legal_corpus_releases WHERE corpus_version=:version"), {"version": version}).mappings().first()
        if not release or repair_text(release.get("status")) != "staged":
            raise PermissionError("Release is not staged")
        conn.execute(text("UPDATE legal_corpus_releases SET status='inactive' WHERE status='active'"))
        conn.execute(text("UPDATE legal_corpus_releases SET status='active',activated_at=CURRENT_TIMESTAMP WHERE id=:id"), {"id": release["id"]})
        conn.execute(text("UPDATE legal_corpus_changesets SET status='promoted',promoted_at=CURRENT_TIMESTAMP WHERE candidate_corpus_version=:version"), {"version": version})
    return {"status": "active", "corpus_version": version}
