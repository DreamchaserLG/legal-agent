from __future__ import annotations

import json
import math
import re
from datetime import datetime

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, is_sqlite
from app.service.common_service import plain_text_preview, repair_text
from app.service.embedding_service import create_embedding, create_embeddings, embedding_metadata
from app.service.module_service import normalize_module
from app.service.rag_service import ensure_rag_tables


CANADA_SOURCE_CODES = {
    "canlii",
    "ca_federal_act",
    "ca_federal_regulation",
    "on_statute",
    "on_regulation",
    "manual_canada_case",
    "url_canada_case",
    "manual_canada_rule",
    "url_canada_rule",
    "legal_case",
    "legal_rule",
    "canada_law",
}
OFAC_SOURCE_CODES = {"ofac"}


def _json_text(payload) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, default=str)


def _vector_text(vector: list[float]) -> str:
    return "[" + ",".join(str(float(value)) for value in vector) + "]"


def _parse_vector(value) -> list[float]:
    if isinstance(value, list):
        return [float(item) for item in value]
    if not value:
        return []
    try:
        return [float(item) for item in json.loads(value)]
    except Exception:
        return []


def _metadata(value) -> dict:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _cosine(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right))
    left_norm = math.sqrt(sum(a * a for a in left)) or 1.0
    right_norm = math.sqrt(sum(b * b for b in right)) or 1.0
    return dot / (left_norm * right_norm)


def _pgvector_available() -> bool:
    if is_sqlite():
        return False
    try:
        with engine.connect() as conn:
            return bool(conn.execute(text("SELECT to_regtype('vector') IS NOT NULL")).scalar())
    except Exception:
        return False


def _pgvector_column_available() -> bool:
    if not _pgvector_available():
        return False
    try:
        with engine.connect() as conn:
            return bool(
                conn.execute(
                    text(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM information_schema.columns
                            WHERE table_name = 'rag_chunk_embeddings'
                              AND column_name = 'embedding_vector'
                        )
                        """
                    )
                ).scalar()
            )
    except Exception:
        return False


def _pgvector_column_dimension() -> int:
    if not _pgvector_column_available():
        return 0
    try:
        with engine.connect() as conn:
            type_name = conn.execute(
                text(
                    """
                    SELECT format_type(a.atttypid, a.atttypmod)
                    FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    WHERE c.relname = 'rag_chunk_embeddings'
                      AND a.attname = 'embedding_vector'
                      AND NOT a.attisdropped
                    """
                )
            ).scalar()
        match = re.search(r"vector\((\d+)\)", str(type_name or ""))
        return int(match.group(1)) if match else 0
    except Exception:
        return 0


def _ensure_pgvector_dimension(conn, dimension: int) -> None:
    if not _pgvector_available():
        return
    current_dimension = _pgvector_column_dimension()
    if current_dimension == dimension:
        return
    conn.execute(text("DROP INDEX IF EXISTS idx_rag_chunk_embeddings_vector"))
    if current_dimension:
        conn.execute(
            text(
                f"""
                ALTER TABLE rag_chunk_embeddings
                ALTER COLUMN embedding_vector TYPE VECTOR({dimension})
                USING NULL::VECTOR({dimension})
                """
            )
        )
    else:
        conn.execute(
            text(
                f"""
                ALTER TABLE rag_chunk_embeddings
                ADD COLUMN IF NOT EXISTS embedding_vector VECTOR({dimension})
                """
            )
        )


def _try_enable_pgvector() -> bool:
    if is_sqlite():
        return False
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        return _pgvector_available()
    except Exception:
        return False


def ensure_vector_tables() -> None:
    ensure_rag_tables()
    use_pgvector = _try_enable_pgvector()
    vector_dimension = int(getattr(settings, "embedding_dimension", 384))
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS rag_chunk_embeddings (
                chunk_id INTEGER PRIMARY KEY,
                embedding_provider VARCHAR(40) NOT NULL DEFAULT '',
                embedding_model VARCHAR(120) NOT NULL DEFAULT '',
                dimension INTEGER NOT NULL DEFAULT 0,
                content_hash VARCHAR(64) NOT NULL DEFAULT '',
                embedding_json TEXT NOT NULL DEFAULT '[]',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_rag_chunk_embeddings_model ON rag_chunk_embeddings(embedding_model)",
        ]
    else:
        vector_column = f", embedding_vector VECTOR({vector_dimension})" if use_pgvector else ""
        statements = [
            f"""
            CREATE TABLE IF NOT EXISTS rag_chunk_embeddings (
                chunk_id BIGINT PRIMARY KEY REFERENCES rag_chunks(id) ON DELETE CASCADE,
                embedding_provider VARCHAR(40) NOT NULL DEFAULT '',
                embedding_model VARCHAR(120) NOT NULL DEFAULT '',
                dimension INTEGER NOT NULL DEFAULT 0,
                content_hash VARCHAR(64) NOT NULL DEFAULT '',
                embedding_json JSONB NOT NULL DEFAULT '[]'::jsonb
                {vector_column},
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_rag_chunk_embeddings_model ON rag_chunk_embeddings(embedding_model)",
        ]
        if use_pgvector:
            statements.append(
                "CREATE INDEX IF NOT EXISTS idx_rag_chunk_embeddings_vector "
                "ON rag_chunk_embeddings USING ivfflat (embedding_vector vector_cosine_ops)"
            )
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
        if use_pgvector:
            _ensure_pgvector_dimension(conn, vector_dimension)
            conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS idx_rag_chunk_embeddings_vector "
                    "ON rag_chunk_embeddings USING ivfflat (embedding_vector vector_cosine_ops)"
                )
            )


