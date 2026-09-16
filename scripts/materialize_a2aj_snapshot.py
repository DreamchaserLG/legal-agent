from __future__ import annotations

import argparse
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from app.core.database import engine, fetch_all
from app.service.canada_case_law_service import _replace_case_links
from app.service.legal_data_service import _upsert_legal_case_from_source, ensure_legal_data_tables
from app.service.vector_store_service import ensure_vector_tables


def _log(path: Path, event: str, **payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _source_snapshot() -> tuple[int, int]:
    row = fetch_all(
        "select count(*) as total, coalesce(max(id), 0) as max_id from source_items where source_code='a2aj_case'"
    )[0]
    return int(row["total"]), int(row["max_id"])


def _materialize_cases(max_source_id: int) -> int:
    sql = """
    INSERT INTO legal_cases (
        title, country, court_name, court_level, court_rank, case_type,
        summary, facts, judgment_result, judgment_date, source_url, source_site,
        raw_text, source_item_id, source_code, external_uid, normalized_title,
        created_at, updated_at
    )
    SELECT
        si.title,
        COALESCE(NULLIF(si.raw_json ->> 'country', ''), 'Canada'),
        COALESCE(NULLIF(si.raw_json ->> 'database_name', ''), NULLIF(si.raw_json ->> 'database_id', ''), 'Canadian court'),
        CASE
            WHEN UPPER(COALESCE(si.raw_json ->> 'database_id', '')) = 'SCC' THEN 'Supreme Court of Canada'
            WHEN UPPER(COALESCE(si.raw_json ->> 'database_id', '')) LIKE '%CA' THEN 'Court of appeal'
            WHEN UPPER(COALESCE(si.raw_json ->> 'database_id', '')) IN ('FC', 'BCSC', 'ONSC', 'QCCS', 'NSFC') THEN 'Superior court'
            ELSE 'Tribunal or other court'
        END,
        CASE
            WHEN UPPER(COALESCE(si.raw_json ->> 'database_id', '')) = 'SCC' THEN 5
            WHEN UPPER(COALESCE(si.raw_json ->> 'database_id', '')) LIKE '%CA' THEN 4
            WHEN UPPER(COALESCE(si.raw_json ->> 'database_id', '')) IN ('FC', 'BCSC', 'ONSC', 'QCCS', 'NSFC') THEN 3
            ELSE 2
        END,
        COALESCE(NULLIF(si.raw_json ->> 'case_type', ''), 'Case'),
        LEFT(COALESCE(si.summary, ''), 4000),
        LEFT(COALESCE(si.raw_json ->> 'facts', ''), 4000),
        LEFT(COALESCE(si.raw_json ->> 'judgment_result', ''), 2000),
        si.published_at::date,
        COALESCE(si.item_url, ''),
        'A2AJ',
        LEFT(COALESCE(si.raw_text, ''), 20000),
        si.id,
        si.source_code,
        si.source_uid,
        LOWER(REGEXP_REPLACE(TRIM(si.title), '\\s+', ' ', 'g')),
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    FROM (
        SELECT DISTINCT ON (
            COALESCE(NULLIF(raw_json ->> 'country', ''), 'Canada'),
            COALESCE(NULLIF(raw_json ->> 'database_name', ''), NULLIF(raw_json ->> 'database_id', ''), 'Canadian court'),
            LOWER(REGEXP_REPLACE(TRIM(title), '\\s+', ' ', 'g')),
            published_at::date
        ) *
        FROM (
            SELECT DISTINCT ON (LOWER(COALESCE(NULLIF(item_url, ''), source_uid))) *
            FROM source_items
            WHERE source_code = 'a2aj_case'
              AND id <= :max_source_id
              AND COALESCE(raw_text, '') <> ''
            ORDER BY LOWER(COALESCE(NULLIF(item_url, ''), source_uid)), id DESC
        ) url_deduplicated
        ORDER BY
            COALESCE(NULLIF(raw_json ->> 'country', ''), 'Canada'),
            COALESCE(NULLIF(raw_json ->> 'database_name', ''), NULLIF(raw_json ->> 'database_id', ''), 'Canadian court'),
            LOWER(REGEXP_REPLACE(TRIM(title), '\\s+', ' ', 'g')),
            published_at::date,
            id DESC
    ) si
    ON CONFLICT (source_item_id) DO UPDATE SET
        title = EXCLUDED.title,
        country = EXCLUDED.country,
        court_name = EXCLUDED.court_name,
        court_level = EXCLUDED.court_level,
        court_rank = EXCLUDED.court_rank,
        case_type = EXCLUDED.case_type,
        summary = EXCLUDED.summary,
        facts = EXCLUDED.facts,
        judgment_result = EXCLUDED.judgment_result,
        judgment_date = EXCLUDED.judgment_date,
        source_url = EXCLUDED.source_url,
        source_site = EXCLUDED.source_site,
        raw_text = EXCLUDED.raw_text,
        source_code = EXCLUDED.source_code,
        external_uid = EXCLUDED.external_uid,
        normalized_title = EXCLUDED.normalized_title,
        updated_at = CURRENT_TIMESTAMP
    """
    with engine.begin() as conn:
        result = conn.execute(text(sql), {"max_source_id": max_source_id})
    return int(result.rowcount or 0)


def _materialize_cases_resilient(log_path: Path, max_source_id: int, batch_size: int) -> int:
    """分页复用既有实体 upsert，处理同案多来源与历史唯一索引冲突。"""
    last_id = 0
    processed = 0
    while True:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, source_code, source_uid, title, item_url, published_at, summary, raw_text, raw_json
                    FROM source_items
                    WHERE source_code = 'a2aj_case'
                      AND id > :last_id AND id <= :max_source_id
                      AND COALESCE(raw_text, '') <> ''
                    ORDER BY id
                    LIMIT :batch_size
                    """
                ),
                {"last_id": last_id, "max_source_id": max_source_id, "batch_size": batch_size},
            ).mappings().all()
        if not rows:
            break
        for row in rows:
            _upsert_legal_case_from_source(dict(row))
            processed += 1
        last_id = int(rows[-1]["id"])
        if processed % max(batch_size * 10, 1000) == 0:
            _log(log_path, "structured_progress", processed=processed, last_source_item_id=last_id)
    return processed


def _law_rows() -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, title, normalized_title, aliases_json, citation, origin
                FROM canada_laws
                WHERE origin = 'official'
                ORDER BY id
                """
            )
        ).mappings().all()
    return [dict(row) for row in rows]


