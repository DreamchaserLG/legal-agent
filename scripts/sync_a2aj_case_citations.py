from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from app.core.database import engine, is_sqlite


def _log(path: Path, event: str, **payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def ensure_tables() -> None:
    if is_sqlite():
        raise RuntimeError("A2AJ 案例互引同步需要 PostgreSQL 的 JSONB 集合展开能力。")
    statements = [
        """
        CREATE TABLE IF NOT EXISTS a2aj_case_citations (
            id BIGSERIAL PRIMARY KEY,
            citing_item_id BIGINT NOT NULL,
            cited_citation TEXT NOT NULL,
            normalized_citation TEXT NOT NULL,
            cited_item_id BIGINT NULL,
            match_method VARCHAR(40) NOT NULL DEFAULT 'normalized_neutral_citation',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (citing_item_id, normalized_citation)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_a2aj_case_citations_cited_item ON a2aj_case_citations(cited_item_id)",
        "CREATE INDEX IF NOT EXISTS idx_a2aj_case_citations_citing_item ON a2aj_case_citations(citing_item_id)",
        """
        CREATE TABLE IF NOT EXISTS case_case_citations (
            id BIGSERIAL PRIMARY KEY,
            citing_case_id BIGINT NOT NULL REFERENCES legal_cases(id) ON DELETE CASCADE,
            cited_case_id BIGINT NOT NULL REFERENCES legal_cases(id) ON DELETE CASCADE,
            relation_source VARCHAR(40) NOT NULL DEFAULT 'a2aj_cases_cited',
            match_score NUMERIC(6, 4) NOT NULL DEFAULT 1.0000,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (citing_case_id, cited_case_id, relation_source),
            CHECK (citing_case_id <> cited_case_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_case_case_citations_cited ON case_case_citations(cited_case_id)",
        "CREATE INDEX IF NOT EXISTS idx_case_case_citations_citing ON case_case_citations(citing_case_id)",
    ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def sync() -> dict:
    # A2AJ already gives both directions. Importing cases_cited alone keeps one canonical directed edge.
    source_sync_sql = """
    WITH citation_index AS (
        SELECT DISTINCT ON (normalized_citation) normalized_citation, item_id
        FROM (
            SELECT
                id AS item_id,
                LOWER(REGEXP_REPLACE(TRIM(COALESCE(raw_json ->> 'citation', '')), '\\s+', ' ', 'g')) AS normalized_citation
            FROM source_items
            WHERE source_code = 'a2aj_case' AND COALESCE(raw_json ->> 'citation', '') <> ''
            UNION ALL
            SELECT
                id AS item_id,
                LOWER(REGEXP_REPLACE(TRIM(COALESCE(raw_json ->> 'citation2', '')), '\\s+', ' ', 'g')) AS normalized_citation
            FROM source_items
            WHERE source_code = 'a2aj_case' AND COALESCE(raw_json ->> 'citation2', '') <> ''
        ) values_with_citation
        WHERE normalized_citation <> ''
        ORDER BY normalized_citation, item_id DESC
    ), extracted AS (
        SELECT
            si.id AS citing_item_id,
            cited.cited_citation,
            LOWER(REGEXP_REPLACE(TRIM(cited.cited_citation), '\\s+', ' ', 'g')) AS normalized_citation
        FROM source_items si
        CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(si.raw_json -> 'cases_cited', '[]'::jsonb)) AS cited(cited_citation)
        WHERE si.source_code = 'a2aj_case'
          AND TRIM(cited.cited_citation) <> ''
    )
    INSERT INTO a2aj_case_citations (
        citing_item_id, cited_citation, normalized_citation, cited_item_id,
        match_method, created_at, updated_at
    )
    SELECT
        extracted.citing_item_id,
        extracted.cited_citation,
        extracted.normalized_citation,
        citation_index.item_id,
        'normalized_neutral_citation', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
    FROM extracted
    LEFT JOIN citation_index ON citation_index.normalized_citation = extracted.normalized_citation
    ON CONFLICT (citing_item_id, normalized_citation)
    DO UPDATE SET
        cited_citation = EXCLUDED.cited_citation,
        cited_item_id = EXCLUDED.cited_item_id,
        match_method = EXCLUDED.match_method,
        updated_at = CURRENT_TIMESTAMP
    """
    materialize_sql = """
    INSERT INTO case_case_citations (
        citing_case_id, cited_case_id, relation_source, match_score, created_at, updated_at
    )
    SELECT DISTINCT
        citing_case.id,
        cited_case.id,
        'a2aj_cases_cited',
        1.0000,
        CURRENT_TIMESTAMP,
        CURRENT_TIMESTAMP
    FROM a2aj_case_citations acc
    JOIN legal_cases citing_case ON citing_case.source_item_id = acc.citing_item_id
    JOIN legal_cases cited_case ON cited_case.source_item_id = acc.cited_item_id
    WHERE cited_case.id <> citing_case.id
    ON CONFLICT (citing_case_id, cited_case_id, relation_source)
    DO UPDATE SET match_score = EXCLUDED.match_score, updated_at = CURRENT_TIMESTAMP
    """
    with engine.begin() as conn:
        source_result = conn.execute(text(source_sync_sql))
        case_result = conn.execute(text(materialize_sql))
        stats = conn.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*) FROM a2aj_case_citations) AS source_edges,
                    (SELECT COUNT(*) FROM a2aj_case_citations WHERE cited_item_id IS NOT NULL) AS resolved_source_edges,
                    (SELECT COUNT(*) FROM case_case_citations WHERE relation_source = 'a2aj_cases_cited') AS resolved_case_edges,
                    (SELECT COUNT(DISTINCT citing_case_id) FROM case_case_citations WHERE relation_source = 'a2aj_cases_cited') AS citing_cases,
                    (SELECT COUNT(DISTINCT cited_case_id) FROM case_case_citations WHERE relation_source = 'a2aj_cases_cited') AS cited_cases
                """
            )
        ).mappings().one()
    return {
        "source_edges_upserted": int(source_result.rowcount or 0),
        "case_edges_upserted": int(case_result.rowcount or 0),
        **{key: int(value or 0) for key, value in dict(stats).items()},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="同步 A2AJ 原始案例互引图到本地关系数据库。")
    parser.add_argument("--log-path", default="logs/a2aj_case_citation_sync.jsonl")
    args = parser.parse_args(argv)
    started = time.perf_counter()
    log_path = Path(args.log_path)
    try:
        ensure_tables()
        result = sync()
        result["duration_seconds"] = round(time.perf_counter() - started, 3)
        _log(log_path, "completed", **result)
        print(json.dumps(result, ensure_ascii=False))
        print("[RESULT]: SUCCESS")
        return 0
    except Exception as exc:
        _log(log_path, "failed", error=str(exc))
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        print("[RESULT]: FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