def _chunk_filter_clause(source_filter: str, module: str, params: dict, alias: str = "rc") -> str:
    clauses = []
    normalized_module = normalize_module(module)
    source = repair_text(source_filter or "all").lower()

    def in_clause(values: set[str], name: str) -> str:
        sorted_values = sorted(values)
        if is_sqlite():
            keys = []
            for index, value in enumerate(sorted_values):
                key = f"{name}_{index}"
                params[key] = value
                keys.append(f":{key}")
            return f"{alias}.source_code IN ({', '.join(keys)})"
        params[name] = sorted_values
        return f"{alias}.source_code = ANY(:{name})"

    if normalized_module == "us_sanctions":
        clauses.append(in_clause(OFAC_SOURCE_CODES, "module_source_codes"))
    elif normalized_module == "canada":
        clauses.append(
            f"({in_clause(CANADA_SOURCE_CODES, 'module_source_codes')} "
            f"OR {alias}.source_table IN ('legal_cases', 'legal_rules', 'canada_laws'))"
        )

    if source not in {"", "all"}:
        if source == "canada":
            clauses.append(
                f"({in_clause(CANADA_SOURCE_CODES, 'filter_source_codes')} "
                f"OR {alias}.source_table IN ('legal_cases', 'legal_rules', 'canada_laws'))"
            )
        elif source == "case":
            clauses.append(f"{alias}.source_kind = 'case'")
        elif source == "law":
            clauses.append(f"{alias}.source_kind = 'law'")
        else:
            params["filter_source_code"] = source
            clauses.append(f"{alias}.source_code = :filter_source_code")
    return " AND ".join(clauses) if clauses else "1=1"