def _link_cases(log_path: Path, batch_size: int, max_source_id: int) -> int:
    laws = _law_rows()
    last_id = 0
    processed = 0
    while True:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    """
                    SELECT id, title, summary, raw_text, raw_json, published_at, source_code
                    FROM source_items
                    WHERE source_code = 'a2aj_case' AND id > :last_id AND id <= :max_source_id
                    ORDER BY id
                    LIMIT :batch_size
                    """
                ),
                {"last_id": last_id, "max_source_id": max_source_id, "batch_size": batch_size},
            ).mappings().all()
        if not rows:
            break
        batch = [dict(row) for row in rows]
        _replace_case_links(batch, laws)
        last_id = int(batch[-1]["id"])
        processed += len(batch)
        if processed % max(batch_size * 10, 1000) == 0:
            _log(log_path, "link_progress", processed=processed, last_source_item_id=last_id)
    return processed


def _sync_relations() -> int:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                DELETE FROM case_rule_relations crr
                USING legal_cases lc, legal_rules lr
                WHERE crr.case_id = lc.id AND crr.rule_id = lr.id
                  AND lc.source_code = 'a2aj_case' AND lr.country = 'Canada'
                  AND COALESCE(crr.relation_type, '') <> 'demo_seed'
                """
            )
        )
        result = conn.execute(
            text(
                """
                INSERT INTO case_rule_relations (case_id, rule_id, relation_type, match_score, match_reason, created_at)
                SELECT
                    lc.id,
                    lr.id,
                    COALESCE(NULLIF(cl.match_source, ''), 'direct_mention'),
                    COALESCE(cl.match_score, 0),
                    LEFT(CONCAT_WS('；', NULLIF(cl.match_source, ''), NULLIF(cl.matched_alias, ''), NULLIF(cl.evidence_excerpt, '')), 3000),
                    CURRENT_TIMESTAMP
                FROM canada_case_law_links cl
                JOIN legal_cases lc ON lc.source_item_id = cl.case_item_id
                JOIN legal_rules lr ON lr.canada_law_id = cl.law_id
                WHERE lc.source_code = 'a2aj_case'
                ON CONFLICT (case_id, rule_id) DO UPDATE SET
                    relation_type = EXCLUDED.relation_type,
                    match_score = EXCLUDED.match_score,
                    match_reason = EXCLUDED.match_reason
                """
            )
        )
    return int(result.rowcount or 0)


def _vector_capacity_status() -> dict:
    drive = shutil.disk_usage(ROOT_DIR.drive + "\\")
    stats = fetch_all(
        """
        SELECT
            (SELECT COUNT(*) FROM source_items WHERE source_code='a2aj_case') AS cases,
            (SELECT COUNT(*) FROM rag_chunks WHERE source_kind='case') AS case_chunks,
            (SELECT COUNT(DISTINCT source_table || ':' || source_id) FROM rag_chunks WHERE source_kind='case') AS chunk_documents,
            pg_total_relation_size('rag_chunk_embeddings') AS embedding_bytes
        """
    )[0]
    documents = max(int(stats["chunk_documents"] or 0), 1)
    chunks_per_case = max(float(stats["case_chunks"] or 0) / documents, 1.0)
    projected_chunks = int(float(stats["cases"] or 0) * chunks_per_case)
    # Conservative estimate: 4 KB vector + HNSW/row/index overhead per chunk.
    projected_bytes = projected_chunks * 8192
    return {
        "free_bytes": drive.free,
        "free_gb": round(drive.free / 1024**3, 2),
        "estimated_chunks": projected_chunks,
        "estimated_vector_bytes": projected_bytes,
        "estimated_vector_gb": round(projected_bytes / 1024**3, 2),
        "allowed": drive.free >= projected_bytes * 1.35,
    }


def run(args: argparse.Namespace) -> dict:
    log_path = Path(args.log_path)
    started = time.perf_counter()
    source_count, max_source_id = _source_snapshot()
    if source_count < args.minimum_cases:
        raise RuntimeError(f"A2AJ 案例数量 {source_count} 低于阈值 {args.minimum_cases}")
    _log(log_path, "started", source_count=source_count, max_source_item_id=max_source_id)
    ensure_legal_data_tables()
    structured_rows = _materialize_cases(max_source_id)
    _log(log_path, "structured_completed", affected_rows=structured_rows)
    linked_cases = _link_cases(log_path, args.link_batch_size, max_source_id)
    relation_rows = _sync_relations()
    _log(log_path, "relations_completed", linked_cases=linked_cases, relation_rows=relation_rows)
    ensure_vector_tables()
    capacity = _vector_capacity_status()
    _log(log_path, "vector_capacity_checked", **capacity)
    return {
        "status": "partial" if not capacity["allowed"] else "ready_for_vectors",
        "source_count": source_count,
        "structured_rows": structured_rows,
        "linked_cases": linked_cases,
        "relation_rows": relation_rows,
        "vector_capacity": capacity,
        "duration_seconds": round(time.perf_counter() - started, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="20 万 A2AJ 案例的结构化入库、关联和向量容量门禁")
    parser.add_argument("--minimum-cases", type=int, default=200000)
    parser.add_argument("--link-batch-size", type=int, default=250)
    parser.add_argument("--log-path", default="logs/a2aj_materialization_20260910.jsonl")
    args = parser.parse_args()
    try:
        result = run(args)
        print(json.dumps(result, ensure_ascii=False, default=str))
        print("[RESULT]: SUCCESS" if result["status"] == "ready_for_vectors" else "[RESULT]: CAPACITY_BLOCKED")
        return 0 if result["status"] == "ready_for_vectors" else 2
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        print("[RESULT]: FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
