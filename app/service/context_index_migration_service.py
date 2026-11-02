from __future__ import annotations

"""Resumable, storage-conscious contextual index migration.

The migration stores a compact provenance prefix instead of duplicating every
chunk into ``contextual_text``.  Vectorization composes prefix + original text
at read time.  Each batch is independently committed so a local upgrade can be
stopped and resumed safely.
"""

from sqlalchemy import text

from app.core.database import engine, is_sqlite


CONTEXT_COLUMNS = {
    "context_prefix": "TEXT NOT NULL DEFAULT ''",
    "contextual_text": "TEXT NOT NULL DEFAULT ''",
    "document_hash": "VARCHAR(64) NOT NULL DEFAULT ''",
    "index_version": "VARCHAR(40) NOT NULL DEFAULT 'legacy'",
    "authority_status": "VARCHAR(40) NOT NULL DEFAULT 'unknown'",
    "effective_from": "TEXT NOT NULL DEFAULT ''",
    "effective_to": "TEXT NOT NULL DEFAULT ''",
}


def ensure_context_index_schema() -> dict:
    """Install only additive columns; never drop or rewrite source data."""
    if is_sqlite():
        with engine.begin() as conn:
            existing = {str(row[1]) for row in conn.exec_driver_sql("PRAGMA table_info(rag_chunks)").all()}
            for column, definition in CONTEXT_COLUMNS.items():
                if column not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE rag_chunks ADD COLUMN {column} {definition}")
        return {"status": "ready", "database": "sqlite"}

    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = 0"))
        conn.execute(text("SET LOCAL lock_timeout = '30s'"))
        existing = {
            str(row[0])
            for row in conn.execute(text("""
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = 'rag_chunks'
            """)).all()
        }
        added = []
        for column, definition in CONTEXT_COLUMNS.items():
            if column not in existing:
                conn.execute(text(f"ALTER TABLE rag_chunks ADD COLUMN {column} {definition}"))
                added.append(column)
    return {"status": "ready", "database": "postgresql", "columns_added": added}


def backfill_context_index_batch(batch_size: int = 10_000) -> dict:
    """Backfill one Canadian-law batch and return progress for the next run."""
    ensure_context_index_schema()
    size = max(100, min(int(batch_size), 100_000))
    if is_sqlite():
        # SQLite is a small demonstration mode. Prefix generation for new rows is
        # already handled by rag_service; avoid dialect-specific bulk SQL here.
        return {"status": "not_required", "database": "sqlite", "updated": 0}

    canada_predicate = """
        (source_code = ANY(:source_codes)
         OR source_table IN ('legal_cases', 'legal_rules', 'canada_laws'))
    """
    source_codes = [
        "a2aj_case", "a2aj_law", "a2aj_regulation", "canlii",
        "ca_federal_act", "ca_federal_regulation", "laws_lois_xml",
        "on_statute", "on_regulation", "manual_canada_case",
        "url_canada_case", "manual_canada_rule", "url_canada_rule",
    ]
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL statement_timeout = 0"))
        conn.execute(text("SET LOCAL lock_timeout = '30s'"))
        rows = conn.execute(text(f"""
            WITH target AS (
                SELECT id
                FROM rag_chunks
                WHERE {canada_predicate}
                  AND COALESCE(context_prefix, '') = ''
                ORDER BY id
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            ), prepared AS (
                SELECT rc.id,
                       concat_ws(' | ',
                           NULLIF('Jurisdiction: ' || COALESCE(NULLIF(rc.jurisdiction, ''), 'Canada'), 'Jurisdiction: '),
                           NULLIF('Type: ' || COALESCE(rc.document_type, rc.source_kind), 'Type: '),
                           NULLIF('Title: ' || rc.title, 'Title: '),
                           NULLIF('Court: ' || rc.court_level, 'Court: '),
                           NULLIF('Citation: ' || rc.citation, 'Citation: '),
                           'Chunk: ' || (rc.chunk_index + 1)::text
                       ) AS prefix
                FROM rag_chunks rc JOIN target ON target.id = rc.id
            )
            UPDATE rag_chunks rc
            SET context_prefix = prepared.prefix,
                index_version = 'context-v1',
                content_hash = md5(prepared.prefix || E'\n\n' || rc.text_content),
                search_vector =
                    setweight(to_tsvector('english', prepared.prefix), 'A') ||
                    setweight(to_tsvector('english', rc.text_content), 'B'),
                updated_at = CURRENT_TIMESTAMP
            FROM prepared
            WHERE rc.id = prepared.id
            RETURNING rc.id
        """), {"source_codes": source_codes, "batch_size": size}).all()
        remaining = conn.execute(text(f"""
            SELECT count(*) FROM rag_chunks
            WHERE {canada_predicate} AND COALESCE(context_prefix, '') = ''
        """), {"source_codes": source_codes}).scalar() or 0
    return {
        "status": "completed" if not remaining else "in_progress",
        "database": "postgresql",
        "updated": len(rows),
        "remaining": int(remaining),
        "batch_size": size,
        "storage_strategy": "prefix_only_no_full_text_duplication",
    }


def context_index_status() -> dict:
    ensure_context_index_schema()
    with engine.connect() as conn:
        if is_sqlite():
            row = conn.execute(text("""
                SELECT count(*) AS total,
                       sum(CASE WHEN context_prefix <> '' THEN 1 ELSE 0 END) AS contextualized
                FROM rag_chunks
            """)).mappings().one()
        else:
            row = conn.execute(text("""
                SELECT count(*) AS total,
                       count(*) FILTER (WHERE context_prefix <> '') AS contextualized,
                       count(*) FILTER (WHERE index_version = 'context-v1') AS context_v1
                FROM rag_chunks
                WHERE source_table IN ('legal_cases', 'legal_rules', 'canada_laws')
                   OR source_code IN (
                       'a2aj_case', 'a2aj_law', 'a2aj_regulation', 'canlii',
                       'ca_federal_act', 'ca_federal_regulation', 'laws_lois_xml',
                       'on_statute', 'on_regulation', 'manual_canada_case',
                       'url_canada_case', 'manual_canada_rule', 'url_canada_rule'
                   )
            """)).mappings().one()
    return dict(row)


if __name__ == "__main__":
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Upgrade the local Canadian contextual RAG index safely.")
    parser.add_argument("--schema-only", action="store_true")
    parser.add_argument("--batch-size", type=int, default=10_000)
    args = parser.parse_args()
    result = ensure_context_index_schema() if args.schema_only else backfill_context_index_batch(args.batch_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))
