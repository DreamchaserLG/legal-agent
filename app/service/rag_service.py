from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.service.common_service import plain_text_preview, repair_text, sha256_text, split_keywords
from app.service.module_service import normalize_module

CANADA_SOURCE_CODES = {
    "a2aj_case",
    "a2aj_law",
    "a2aj_regulation",
    "canlii",
    "ca_federal_act",
    "ca_federal_regulation",
    "laws_lois_xml",
    "on_statute",
    "on_regulation",
    "manual_canada_case",
    "url_canada_case",
    "manual_canada_rule",
    "url_canada_rule",
}
OFAC_SOURCE_CODES = {"ofac"}


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _chunk_size() -> int:
    return max(800, _safe_int(getattr(settings, "rag_chunk_size", 1800), 1800))


def _chunk_overlap() -> int:
    size = _chunk_size()
    configured = max(0, _safe_int(getattr(settings, "rag_chunk_overlap", 180), 180))
    return min(configured, size // 3)


def _max_context_items(default: int = 8) -> int:
    return max(1, min(_safe_int(getattr(settings, "rag_max_context_items", default), default), 20))


def _json_text(payload: dict | list | None) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, default=str)


def _first_text(*values) -> str:
    for value in values:
        if isinstance(value, list):
            value = ", ".join([repair_text(item) for item in value if repair_text(item)])
        clean = repair_text(value)
        if clean:
            return clean
    return ""


def _doc_structured_fields(doc: dict, source_kind: str) -> dict:
    metadata = doc.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    document_type = _first_text(
        metadata.get("document_type"),
        metadata.get("law_kind"),
        metadata.get("legal_type"),
        metadata.get("case_type"),
        metadata.get("type"),
        source_kind,
    ).lower()
    return {
        "jurisdiction": _first_text(
            metadata.get("jurisdiction"),
            metadata.get("country"),
            metadata.get("province"),
            metadata.get("database_id"),
        )[:120],
        "document_type": document_type[:80],
        "court_level": _first_text(
            metadata.get("court_level"),
            metadata.get("court_name"),
            metadata.get("database_name"),
        )[:120],
        "language": _first_text(metadata.get("language"), metadata.get("lang"), "en")[:20].lower(),
        "citation": _first_text(metadata.get("citation"), metadata.get("article_no"), metadata.get("docket_number"))[:240],
    }