def _select_chunks_needing_embeddings(source_filter: str, limit: int | None, module: str = "canada") -> list[dict]:
    meta = embedding_metadata()
    params = {
        "embedding_model": meta["model"],
        "embedding_provider": meta["provider"],
    }
    where_clause = _chunk_filter_clause(source_filter, module, params)
    limit_clause = "LIMIT :limit" if limit else ""
    if limit:
        params["limit"] = int(limit)
    sql = f"""
    SELECT rc.id, rc.title, rc.text_content, rc.content_hash
    FROM rag_chunks rc
    LEFT JOIN rag_chunk_embeddings rce ON rce.chunk_id = rc.id
    WHERE {where_clause}
      AND (
          rce.chunk_id IS NULL
          OR rce.content_hash <> rc.content_hash
          OR rce.embedding_model <> :embedding_model
          OR rce.embedding_provider <> :embedding_provider
      )
    ORDER BY rc.updated_at DESC, rc.id DESC
    {limit_clause}
    """
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def _upsert_embedding(conn, chunk: dict, vector: list[float]) -> None:
    meta = embedding_metadata()
    if is_sqlite():
        conn.execute(
            text(
                """
                INSERT OR REPLACE INTO rag_chunk_embeddings (
                    chunk_id, embedding_provider, embedding_model, dimension,
                    content_hash, embedding_json, created_at, updated_at
                )
                VALUES (
                    :chunk_id, :embedding_provider, :embedding_model, :dimension,
                    :content_hash, :embedding_json, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "chunk_id": int(chunk["id"]),
                "embedding_provider": meta["provider"],
                "embedding_model": meta["model"],
                "dimension": len(vector),
                "content_hash": repair_text(chunk.get("content_hash")),
                "embedding_json": _json_text(vector),
            },
        )
        return

    if _pgvector_column_available():
        conn.execute(
            text(
                """
                INSERT INTO rag_chunk_embeddings (
                    chunk_id, embedding_provider, embedding_model, dimension,
                    content_hash, embedding_json, embedding_vector, created_at, updated_at
                )
                VALUES (
                    :chunk_id, :embedding_provider, :embedding_model, :dimension,
                    :content_hash, CAST(:embedding_json AS jsonb), CAST(:embedding_vector AS vector),
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                )
                ON CONFLICT (chunk_id)
                DO UPDATE SET
                    embedding_provider = EXCLUDED.embedding_provider,
                    embedding_model = EXCLUDED.embedding_model,
                    dimension = EXCLUDED.dimension,
                    content_hash = EXCLUDED.content_hash,
                    embedding_json = EXCLUDED.embedding_json,
                    embedding_vector = EXCLUDED.embedding_vector,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {
                "chunk_id": int(chunk["id"]),
                "embedding_provider": meta["provider"],
                "embedding_model": meta["model"],
                "dimension": len(vector),
                "content_hash": repair_text(chunk.get("content_hash")),
                "embedding_json": _json_text(vector),
                "embedding_vector": _vector_text(vector),
            },
        )
        return

    conn.execute(
        text(
            """
            INSERT INTO rag_chunk_embeddings (
                chunk_id, embedding_provider, embedding_model, dimension,
                content_hash, embedding_json, created_at, updated_at
            )
            VALUES (
                :chunk_id, :embedding_provider, :embedding_model, :dimension,
                :content_hash, CAST(:embedding_json AS jsonb), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
            ON CONFLICT (chunk_id)
            DO UPDATE SET
                embedding_provider = EXCLUDED.embedding_provider,
                embedding_model = EXCLUDED.embedding_model,
                dimension = EXCLUDED.dimension,
                content_hash = EXCLUDED.content_hash,
                embedding_json = EXCLUDED.embedding_json,
                updated_at = CURRENT_TIMESTAMP
            """
        ),
        {
            "chunk_id": int(chunk["id"]),
            "embedding_provider": meta["provider"],
            "embedding_model": meta["model"],
            "dimension": len(vector),
            "content_hash": repair_text(chunk.get("content_hash")),
            "embedding_json": _json_text(vector),
        },
    )


def rebuild_chunk_embeddings(source_filter: str = "all", limit: int | None = None, module: str = "canada") -> dict:
    ensure_vector_tables()
    started_at = datetime.utcnow()
    batch_size = max(1, int(getattr(settings, "embedding_batch_size", 32) or 32))
    chunks = _select_chunks_needing_embeddings(source_filter, limit, module=module)
    processed = 0
    error_message = ""
    try:
        for start in range(0, len(chunks), batch_size):
            batch = chunks[start : start + batch_size]
            texts = [
                "\n\n".join([repair_text(item.get("title")), repair_text(item.get("text_content"))]).strip()
                for item in batch
            ]
            vectors = create_embeddings(texts)
            with engine.begin() as conn:
                for chunk, vector in zip(batch, vectors):
                    _upsert_embedding(conn, chunk, vector)
                    processed += 1
        status = "completed"
    except Exception as exc:
        status = "failed"
        error_message = str(exc)
    return {
        "status": status,
        "source_filter": source_filter,
        "module": normalize_module(module),
        "chunks_seen": len(chunks),
        "embeddings_written": processed,
        "embedding": embedding_metadata(),
        "pgvector_enabled": _pgvector_column_available(),
        "duration_seconds": round((datetime.utcnow() - started_at).total_seconds(), 3),
        "error_message": error_message,
    }


def get_vector_status() -> dict:
    ensure_vector_tables()
    with engine.connect() as conn:
        total = conn.execute(text("SELECT COUNT(*) FROM rag_chunk_embeddings")).scalar() or 0
        by_model = conn.execute(
            text(
                """
                SELECT embedding_provider, embedding_model, dimension, COUNT(*) AS chunks
                FROM rag_chunk_embeddings
                GROUP BY embedding_provider, embedding_model, dimension
                ORDER BY chunks DESC
                """
            )
        ).mappings().all()
    return {
        "enabled": True,
        "pgvector_enabled": _pgvector_column_available(),
        "embedding": embedding_metadata(),
        "total_embeddings": int(total),
        "by_model": [dict(row) for row in by_model],
    }


def _row_to_item(row: dict, score: float) -> dict:
    return {
        "chunk_id": int(row["id"]),
        "source_kind": repair_text(row.get("source_kind")),
        "source_table": repair_text(row.get("source_table")),
        "source_id": int(row.get("source_id") or 0),
        "source_code": repair_text(row.get("source_code")),
        "source_uid": repair_text(row.get("source_uid")),
        "title": repair_text(row.get("title")),
        "source_url": repair_text(row.get("source_url")),
        "published_at": str(row.get("published_at") or "")[:10],
        "chunk_index": int(row.get("chunk_index") or 0),
        "excerpt": plain_text_preview(row.get("text_content"))[:900],
        "score": round(float(score or 0), 4),
        "vector_score": round(float(score or 0), 4),
        "metadata": _metadata(row.get("metadata_json")),
    }


def _vector_item_key(item: dict) -> str:
    source_url = repair_text(item.get("source_url")).lower().rstrip("/")
    if source_url:
        return f"url:{source_url}"
    source_code = repair_text(item.get("source_code")).lower()
    source_uid = repair_text(item.get("source_uid")).lower()
    if source_code and source_uid:
        return f"source:{source_code}:{source_uid}"
    source_table = repair_text(item.get("source_table"))
    source_id = item.get("source_id")
    if source_table and source_id:
        return f"{source_table}:{source_id}"
    return f"chunk:{item.get('chunk_id')}"


def _dedupe_vector_items(items: list[dict], limit: int) -> list[dict]:
    deduped = []
    seen = set()
    for item in items:
        key = _vector_item_key(item)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def vector_search(
    query: str,
    *,
    module: str = "canada",
    source_filter: str = "all",
    limit: int | None = None,
) -> dict:
    ensure_vector_tables()
    clean_query = repair_text(query)
    if not clean_query:
        return {"query": clean_query, "items": [], "total": 0, "status": "empty_query"}
    query_vector = create_embedding(clean_query)
    max_items = max(1, min(int(limit or getattr(settings, "rag_vector_candidate_limit", 40)), 100))
    meta = embedding_metadata()
    diagnostic_only = repair_text(meta.get("provider")).lower() == "hash"
    raw_limit = max_items * 4
    params = {
        "limit": raw_limit,
        "embedding_model": meta["model"],
        "embedding_provider": meta["provider"],
    }
    where_clause = _chunk_filter_clause(source_filter, module, params, alias="rc")

    if _pgvector_column_available():
        params["query_vector"] = _vector_text(query_vector)
        sql = f"""
        SELECT
            rc.id, rc.source_kind, rc.source_table, rc.source_id, rc.source_code,
            rc.source_uid, rc.title, rc.source_url, rc.published_at, rc.chunk_index,
            rc.text_content, rc.metadata_json,
            1 - (rce.embedding_vector <=> CAST(:query_vector AS vector)) AS score
        FROM rag_chunks rc
        JOIN rag_chunk_embeddings rce ON rce.chunk_id = rc.id
        WHERE {where_clause}
          AND rce.embedding_model = :embedding_model
          AND rce.embedding_provider = :embedding_provider
          AND rce.embedding_vector IS NOT NULL
        ORDER BY rce.embedding_vector <=> CAST(:query_vector AS vector)
        LIMIT :limit
        """
        with engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(text(sql), params).mappings().all()]
        items = [_row_to_item(row, float(row.get("score") or 0)) for row in rows]
    else:
        sql = f"""
        SELECT
            rc.id, rc.source_kind, rc.source_table, rc.source_id, rc.source_code,
            rc.source_uid, rc.title, rc.source_url, rc.published_at, rc.chunk_index,
            rc.text_content, rc.metadata_json, rce.embedding_json
        FROM rag_chunks rc
        JOIN rag_chunk_embeddings rce ON rce.chunk_id = rc.id
        WHERE {where_clause}
          AND rce.embedding_model = :embedding_model
          AND rce.embedding_provider = :embedding_provider
        ORDER BY rc.updated_at DESC
        LIMIT 2000
        """
        with engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(text(sql), params).mappings().all()]
        scored = []
        for row in rows:
            score = _cosine(query_vector, _parse_vector(row.get("embedding_json")))
            if score > 0:
                scored.append((score, row))
        scored.sort(key=lambda item: item[0], reverse=True)
        items = [_row_to_item(row, score) for score, row in scored[: max_items * 4]]

    items = _dedupe_vector_items(items, max_items)
    for item in items:
        item["vector_diagnostic_only"] = diagnostic_only

    return {
        "query": clean_query,
        "module": normalize_module(module),
        "source_filter": source_filter,
        "items": items,
        "total": len(items),
        "status": "ok",
        "embedding": meta,
        "diagnostic_only": diagnostic_only,
        "pgvector_enabled": _pgvector_column_available(),
    }