def ensure_rag_tables() -> None:
    from app.core.database import is_sqlite
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_kind VARCHAR(40) NOT NULL,
                source_table VARCHAR(80) NOT NULL,
                source_id BIGINT NOT NULL,
                source_code VARCHAR(80) NOT NULL DEFAULT '',
                source_uid TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                jurisdiction TEXT NOT NULL DEFAULT '',
                document_type TEXT NOT NULL DEFAULT '',
                court_level TEXT NOT NULL DEFAULT '',
                language TEXT NOT NULL DEFAULT '',
                citation TEXT NOT NULL DEFAULT '',
                published_at TIMESTAMP NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                text_content TEXT NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (source_table, source_id, chunk_index)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_source ON rag_chunks(source_kind, source_code)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_updated ON rag_chunks(updated_at DESC)",
            """
            CREATE TABLE IF NOT EXISTS rag_index_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_filter VARCHAR(80) NOT NULL DEFAULT 'all',
                status VARCHAR(30) NOT NULL,
                documents_seen INTEGER NOT NULL DEFAULT 0,
                chunks_written INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_rag_index_runs_created ON rag_index_runs(created_at DESC)",
        ]
    else:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS rag_chunks (
                id BIGSERIAL PRIMARY KEY,
                source_kind VARCHAR(40) NOT NULL,
                source_table VARCHAR(80) NOT NULL,
                source_id BIGINT NOT NULL,
                source_code VARCHAR(80) NOT NULL DEFAULT '',
                source_uid TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL DEFAULT '',
                source_url TEXT NOT NULL DEFAULT '',
                jurisdiction VARCHAR(120) NOT NULL DEFAULT '',
                document_type VARCHAR(80) NOT NULL DEFAULT '',
                court_level VARCHAR(120) NOT NULL DEFAULT '',
                language VARCHAR(20) NOT NULL DEFAULT '',
                citation TEXT NOT NULL DEFAULT '',
                published_at TIMESTAMP NULL,
                chunk_index INTEGER NOT NULL DEFAULT 0,
                text_content TEXT NOT NULL,
                content_hash VARCHAR(64) NOT NULL,
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                search_vector TSVECTOR,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (source_table, source_id, chunk_index)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_source ON rag_chunks(source_kind, source_code)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_updated ON rag_chunks(updated_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_rag_chunks_metadata_gin ON rag_chunks USING GIN(metadata_json)",
            """
            CREATE TABLE IF NOT EXISTS rag_index_runs (
                id BIGSERIAL PRIMARY KEY,
                source_filter VARCHAR(80) NOT NULL DEFAULT 'all',
                status VARCHAR(30) NOT NULL,
                documents_seen INTEGER NOT NULL DEFAULT 0,
                chunks_written INTEGER NOT NULL DEFAULT 0,
                error_message TEXT NOT NULL DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP NULL
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_rag_index_runs_created ON rag_index_runs(created_at DESC)",
        ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
        if is_sqlite():
            existing_columns = {
                str(row[1])
                for row in conn.exec_driver_sql("PRAGMA table_info(rag_chunks)").all()
            }
            sqlite_columns = {
                "jurisdiction": "TEXT NOT NULL DEFAULT ''",
                "document_type": "TEXT NOT NULL DEFAULT ''",
                "court_level": "TEXT NOT NULL DEFAULT ''",
                "language": "TEXT NOT NULL DEFAULT ''",
                "citation": "TEXT NOT NULL DEFAULT ''",
            }
            for column, definition in sqlite_columns.items():
                if column not in existing_columns:
                    conn.exec_driver_sql(f"ALTER TABLE rag_chunks ADD COLUMN {column} {definition}")
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_rag_chunks_structured "
                    "ON rag_chunks(jurisdiction, document_type, court_level, language)"
                )
            )
        else:
            existing_columns = {
                str(row[0])
                for row in conn.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = current_schema()
                          AND table_name = 'rag_chunks'
                        """
                    )
                ).all()
            }
            pg_columns = {
                "jurisdiction": "VARCHAR(120) NOT NULL DEFAULT ''",
                "document_type": "VARCHAR(80) NOT NULL DEFAULT ''",
                "court_level": "VARCHAR(120) NOT NULL DEFAULT ''",
                "language": "VARCHAR(20) NOT NULL DEFAULT ''",
                "citation": "TEXT NOT NULL DEFAULT ''",
                "search_vector": "TSVECTOR",
            }
            for column, definition in pg_columns.items():
                if column not in existing_columns:
                    conn.execute(text(f"ALTER TABLE rag_chunks ADD COLUMN {column} {definition}"))
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_rag_chunks_structured "
                    "ON rag_chunks(jurisdiction, document_type, court_level, language)"
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_rag_chunks_vector ON rag_chunks USING GIN(search_vector)"))


def _table_exists(table_name: str) -> bool:
    with engine.connect() as conn:
        value = conn.execute(
            text("SELECT to_regclass(:table_name) IS NOT NULL"),
            {"table_name": table_name},
        ).scalar()
    return bool(value)


def _source_kind(source_code: str, source_table: str = "") -> str:
    code = repair_text(source_code).lower()
    table = repair_text(source_table).lower()
    if code in OFAC_SOURCE_CODES:
        return "ofac"
    if code == "canlii" or "case" in code or table == "legal_cases":
        return "case"
    if code in CANADA_SOURCE_CODES or table in {"legal_rules", "canada_laws"}:
        return "law"
    return "document"


def _module_source_clause(module: str) -> tuple[str, dict]:
    normalized = normalize_module(module)
    if normalized == "us_sanctions":
        return "AND source_code = ANY(:source_codes)", {"source_codes": sorted(OFAC_SOURCE_CODES)}
    if normalized == "canada":
        return (
            "AND (source_code = ANY(:source_codes) OR source_table IN ('legal_cases', 'legal_rules', 'canada_laws'))",
            {"source_codes": sorted(CANADA_SOURCE_CODES)},
        )
    return "", {}


def _source_filter_clause(source_filter: str) -> tuple[str, dict]:
    source = repair_text(source_filter or "all").lower()
    if source in {"all", ""}:
        return "", {}
    if source == "canada":
        return (
            "AND (source_code = ANY(:filter_source_codes) OR source_table IN ('legal_cases', 'legal_rules', 'canada_laws'))",
            {"filter_source_codes": sorted(CANADA_SOURCE_CODES)},
        )
    if source == "case":
        return "AND source_kind = 'case'", {}
    if source == "law":
        return "AND source_kind = 'law'", {}
    return "AND source_code = :filter_source_code", {"filter_source_code": source}


def _structured_filter_clause(filters: dict | None, *, alias: str = "") -> tuple[str, dict]:
    from app.core.database import is_sqlite

    filters = filters or {}
    clauses = []
    params: dict = {}
    prefix = f"{alias}." if alias else ""

    for key in ("jurisdiction", "document_type", "court_level", "language"):
        value = repair_text(filters.get(key))
        if not value:
            continue
        params[f"filter_{key}"] = value.lower()
        clauses.append(f"LOWER({prefix}{key}) = :filter_{key}")

    date_from = repair_text(filters.get("date_from"))
    if date_from:
        params["filter_date_from"] = date_from
        if is_sqlite():
            clauses.append(f"DATE({prefix}published_at) >= DATE(:filter_date_from)")
        else:
            clauses.append(f"{prefix}published_at >= CAST(:filter_date_from AS timestamp)")

    date_to = repair_text(filters.get("date_to"))
    if date_to:
        params["filter_date_to"] = date_to
        if is_sqlite():
            clauses.append(f"DATE({prefix}published_at) <= DATE(:filter_date_to)")
        else:
            clauses.append(f"{prefix}published_at <= CAST(:filter_date_to AS timestamp)")

    return ("AND " + " AND ".join(clauses), params) if clauses else ("", {})


def _compact_metadata(value) -> dict:
    if not isinstance(value, dict):
        return {}
    keep_keys = [
        "citation",
        "database_id",
        "database_name",
        "docket_number",
        "fetch_mode",
        "jurisdiction",
        "keywords",
        "language",
        "name",
        "source_url",
    ]
    return {key: value.get(key) for key in keep_keys if value.get(key) not in (None, "", [])}


def _document_text(*, title: str, summary: str = "", body: str = "", metadata: dict | None = None) -> str:
    parts = []
    title = repair_text(title)
    if title:
        parts.append(f"Title: {title}")
    meta = _compact_metadata(metadata or {})
    citation = repair_text(meta.get("citation"))
    if citation:
        parts.append(f"Citation: {citation}")
    keywords = meta.get("keywords")
    if isinstance(keywords, list) and keywords:
        parts.append("Keywords: " + ", ".join([repair_text(item) for item in keywords[:12] if repair_text(item)]))
    summary = plain_text_preview(summary)
    if summary:
        parts.append(f"Summary: {summary}")
    body = plain_text_preview(body)
    if body and body != summary:
        parts.append(body)
    return "\n\n".join([part for part in parts if part]).strip()


def split_into_chunks(text_value: str, *, chunk_size: int | None = None, overlap: int | None = None) -> list[str]:
    cleaned = plain_text_preview(text_value)
    if not cleaned:
        return []
    size = max(800, int(chunk_size or _chunk_size()))
    overlap = max(0, min(int(overlap if overlap is not None else _chunk_overlap()), size // 3))
    if len(cleaned) <= size:
        return [cleaned]

    chunks = []
    start = 0
    while start < len(cleaned):
        end = min(start + size, len(cleaned))
        if end < len(cleaned):
            boundary = max(cleaned.rfind(". ", start, end), cleaned.rfind("; ", start, end), cleaned.rfind(" ", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        chunk = cleaned[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(cleaned):
            break
        start = max(end - overlap, start + 1)
    return chunks


def _source_item_documents(source_filter: str, limit: int | None) -> list[dict]:
    where = ""
    params: dict = {}
    source = repair_text(source_filter or "all").lower()
    if source == "canada":
        where = "WHERE source_code = ANY(:source_codes)"
        params["source_codes"] = sorted(CANADA_SOURCE_CODES)
    elif source in {"ofac", "canlii"}:
        where = "WHERE source_code = :source_code"
        params["source_code"] = source
    elif source not in {"all", "", "case", "law"}:
        where = "WHERE source_code = :source_code"
        params["source_code"] = source
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT :limit"
        params["limit"] = int(limit)
    sql = f"""
    SELECT id, source_code, source_uid, title, item_url, published_at, summary, raw_text, raw_json
    FROM source_items
    {where}
    ORDER BY id
    {limit_clause}
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    docs = []
    for row in rows:
        meta = dict(row.get("raw_json") or {})
        docs.append(
            {
                "source_table": "source_items",
                "source_id": int(row["id"]),
                "source_code": repair_text(row.get("source_code")),
                "source_uid": repair_text(row.get("source_uid")),
                "title": repair_text(row.get("title")),
                "source_url": repair_text(row.get("item_url")),
                "published_at": row.get("published_at"),
                "metadata": meta,
                "text": _document_text(
                    title=row.get("title"),
                    summary=row.get("summary"),
                    body=row.get("raw_text"),
                    metadata=meta,
                ),
            }
        )
    return docs


def _legal_case_documents(limit: int | None) -> list[dict]:
    if not _table_exists("legal_cases"):
        return []
    limit_clause = "LIMIT :limit" if limit else ""
    params = {"limit": int(limit)} if limit else {}
    sql = f"""
    SELECT id, title, country, court_name, court_level, case_type, summary, facts,
           judgment_result, judgment_date, source_url, source_code, external_uid, raw_text
    FROM legal_cases
    ORDER BY id
    {limit_clause}
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    docs = []
    for row in rows:
        meta = {
            "country": row.get("country"),
            "court_name": row.get("court_name"),
            "court_level": row.get("court_level"),
            "case_type": row.get("case_type"),
            "judgment_result": row.get("judgment_result"),
        }
        body = "\n\n".join(
            [
                repair_text(row.get("facts")),
                repair_text(row.get("judgment_result")),
                repair_text(row.get("raw_text")),
            ]
        )
        docs.append(
            {
                "source_table": "legal_cases",
                "source_id": int(row["id"]),
                "source_code": repair_text(row.get("source_code") or "legal_case"),
                "source_uid": repair_text(row.get("external_uid") or f"legal_cases:{row['id']}"),
                "title": repair_text(row.get("title")),
                "source_url": repair_text(row.get("source_url")),
                "published_at": row.get("judgment_date"),
                "metadata": meta,
                "text": _document_text(title=row.get("title"), summary=row.get("summary"), body=body, metadata=meta),
            }
        )
    return docs


def _legal_rule_documents(limit: int | None) -> list[dict]:
    if not _table_exists("legal_rules"):
        return []
    limit_clause = "LIMIT :limit" if limit else ""
    params = {"limit": int(limit)} if limit else {}
    sql = f"""
    SELECT id, title, country, legal_type, article_no, article_text, article_summary,
           source_url, source_site, slug, rule_level, citation
    FROM legal_rules
    ORDER BY id
    {limit_clause}
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    docs = []
    for row in rows:
        meta = {
            "country": row.get("country"),
            "legal_type": row.get("legal_type"),
            "article_no": row.get("article_no"),
            "citation": row.get("citation"),
            "source_site": row.get("source_site"),
        }
        docs.append(
            {
                "source_table": "legal_rules",
                "source_id": int(row["id"]),
                "source_code": "legal_rule",
                "source_uid": repair_text(row.get("slug") or f"legal_rules:{row['id']}"),
                "title": repair_text(row.get("title")),
                "source_url": repair_text(row.get("source_url")),
                "published_at": None,
                "metadata": meta,
                "text": _document_text(
                    title=row.get("title"),
                    summary=row.get("article_summary"),
                    body=row.get("article_text"),
                    metadata=meta,
                ),
            }
        )
    return docs


def _canada_law_documents(limit: int | None) -> list[dict]:
    if not _table_exists("canada_laws"):
        return []
    limit_clause = "LIMIT :limit" if limit else ""
    params = {"limit": int(limit)} if limit else {}
    sql = f"""
    SELECT id, source_code, source_uid, title, citation, jurisdiction, law_level,
           law_kind, source_url, aliases_json
    FROM canada_laws
    ORDER BY id
    {limit_clause}
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()
    docs = []
    for row in rows:
        aliases = row.get("aliases_json") or []
        meta = {
            "citation": row.get("citation"),
            "jurisdiction": row.get("jurisdiction"),
            "law_level": row.get("law_level"),
            "law_kind": row.get("law_kind"),
            "keywords": aliases if isinstance(aliases, list) else [],
        }
        body = " ".join([repair_text(row.get("jurisdiction")), repair_text(row.get("law_level")), repair_text(row.get("law_kind"))])
        docs.append(
            {
                "source_table": "canada_laws",
                "source_id": int(row["id"]),
                "source_code": repair_text(row.get("source_code") or "canada_law"),
                "source_uid": repair_text(row.get("source_uid") or f"canada_laws:{row['id']}"),
                "title": repair_text(row.get("title")),
                "source_url": repair_text(row.get("source_url")),
                "published_at": None,
                "metadata": meta,
                "text": _document_text(title=row.get("title"), summary=row.get("citation"), body=body, metadata=meta),
            }
        )
    return docs


def iter_rag_documents(source_filter: str = "all", limit: int | None = None) -> Iterable[dict]:
    source = repair_text(source_filter or "all").lower()
    remaining = int(limit) if limit else None

    def take(docs: list[dict]) -> list[dict]:
        nonlocal remaining
        if remaining is None:
            return docs
        selected = docs[: max(0, remaining)]
        remaining -= len(selected)
        return selected

    for doc in take(_source_item_documents(source, remaining)):
        yield doc
    if remaining is not None and remaining <= 0:
        return
    if source in {"all", "canada", "case"}:
        for doc in take(_legal_case_documents(remaining)):
            yield doc
    if remaining is not None and remaining <= 0:
        return
    if source in {"all", "canada", "law"}:
        for doc in take(_legal_rule_documents(remaining)):
            yield doc
    if remaining is not None and remaining <= 0:
        return
    if source in {"all", "canada", "law"}:
        for doc in take(_canada_law_documents(remaining)):
            yield doc


def _upsert_chunk(conn, doc: dict, chunk_index: int, chunk_text: str) -> None:
    from app.core.database import is_sqlite
    source_kind = _source_kind(doc.get("source_code", ""), doc.get("source_table", ""))
    structured_fields = _doc_structured_fields(doc, source_kind)

    if is_sqlite():
        # SQLite: 不支持 search_vector 和 ON CONFLICT DO UPDATE
        conn.execute(
            text(
                """
                INSERT OR REPLACE INTO rag_chunks (
                    source_kind, source_table, source_id, source_code, source_uid,
                    title, source_url, jurisdiction, document_type, court_level,
                    language, citation, published_at, chunk_index, text_content,
                    content_hash, metadata_json, created_at, updated_at
                )
                VALUES (
                    :source_kind, :source_table, :source_id, :source_code, :source_uid,
                    :title, :source_url, :jurisdiction, :document_type, :court_level,
                    :language, :citation, :published_at, :chunk_index, :text_content,
                    :content_hash, :metadata_json,
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "source_kind": source_kind,
                "source_table": doc.get("source_table"),
                "source_id": int(doc.get("source_id")),
                "source_code": repair_text(doc.get("source_code")),
                "source_uid": repair_text(doc.get("source_uid")),
                "title": repair_text(doc.get("title")),
                "source_url": repair_text(doc.get("source_url")),
                "jurisdiction": structured_fields["jurisdiction"],
                "document_type": structured_fields["document_type"],
                "court_level": structured_fields["court_level"],
                "language": structured_fields["language"],
                "citation": structured_fields["citation"],
                "published_at": doc.get("published_at"),
                "chunk_index": int(chunk_index),
                "text_content": repair_text(chunk_text),
                "content_hash": sha256_text(chunk_text),
                "metadata_json": _json_text(doc.get("metadata") or {}),
            },
        )
    else:
        conn.execute(
            text(
                """
                INSERT INTO rag_chunks (
                    source_kind, source_table, source_id, source_code, source_uid,
                    title, source_url, jurisdiction, document_type, court_level,
                    language, citation, published_at, chunk_index, text_content,
                    content_hash, metadata_json, search_vector, created_at, updated_at
                )
                VALUES (
                    :source_kind, :source_table, :source_id, :source_code, :source_uid,
                    :title, :source_url, :jurisdiction, :document_type, :court_level,
                    :language, :citation, :published_at, :chunk_index, :text_content,
                    :content_hash, CAST(:metadata_json AS jsonb),
                    to_tsvector('english', :search_text),
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (source_table, source_id, chunk_index)
                DO UPDATE SET
                    source_kind = EXCLUDED.source_kind,
                    source_code = EXCLUDED.source_code,
                    source_uid = EXCLUDED.source_uid,
                    title = EXCLUDED.title,
                    source_url = EXCLUDED.source_url,
                    jurisdiction = EXCLUDED.jurisdiction,
                    document_type = EXCLUDED.document_type,
                    court_level = EXCLUDED.court_level,
                    language = EXCLUDED.language,
                    citation = EXCLUDED.citation,
                    published_at = EXCLUDED.published_at,
                    text_content = EXCLUDED.text_content,
                    content_hash = EXCLUDED.content_hash,
                    metadata_json = EXCLUDED.metadata_json,
                    search_vector = EXCLUDED.search_vector,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "source_kind": source_kind,
                "source_table": doc.get("source_table"),
                "source_id": int(doc.get("source_id")),
                "source_code": repair_text(doc.get("source_code")),
                "source_uid": repair_text(doc.get("source_uid")),
                "title": repair_text(doc.get("title")),
                "source_url": repair_text(doc.get("source_url")),
                "jurisdiction": structured_fields["jurisdiction"],
                "document_type": structured_fields["document_type"],
                "court_level": structured_fields["court_level"],
                "language": structured_fields["language"],
                "citation": structured_fields["citation"],
                "published_at": doc.get("published_at"),
                "chunk_index": int(chunk_index),
                "text_content": repair_text(chunk_text),
                "content_hash": sha256_text(chunk_text),
                "metadata_json": _json_text(doc.get("metadata") or {}),
                "search_text": " ".join([repair_text(doc.get("title")), repair_text(chunk_text)]),
            },
        )


def rebuild_rag_index(source_filter: str = "all", limit: int | None = None) -> dict:
    ensure_rag_tables()
    run_id = 0
    started_at = datetime.utcnow()
    with engine.begin() as conn:
        row = conn.execute(
            text(
                """
                INSERT INTO rag_index_runs (source_filter, status, created_at)
                VALUES (:source_filter, 'running', CURRENT_TIMESTAMP)
                RETURNING id
                """
            ),
            {"source_filter": repair_text(source_filter or "all")},
        ).mappings().first()
        run_id = int(row["id"]) if row else 0

    documents_seen = 0
    chunks_written = 0
    try:
        for doc in iter_rag_documents(source_filter=source_filter, limit=limit):
            chunks = split_into_chunks(doc.get("text", ""))
            if not chunks:
                continue
            documents_seen += 1
            with engine.begin() as conn:
                for index, chunk in enumerate(chunks):
                    _upsert_chunk(conn, doc, index, chunk)
                    chunks_written += 1
                conn.execute(
                    text(
                        """
                        DELETE FROM rag_chunks
                        WHERE source_table = :source_table
                          AND source_id = :source_id
                          AND chunk_index >= :chunk_count
                        """
                    ),
                    {
                        "source_table": doc.get("source_table"),
                        "source_id": int(doc.get("source_id")),
                        "chunk_count": len(chunks),
                    },
                )
        status = "completed"
        error_message = ""
    except Exception as exc:
        status = "failed"
        error_message = str(exc)

    with engine.begin() as conn:
        conn.execute(
            text(
                """
                UPDATE rag_index_runs
                SET status = :status,
                    documents_seen = :documents_seen,
                    chunks_written = :chunks_written,
                    error_message = :error_message,
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = :run_id
                """
            ),
            {
                "run_id": run_id,
                "status": status,
                "documents_seen": documents_seen,
                "chunks_written": chunks_written,
                "error_message": error_message,
            },
        )
    return {
        "run_id": run_id,
        "status": status,
        "source_filter": source_filter,
        "documents_seen": documents_seen,
        "chunks_written": chunks_written,
        "duration_seconds": round((datetime.utcnow() - started_at).total_seconds(), 3),
        "error_message": error_message,
    }


def get_rag_status() -> dict:
    ensure_rag_tables()
    with engine.connect() as conn:
        by_kind = conn.execute(
            text(
                """
                SELECT source_kind, COUNT(*) AS chunks, COUNT(DISTINCT source_table || ':' || source_id) AS documents
                FROM rag_chunks
                GROUP BY source_kind
                ORDER BY source_kind
                """
            )
        ).mappings().all()
        recent_runs = conn.execute(
            text(
                """
                SELECT id, source_filter, status, documents_seen, chunks_written, error_message, created_at, finished_at
                FROM rag_index_runs
                ORDER BY created_at DESC
                LIMIT 5
                """
            )
        ).mappings().all()
    vector_status = {}
    try:
        from app.service.vector_store_service import get_vector_status

        vector_status = get_vector_status()
    except Exception as exc:
        vector_status = {"enabled": False, "error_message": str(exc)}

    return {
        "enabled": bool(getattr(settings, "rag_enabled", True)),
        "hybrid_enabled": bool(getattr(settings, "rag_hybrid_enabled", True)),
        "chunk_size": _chunk_size(),
        "chunk_overlap": _chunk_overlap(),
        "max_context_items": _max_context_items(),
        "by_kind": [dict(row) for row in by_kind],
        "recent_runs": [dict(row) for row in recent_runs],
        "vector": vector_status,
    }


def _query_terms(query: str, keywords: list[str] | None = None) -> list[str]:
    values = split_keywords(keywords or [])
    values.extend(re.findall(r"[A-Za-z][A-Za-z0-9'./-]{2,}", repair_text(query)))
    seen = set()
    terms = []
    for value in values:
        term = repair_text(value).lower()
        if len(term) < 3 or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) >= 10:
            break
    return terms


def _tsquery_or_terms(query: str, keywords: list[str] | None = None) -> str:
    terms = []
    seen = set()
    for value in _query_terms(query, keywords):
        for token in re.findall(r"[a-z0-9]{3,}", value.lower()):
            if token in seen:
                continue
            seen.add(token)
            terms.append(f"{token}:*")
            if len(terms) >= 10:
                return " | ".join(terms)
    return " | ".join(terms)


def rag_search(
    query: str,
    *,
    keywords: list[str] | None = None,
    module: str = "canada",
    limit: int | None = None,
    source_filter: str = "all",
    filters: dict | None = None,
) -> dict:
    ensure_rag_tables()
    clean_query = repair_text(query)
    merged_query = " ".join([clean_query] + [repair_text(item) for item in (keywords or []) if repair_text(item)]).strip()
    if not merged_query:
        return {"query": clean_query, "items": [], "total": 0, "status": "empty_query"}

    params = {
        "query": merged_query,
        "limit": max(1, min(int(limit or _max_context_items()), 30)),
    }
    from app.core.database import is_sqlite
    if is_sqlite():
        # SQLite 没有 PostgreSQL tsvector，保留 LIKE 兜底以维持本地 demo 可用。
        conditions = []
        score_parts = []
        for index, term in enumerate(_query_terms(merged_query, keywords)):
            key = f"term{index}"
            params[key] = f"%{term}%"
            conditions.append(f"LOWER(title) LIKE :{key}")
            conditions.append(f"LOWER(text_content) LIKE :{key}")
            score_parts.append(f"CASE WHEN LOWER(title) LIKE :{key} THEN 0.35 ELSE 0 END")
            score_parts.append(f"CASE WHEN LOWER(text_content) LIKE :{key} THEN 0.08 ELSE 0 END")
        if not conditions:
            conditions = ["1=1"]
        where_clause = " OR ".join(conditions)
    else:
        ts_or_query = _tsquery_or_terms(merged_query, keywords)
        if ts_or_query:
            params["ts_or_query"] = ts_or_query
            conditions = [
                "search_vector @@ websearch_to_tsquery('english', :query)",
                "search_vector @@ to_tsquery('english', :ts_or_query)",
            ]
            score_parts = [
                "ts_rank_cd(search_vector, websearch_to_tsquery('english', :query))",
                "0.35 * ts_rank_cd(search_vector, to_tsquery('english', :ts_or_query))",
            ]
            where_clause = "(" + " OR ".join(conditions) + ")"
        else:
            where_clause = "search_vector @@ plainto_tsquery('english', :query)"
            score_parts = ["ts_rank_cd(search_vector, plainto_tsquery('english', :query))"]
    module_clause, module_params = _module_source_clause(module)
    source_clause, source_params = _source_filter_clause(source_filter)
    structured_clause, structured_params = _structured_filter_clause(filters)
    params.update(module_params)
    params.update(source_params)
    params.update(structured_params)
    score_expr = " + ".join(score_parts)
    sql = f"""
    SELECT
        id,
        source_kind,
        source_table,
        source_id,
        source_code,
        source_uid,
        title,
        source_url,
        jurisdiction,
        document_type,
        court_level,
        language,
        citation,
        published_at,
        chunk_index,
        text_content,
        metadata_json,
        ({score_expr}) AS score
    FROM rag_chunks
    WHERE {where_clause}
      {module_clause}
      {source_clause}
      {structured_clause}
    ORDER BY score DESC, published_at DESC NULLS LAST, updated_at DESC
    LIMIT :limit
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    items = []
    for row in rows:
        score = float(row.get("score") or 0)
        if score <= 0:
            continue
        items.append(
            {
                "chunk_id": int(row["id"]),
                "source_kind": repair_text(row.get("source_kind")),
                "source_table": repair_text(row.get("source_table")),
                "source_id": int(row.get("source_id") or 0),
                "source_code": repair_text(row.get("source_code")),
                "source_uid": repair_text(row.get("source_uid")),
                "title": repair_text(row.get("title")),
                "source_url": repair_text(row.get("source_url")),
                "jurisdiction": repair_text(row.get("jurisdiction")),
                "document_type": repair_text(row.get("document_type")),
                "court_level": repair_text(row.get("court_level")),
                "language": repair_text(row.get("language")),
                "citation": repair_text(row.get("citation")),
                "published_at": str(row.get("published_at") or "")[:10],
                "chunk_index": int(row.get("chunk_index") or 0),
                "excerpt": plain_text_preview(row.get("text_content"))[:900],
                "score": round(score, 4),
                "metadata": dict(row.get("metadata_json") or {}),
            }
        )
    return {
        "query": clean_query,
        "keywords": keywords or [],
        "module": normalize_module(module),
        "source_filter": source_filter,
        "filters": filters or {},
        "items": items,
        "total": len(items),
        "status": "ok",
    }


def build_rag_context(
    query: str,
    *,
    keywords: list[str] | None = None,
    module: str = "canada",
    limit: int | None = None,
    filters: dict | None = None,
) -> dict:
    if not bool(getattr(settings, "rag_enabled", True)):
        return {"enabled": False, "status": "disabled", "items": [], "total": 0}
    try:
        if bool(getattr(settings, "rag_hybrid_enabled", True)):
            from app.service.hybrid_retrieval_service import hybrid_search

            result = hybrid_search(
                query,
                keywords=keywords,
                module=module,
                limit=limit or _max_context_items(),
                filters=filters,
            )
        else:
            result = rag_search(
                query,
                keywords=keywords,
                module=module,
                limit=limit or _max_context_items(),
                filters=filters,
            )
    except Exception as exc:
        try:
            result = rag_search(
                query,
                keywords=keywords,
                module=module,
                limit=limit or _max_context_items(),
                filters=filters,
            )
            result["fallback_reason"] = str(exc)
            result["strategy"] = "lexical_fallback"
        except Exception as fallback_exc:
            return {"enabled": True, "status": "error", "error_message": str(fallback_exc), "items": [], "total": 0}
    items = result.get("items") or []
    citations = []
    for index, item in enumerate(items, start=1):
        citations.append(
            {
                "ref": f"R{index}",
                "title": item.get("title", ""),
                "source_kind": item.get("source_kind", ""),
                "source_code": item.get("source_code", ""),
                "source_url": item.get("source_url", ""),
                "published_at": item.get("published_at", ""),
                "score": item.get("score", 0),
            }
        )
    result.update(
        {
            "enabled": True,
            "citations": citations,
            "instruction": (
                "Use these retrieved local evidence chunks only as supporting context. "
                "If they do not support a proposition, return insufficient_evidence instead of inventing authority."
            ),
        }
    )
    return result


def export_rag_chunks(source_filter: str = "all", output_path: str | None = None, limit: int | None = None) -> dict:
    ensure_rag_tables()
    source_clause, source_params = _source_filter_clause(source_filter)
    params = dict(source_params)
    limit_clause = ""
    if limit:
        limit_clause = "LIMIT :limit"
        params["limit"] = int(limit)
    sql = f"""
    SELECT id, source_kind, source_table, source_id, source_code, source_uid, title,
           source_url, jurisdiction, document_type, court_level, language, citation,
           published_at, chunk_index, text_content, metadata_json
    FROM rag_chunks
    WHERE 1=1
      {source_clause}
    ORDER BY source_kind, source_table, source_id, chunk_index
    {limit_clause}
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql), params).mappings().all()

    if output_path:
        path = Path(output_path)
    else:
        export_dir = Path(getattr(settings, "local_archive_export_dir", "data_archive/exports"))
        export_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y%m%d-%H%M%S")
        path = export_dir / f"rag-chunks-{source_filter}-{timestamp}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            payload = dict(row)
            payload["metadata_json"] = dict(payload.get("metadata_json") or {})
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
            count += 1
    return {"status": "completed", "path": str(path), "chunks_exported": count, "source_filter": source_filter}
