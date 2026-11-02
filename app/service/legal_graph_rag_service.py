from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy import inspect, text

from app.core.config import settings
from app.core.database import engine, is_sqlite
from app.service.common_service import plain_text_preview, repair_text, sha256_text
from app.service.legal_query_planner_service import build_legal_query_plan
from app.service.legal_entity_extraction_service import extract_legal_entities
from app.service.rag_service import ensure_rag_tables


GRAPH_SCHEMA_VERSION = "legal-graph-rag-1.0"
_LEGAL_GRAPH_TABLES_READY = False
_ENTITY_TYPES = {
    "Case", "Court", "Statute", "Regulation", "JudicialInterpretation", "Section", "LegalVersion",
    "LegalIssue", "LegalConcept", "Holding", "Claim", "Charge", "Element", "Defense", "Exception",
    "Fact", "Evidence", "SentencingFactor", "Remedy", "Person", "Organization", "Procedure",
}
_RELATION_TYPES = {
    "CITES", "MENTIONS", "APPLIES", "INTERPRETS", "DISTINGUISHES",
    "FOLLOWS", "OVERRULES", "AMENDS", "REPEALS", "SUPERSEDES", "HAS_VERSION",
    "HAS_SECTION", "HAS_ELEMENT", "HAS_SENTENCING_FACTOR", "AGGRAVATES", "MITIGATES",
    "SATISFIES", "DISPUTES", "SUPPORTS", "CONTRADICTS", "RELATED_ISSUE", "DECIDED_BY",
}
_LEGAL_CONCEPT_HINTS = (
    (r"租客|承租|房东|出租|\b(?:tenant|landlord|lease|tenancy)\b", "residential tenancy"),
    (r"驱逐|腾退|终止|\b(?:eviction|possession|termination)\b", "eviction and termination"),
    (r"维修|漏水|霉菌|供暖|适居|\b(?:repair|maintenance|habitability)\b", "repair and habitability"),
    (r"劳动|雇主|员工|解雇|\b(?:employment|employee|dismissal)\b", "employment termination"),
    (r"合同|协议|违约|\b(?:contract|agreement|breach)\b", "contract and breach"),
    (r"过失|事故|受伤|因果|\b(?:negligence|injury|causation)\b", "negligence and causation"),
    (r"赔偿|损失|救济|\b(?:damages|remedy|compensation)\b", "damages and remedies"),
)
_LAW_NAME_RE = re.compile(
    r"\b([A-Z][A-Za-z0-9'().,&/\-]*(?:\s+(?:[A-Z][A-Za-z0-9'().,&/\-]*|of|and|the|for|to|in|on)){0,8}\s+(?:Act|Code|Rules|Regulation(?:s)?|Charter))\b"
)
_CITATION_RE = re.compile(r"\b(?:R\.?S\.?[CO]\.?|S\.?C\.?|O\.?\s*Reg\.?)\s*[^,;\n]{2,80}", re.I)
_GENERIC_TERMS = {
    "canada", "canadian", "ontario", "legal", "law", "case", "cases", "issue",
    "matter", "claim", "claims", "dispute", "document", "evidence", "act", "statute",
    "rule", "rules", "regulation", "regulations",
}


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _json_value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (TypeError, ValueError):
            return {}
    return {}


def _normalize_name(value: Any) -> str:
    clean = repair_text(value).lower()
    return re.sub(r"[^a-z0-9\u3400-\u9fff]+", " ", clean).strip()


def _unique(values: list[str], limit: int = 24) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = repair_text(value)
        key = _normalize_name(clean)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def ensure_legal_graph_tables() -> None:
    global _LEGAL_GRAPH_TABLES_READY
    if _LEGAL_GRAPH_TABLES_READY:
        return
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS legal_graph_entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type VARCHAR(40) NOT NULL,
                canonical_key VARCHAR(320) NOT NULL UNIQUE,
                canonical_name TEXT NOT NULL,
                jurisdiction VARCHAR(120) NOT NULL DEFAULT '',
                aliases_json TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                confidence REAL NOT NULL DEFAULT 1.0,
                source_kind VARCHAR(40) NOT NULL DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_entities_type ON legal_graph_entities(entity_type)",
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_entities_name ON legal_graph_entities(canonical_name)",
            """
            CREATE TABLE IF NOT EXISTS legal_graph_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                edge_key VARCHAR(64) NOT NULL UNIQUE,
                source_entity_id BIGINT NOT NULL,
                target_entity_id BIGINT NOT NULL,
                relation_type VARCHAR(40) NOT NULL,
                source_chunk_id BIGINT NULL,
                source_ref TEXT NOT NULL DEFAULT '',
                evidence_excerpt TEXT NOT NULL DEFAULT '',
                extraction_method VARCHAR(80) NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.0,
                valid_from TEXT NOT NULL DEFAULT '',
                valid_to TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_edges_source ON legal_graph_edges(source_entity_id, relation_type)",
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_edges_target ON legal_graph_edges(target_entity_id, relation_type)",
            """
            CREATE TABLE IF NOT EXISTS legal_graph_chunk_entities (
                chunk_id BIGINT NOT NULL,
                entity_id BIGINT NOT NULL,
                relation_role VARCHAR(40) NOT NULL DEFAULT 'evidence',
                confidence REAL NOT NULL DEFAULT 1.0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chunk_id, entity_id, relation_role)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_chunk_entities_entity ON legal_graph_chunk_entities(entity_id, chunk_id)",
        ]
    else:
        statements = [
            """
            CREATE TABLE IF NOT EXISTS legal_graph_entities (
                id BIGSERIAL PRIMARY KEY,
                entity_type VARCHAR(40) NOT NULL,
                canonical_key VARCHAR(320) NOT NULL UNIQUE,
                canonical_name TEXT NOT NULL,
                jurisdiction VARCHAR(120) NOT NULL DEFAULT '',
                aliases_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                confidence NUMERIC(6, 4) NOT NULL DEFAULT 1.0,
                source_kind VARCHAR(40) NOT NULL DEFAULT '',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_entities_type ON legal_graph_entities(entity_type)",
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_entities_name ON legal_graph_entities(LOWER(canonical_name))",
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_entities_aliases ON legal_graph_entities USING GIN(aliases_json)",
            """
            CREATE TABLE IF NOT EXISTS legal_graph_edges (
                id BIGSERIAL PRIMARY KEY,
                edge_key VARCHAR(64) NOT NULL UNIQUE,
                source_entity_id BIGINT NOT NULL,
                target_entity_id BIGINT NOT NULL,
                relation_type VARCHAR(40) NOT NULL,
                source_chunk_id BIGINT NULL,
                source_ref TEXT NOT NULL DEFAULT '',
                evidence_excerpt TEXT NOT NULL DEFAULT '',
                extraction_method VARCHAR(80) NOT NULL DEFAULT '',
                confidence NUMERIC(6, 4) NOT NULL DEFAULT 0.0,
                valid_from TEXT NOT NULL DEFAULT '',
                valid_to TEXT NOT NULL DEFAULT '',
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_edges_source ON legal_graph_edges(source_entity_id, relation_type)",
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_edges_target ON legal_graph_edges(target_entity_id, relation_type)",
            """
            CREATE TABLE IF NOT EXISTS legal_graph_chunk_entities (
                chunk_id BIGINT NOT NULL,
                entity_id BIGINT NOT NULL,
                relation_role VARCHAR(40) NOT NULL DEFAULT 'evidence',
                confidence NUMERIC(6, 4) NOT NULL DEFAULT 1.0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (chunk_id, entity_id, relation_role)
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_legal_graph_chunk_entities_entity ON legal_graph_chunk_entities(entity_id, chunk_id)",
        ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    _LEGAL_GRAPH_TABLES_READY = True


def _upsert_entity(
    conn,
    *,
    entity_type: str,
    canonical_key: str,
    canonical_name: str,
    jurisdiction: str = "",
    aliases: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    confidence: float = 1.0,
    source_kind: str = "",
) -> int:
    if entity_type not in _ENTITY_TYPES:
        raise ValueError(f"Unsupported graph entity type: {entity_type}")
    existing = conn.execute(
        text("SELECT id FROM legal_graph_entities WHERE canonical_key=:canonical_key"),
        {"canonical_key": canonical_key},
    ).scalar()
    params = {
        "entity_type": entity_type,
        "canonical_key": canonical_key,
        "canonical_name": repair_text(canonical_name)[:500],
        "jurisdiction": repair_text(jurisdiction)[:120],
        "aliases_json": _json_text(_unique(list(aliases or []), limit=32)),
        "metadata_json": _json_text(metadata or {}),
        "confidence": max(0.0, min(float(confidence), 1.0)),
        "source_kind": repair_text(source_kind)[:40],
    }
    if existing:
        if is_sqlite():
            sql = """
            UPDATE legal_graph_entities
            SET entity_type=:entity_type, canonical_name=:canonical_name, jurisdiction=:jurisdiction,
                aliases_json=:aliases_json, metadata_json=:metadata_json,
                confidence=:confidence, source_kind=:source_kind, updated_at=CURRENT_TIMESTAMP
            WHERE id=:entity_id
            """
        else:
            sql = """
            UPDATE legal_graph_entities
            SET entity_type=:entity_type, canonical_name=:canonical_name, jurisdiction=:jurisdiction,
                aliases_json=CAST(:aliases_json AS jsonb), metadata_json=CAST(:metadata_json AS jsonb),
                confidence=:confidence, source_kind=:source_kind, updated_at=CURRENT_TIMESTAMP
            WHERE id=:entity_id
            """
        conn.execute(text(sql), {**params, "entity_id": int(existing)})
        return int(existing)
    if is_sqlite():
        sql = """
        INSERT INTO legal_graph_entities
            (entity_type, canonical_key, canonical_name, jurisdiction, aliases_json,
             metadata_json, confidence, source_kind)
        VALUES
            (:entity_type, :canonical_key, :canonical_name, :jurisdiction, :aliases_json,
             :metadata_json, :confidence, :source_kind)
        """
    else:
        sql = """
        INSERT INTO legal_graph_entities
            (entity_type, canonical_key, canonical_name, jurisdiction, aliases_json,
             metadata_json, confidence, source_kind)
        VALUES
            (:entity_type, :canonical_key, :canonical_name, :jurisdiction,
             CAST(:aliases_json AS jsonb), CAST(:metadata_json AS jsonb), :confidence, :source_kind)
        """
    conn.execute(text(sql), params)
    return int(
        conn.execute(
            text("SELECT id FROM legal_graph_entities WHERE canonical_key=:canonical_key"),
            {"canonical_key": canonical_key},
        ).scalar()
    )


def _link_chunk(conn, chunk_id: int, entity_id: int, role: str, confidence: float) -> None:
    if is_sqlite():
        sql = """
        INSERT INTO legal_graph_chunk_entities (chunk_id, entity_id, relation_role, confidence)
        VALUES (:chunk_id, :entity_id, :role, :confidence)
        ON CONFLICT(chunk_id, entity_id, relation_role) DO UPDATE SET confidence=excluded.confidence
        """
    else:
        sql = """
        INSERT INTO legal_graph_chunk_entities (chunk_id, entity_id, relation_role, confidence)
        VALUES (:chunk_id, :entity_id, :role, :confidence)
        ON CONFLICT(chunk_id, entity_id, relation_role) DO UPDATE SET confidence=EXCLUDED.confidence
        """
    conn.execute(
        text(sql),
        {"chunk_id": int(chunk_id), "entity_id": int(entity_id), "role": role[:40], "confidence": confidence},
    )


def _upsert_edge(
    conn,
    *,
    source_entity_id: int,
    target_entity_id: int,
    relation_type: str,
    source_chunk_id: int | None = None,
    source_ref: str = "",
    evidence_excerpt: str = "",
    extraction_method: str = "",
    confidence: float = 0.0,
    valid_from: str = "",
    valid_to: str = "",
    metadata: dict[str, Any] | None = None,
) -> int:
    if relation_type not in _RELATION_TYPES:
        raise ValueError(f"Unsupported graph relation type: {relation_type}")
    edge_key = sha256_text(
        f"{source_entity_id}|{target_entity_id}|{relation_type}|{repair_text(source_ref)}"
    )
    params = {
        "edge_key": edge_key,
        "source_entity_id": int(source_entity_id),
        "target_entity_id": int(target_entity_id),
        "relation_type": relation_type,
        "source_chunk_id": int(source_chunk_id) if source_chunk_id else None,
        "source_ref": repair_text(source_ref)[:600],
        "evidence_excerpt": plain_text_preview(evidence_excerpt)[:1600],
        "extraction_method": repair_text(extraction_method)[:80],
        "confidence": max(0.0, min(float(confidence), 1.0)),
        "valid_from": repair_text(valid_from)[:40],
        "valid_to": repair_text(valid_to)[:40],
        "metadata_json": _json_text(metadata or {}),
    }
    if is_sqlite():
        sql = """
        INSERT INTO legal_graph_edges
            (edge_key, source_entity_id, target_entity_id, relation_type, source_chunk_id,
             source_ref, evidence_excerpt, extraction_method, confidence, valid_from, valid_to, metadata_json)
        VALUES
            (:edge_key, :source_entity_id, :target_entity_id, :relation_type, :source_chunk_id,
             :source_ref, :evidence_excerpt, :extraction_method, :confidence, :valid_from, :valid_to, :metadata_json)
        ON CONFLICT(edge_key) DO UPDATE SET
            source_chunk_id=excluded.source_chunk_id, evidence_excerpt=excluded.evidence_excerpt,
            extraction_method=excluded.extraction_method, confidence=excluded.confidence,
            valid_from=excluded.valid_from, valid_to=excluded.valid_to,
            metadata_json=excluded.metadata_json, updated_at=CURRENT_TIMESTAMP
        """
    else:
        sql = """
        INSERT INTO legal_graph_edges
            (edge_key, source_entity_id, target_entity_id, relation_type, source_chunk_id,
             source_ref, evidence_excerpt, extraction_method, confidence, valid_from, valid_to, metadata_json)
        VALUES
            (:edge_key, :source_entity_id, :target_entity_id, :relation_type, :source_chunk_id,
             :source_ref, :evidence_excerpt, :extraction_method, :confidence, :valid_from, :valid_to,
             CAST(:metadata_json AS jsonb))
        ON CONFLICT(edge_key) DO UPDATE SET
            source_chunk_id=EXCLUDED.source_chunk_id, evidence_excerpt=EXCLUDED.evidence_excerpt,
            extraction_method=EXCLUDED.extraction_method, confidence=EXCLUDED.confidence,
            valid_from=EXCLUDED.valid_from, valid_to=EXCLUDED.valid_to,
            metadata_json=EXCLUDED.metadata_json, updated_at=CURRENT_TIMESTAMP
        """
    conn.execute(text(sql), params)
    return int(
        conn.execute(text("SELECT id FROM legal_graph_edges WHERE edge_key=:edge_key"), {"edge_key": edge_key}).scalar()
    )


def _document_entity_type(source_kind: str) -> str | None:
    if repair_text(source_kind).lower() == "case":
        return "Case"
    if repair_text(source_kind).lower() == "law":
        return "Statute"
    return None


def _document_key(entity_type: str, source_table: str, source_id: int) -> str:
    return f"{entity_type.lower()}:{repair_text(source_table).lower()}:{int(source_id)}"


def _keyword_values(metadata: dict[str, Any]) -> list[str]:
    values: list[str] = []
    for key in ("keywords", "legal_issues", "topics"):
        raw = metadata.get(key)
        if isinstance(raw, list):
            values.extend(repair_text(item) for item in raw)
        elif isinstance(raw, str):
            values.extend(re.split(r"[,;|]", raw))
    return [value for value in _unique(values, limit=8) if 3 <= len(value) <= 120]


def _seed_document_rows(source_filter: str, limit: int | None) -> dict[str, int]:
    source = repair_text(source_filter or "all").lower()
    clauses = ["source_kind IN ('case', 'law')"]
    params: dict[str, Any] = {}
    if source in {"case", "law"}:
        clauses.append("source_kind=:source_kind")
        params["source_kind"] = source
    elif source not in {"", "all", "canada"}:
        clauses.append("source_code=:source_code")
        params["source_code"] = source
    limit_clause = "LIMIT :limit" if limit else ""
    if limit:
        params["limit"] = max(1, int(limit))
    selected_columns = """
        id, source_kind, source_table, source_id, source_code, source_uid, title,
        source_url, jurisdiction, document_type, court_level, language, citation,
        published_at, chunk_index, text_content, metadata_json
    """
    if is_sqlite():
        sql = f"""
        WITH ranked_documents AS (
            SELECT {selected_columns},
                   ROW_NUMBER() OVER (
                       PARTITION BY source_table, source_id
                       ORDER BY chunk_index ASC, id ASC
                   ) AS document_rank
            FROM rag_chunks
            WHERE {' AND '.join(clauses)}
        )
        SELECT {selected_columns}
        FROM ranked_documents
        WHERE document_rank=1
        ORDER BY id ASC
        {limit_clause}
        """
    else:
        sql = f"""
        SELECT DISTINCT ON (source_table, source_id) {selected_columns}
        FROM rag_chunks
        WHERE {' AND '.join(clauses)}
        ORDER BY source_table, source_id, chunk_index ASC, id ASC
        {limit_clause}
        """
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(text(sql), params).mappings().all()]

    entity_cache: dict[str, int] = {}
    entities_written: set[int] = set()
    edge_ids: set[int] = set()
    with engine.begin() as conn:
        for row in rows:
            entity_type = _document_entity_type(row.get("source_kind"))
            if not entity_type:
                continue
            source_table = repair_text(row.get("source_table"))
            source_id = int(row.get("source_id") or 0)
            canonical_key = _document_key(entity_type, source_table, source_id)
            metadata = _json_value(row.get("metadata_json"))
            if not isinstance(metadata, dict):
                metadata = {}
            entity_id = entity_cache.get(canonical_key)
            if entity_id is None:
                aliases = [row.get("title"), row.get("citation"), row.get("source_uid")]
                entity_id = _upsert_entity(
                    conn,
                    entity_type=entity_type,
                    canonical_key=canonical_key,
                    canonical_name=repair_text(row.get("title") or row.get("citation") or row.get("source_uid")),
                    jurisdiction=repair_text(row.get("jurisdiction")),
                    aliases=[repair_text(item) for item in aliases if repair_text(item)],
                    metadata={
                        "source_table": source_table,
                        "source_id": source_id,
                        "source_code": repair_text(row.get("source_code")),
                        "source_url": repair_text(row.get("source_url")),
                        "citation": repair_text(row.get("citation")),
                    },
                    confidence=1.0,
                    source_kind=repair_text(row.get("source_kind")),
                )
                entity_cache[canonical_key] = entity_id
            entities_written.add(entity_id)
            _link_chunk(conn, int(row["id"]), entity_id, "document", 1.0)

            for keyword in _keyword_values(metadata):
                normalized = _normalize_name(keyword)
                if not normalized or normalized in _GENERIC_TERMS:
                    continue
                concept_id = _upsert_entity(
                    conn,
                    entity_type="LegalConcept",
                    canonical_key=f"concept:{normalized}",
                    canonical_name=keyword,
                    jurisdiction=repair_text(row.get("jurisdiction")),
                    aliases=[keyword],
                    metadata={"origin": "rag_metadata_keyword"},
                    confidence=0.72,
                    source_kind="concept",
                )
                entities_written.add(concept_id)
                _link_chunk(conn, int(row["id"]), concept_id, "concept_evidence", 0.72)
                edge_ids.add(
                    _upsert_edge(
                        conn,
                        source_entity_id=entity_id,
                        target_entity_id=concept_id,
                        relation_type="RELATED_ISSUE",
                        source_chunk_id=int(row["id"]),
                        source_ref=canonical_key,
                        evidence_excerpt=repair_text(row.get("text_content"))[:600],
                        extraction_method="metadata_keyword",
                        confidence=0.72,
                    )
                )
    return {"documents_processed": len(rows), "entities_written": len(entities_written), "edges_written": len(edge_ids)}


def _seed_canada_case_law_links(limit: int | None) -> dict[str, int]:
    inspector = inspect(engine)
    if not inspector.has_table("canada_case_law_links") or not inspector.has_table("canada_laws"):
        return {"links_seen": 0, "edges_written": 0}
    params: dict[str, Any] = {}
    limit_clause = "LIMIT :limit" if limit else ""
    if limit:
        params["limit"] = max(1, int(limit))
    sql = f"""
    SELECT cl.case_item_id, cl.law_id, cl.matched_alias, cl.match_source,
           cl.match_score, cl.evidence_excerpt,
           l.source_item_id AS law_source_item_id, l.title AS law_title,
           l.citation AS law_citation, l.jurisdiction AS law_jurisdiction,
           l.source_url AS law_source_url,
           si.title AS case_title, si.source_uid AS case_source_uid,
           (SELECT id FROM rag_chunks rc
            WHERE rc.source_table='source_items' AND rc.source_id=cl.case_item_id
            ORDER BY rc.chunk_index ASC LIMIT 1) AS case_chunk_id,
           (SELECT id FROM rag_chunks rc
            WHERE (rc.source_table='canada_laws' AND rc.source_id=cl.law_id)
               OR (l.source_item_id IS NOT NULL AND rc.source_table='source_items' AND rc.source_id=l.source_item_id)
            ORDER BY rc.chunk_index ASC LIMIT 1) AS law_chunk_id
    FROM canada_case_law_links cl
    JOIN canada_laws l ON l.id=cl.law_id
    LEFT JOIN source_items si ON si.id=cl.case_item_id
    ORDER BY cl.id ASC
    {limit_clause}
    """
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(text(sql), params).mappings().all()]
    edge_ids: set[int] = set()
    with engine.begin() as conn:
        for row in rows:
            case_id = int(row.get("case_item_id") or 0)
            law_id = int(row.get("law_id") or 0)
            if not case_id or not law_id:
                continue
            case_entity_id = _upsert_entity(
                conn,
                entity_type="Case",
                canonical_key=_document_key("Case", "source_items", case_id),
                canonical_name=repair_text(row.get("case_title") or row.get("case_source_uid") or f"Case {case_id}"),
                aliases=[repair_text(row.get("case_title")), repair_text(row.get("case_source_uid"))],
                metadata={"source_table": "source_items", "source_id": case_id},
                confidence=1.0,
                source_kind="case",
            )
            law_entity_id = _upsert_entity(
                conn,
                entity_type="Statute",
                canonical_key=_document_key("Statute", "canada_laws", law_id),
                canonical_name=repair_text(row.get("law_title") or row.get("law_citation") or f"Statute {law_id}"),
                jurisdiction=repair_text(row.get("law_jurisdiction")),
                aliases=[
                    repair_text(row.get("law_title")), repair_text(row.get("law_citation")),
                    repair_text(row.get("matched_alias")),
                ],
                metadata={
                    "source_table": "canada_laws", "source_id": law_id,
                    "source_item_id": row.get("law_source_item_id"),
                    "source_url": repair_text(row.get("law_source_url")),
                },
                confidence=1.0,
                source_kind="law",
            )
            if row.get("case_chunk_id"):
                _link_chunk(conn, int(row["case_chunk_id"]), case_entity_id, "document", 1.0)
            if row.get("law_chunk_id"):
                _link_chunk(conn, int(row["law_chunk_id"]), law_entity_id, "document", 1.0)
            relation_confidence = max(0.65, min(float(row.get("match_score") or 0.0), 0.95))
            edge_ids.add(
                _upsert_edge(
                    conn,
                    source_entity_id=case_entity_id,
                    target_entity_id=law_entity_id,
                    relation_type="MENTIONS",
                    source_chunk_id=int(row["case_chunk_id"]) if row.get("case_chunk_id") else None,
                    source_ref=f"canada_case_law_links:{case_id}:{law_id}",
                    evidence_excerpt=repair_text(row.get("evidence_excerpt")),
                    extraction_method=repair_text(row.get("match_source") or "existing_case_law_link"),
                    confidence=relation_confidence,
                )
            )
    return {"links_seen": len(rows), "edges_written": len(edge_ids)}


def rebuild_legal_graph(source_filter: str = "all", limit: int | None = None) -> dict[str, Any]:
    ensure_rag_tables()
    ensure_legal_graph_tables()
    started_at = datetime.utcnow()
    documents = _seed_document_rows(source_filter, limit)
    authority_links = _seed_canada_case_law_links(limit)
    result = get_legal_graph_status()
    return {
        "status": "completed",
        "schema_version": GRAPH_SCHEMA_VERSION,
        "source_filter": source_filter,
        "documents": documents,
        "authority_links": authority_links,
        "graph": result,
        "duration_seconds": round((datetime.utcnow() - started_at).total_seconds(), 3),
    }


def build_case_query_graph(query: str, *, keywords: list[str] | None = None) -> dict[str, Any]:
    clean_query = repair_text(query)
    plan = build_legal_query_plan(clean_query, keywords=keywords or [])
    nodes: list[dict[str, Any]] = [{"query_id": "Q-CASE", "entity_type": "Case", "name": "current case"}]
    edges: list[dict[str, Any]] = []

    def add_node(entity_type: str, name: str, relation: str) -> None:
        normalized = _normalize_name(name)
        if not normalized:
            return
        for node in nodes:
            if node.get("entity_type") == entity_type and _normalize_name(node.get("name")) == normalized:
                return
        node_id = f"Q-{len(nodes) + 1}"
        nodes.append({"query_id": node_id, "entity_type": entity_type, "name": repair_text(name)})
        edges.append({"source": "Q-CASE", "target": node_id, "relation_type": relation})

    for value in plan.get("jurisdictions") or []:
        add_node("LegalConcept", value, "HAS_JURISDICTION")
    for value in plan.get("issues") or []:
        add_node("LegalIssue", value, "HAS_ISSUE")
    for value in plan.get("domains") or []:
        add_node("LegalConcept", value, "RELATED_CONCEPT")
    for pattern, concept in _LEGAL_CONCEPT_HINTS:
        if re.search(pattern, clean_query, re.I):
            add_node("LegalConcept", concept, "RELATED_CONCEPT")
    for name in _LAW_NAME_RE.findall(clean_query):
        add_node("Statute", name, "MENTIONS")
    for citation in _CITATION_RE.findall(clean_query):
        add_node("Statute", citation, "MENTIONS")
    for term in (plan.get("meaningful_terms") or [])[:8]:
        if _normalize_name(term) not in _GENERIC_TERMS:
            add_node("LegalConcept", term, "RELATED_CONCEPT")
    extracted = extract_legal_entities(clean_query, jurisdiction=(plan.get("jurisdictions") or ["Canada"])[0])
    for entity in extracted.get("entities") or []:
        entity_type = repair_text(entity.get("entity_type"))
        relation = {
            "Statute": "MENTIONS", "Section": "MENTIONS", "Case": "CITES",
            "Court": "DECIDED_BY", "LegalIssue": "HAS_ISSUE",
        }.get(entity_type, "RELATED_CONCEPT")
        add_node(entity_type, repair_text(entity.get("canonical_name")), relation)
    return {
        "query": clean_query,
        "nodes": nodes,
        "edges": edges,
        "legal_domains": plan.get("domains") or [],
        "issues": plan.get("issues") or [],
        "entity_extraction": extracted,
    }


def _candidate_terms(query_graph: dict[str, Any]) -> list[str]:
    names = [
        repair_text(node.get("name"))
        for node in query_graph.get("nodes") or []
        if node.get("query_id") != "Q-CASE"
    ]
    if not names:
        raw = repair_text(query_graph.get("query"))
        names.extend(re.findall(r"[A-Za-z][A-Za-z0-9'/-]{2,}|[\u3400-\u9fff]{2,12}", raw))
    return [
        value for value in _unique(names, limit=16)
        if _normalize_name(value) not in _GENERIC_TERMS and len(_normalize_name(value)) >= 3
    ]


def _entry_score(entity: dict[str, Any], terms: list[str]) -> float:
    canonical = _normalize_name(entity.get("canonical_name"))
    aliases = _json_value(entity.get("aliases_json"))
    alias_text = " ".join(repair_text(value) for value in aliases) if isinstance(aliases, list) else ""
    haystack = _normalize_name(f"{canonical} {alias_text}")
    best = 0.0
    for term in terms:
        normalized = _normalize_name(term)
        if not normalized:
            continue
        if canonical == normalized:
            best = max(best, 1.0)
        elif " " in normalized and (normalized in haystack or canonical in normalized):
            best = max(best, 0.78)
        elif normalized in set(haystack.split()):
            best = max(best, 0.72)
        else:
            left = set(normalized.split())
            right = set(haystack.split())
            if left and right:
                best = max(best, min(len(left & right) / len(left), 1.0) * 0.65)
    confidence = float(entity.get("confidence") or 0.0)
    return round(best * (0.7 + 0.3 * confidence), 6)


def _find_entry_entities(terms: list[str], jurisdiction: str = "") -> list[dict[str, Any]]:
    if not terms:
        return []
    params: dict[str, Any] = {}
    clauses = []
    for index, term in enumerate(terms):
        key = f"term_{index}"
        params[key] = f"%{_normalize_name(term)}%"
        clauses.append(f"LOWER(canonical_name) LIKE :{key}")
        clauses.append(f"LOWER(CAST(aliases_json AS TEXT)) LIKE :{key}")
    jurisdiction_clause = ""
    if repair_text(jurisdiction):
        params["jurisdiction"] = repair_text(jurisdiction).lower()
        jurisdiction_clause = "AND (jurisdiction='' OR LOWER(jurisdiction)=:jurisdiction)"
    sql = f"""
    SELECT id, entity_type, canonical_key, canonical_name, jurisdiction,
           aliases_json, metadata_json, confidence, source_kind
    FROM legal_graph_entities
    WHERE ({' OR '.join(clauses)})
      {jurisdiction_clause}
    LIMIT 160
    """
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(text(sql), params).mappings().all()]
    for row in rows:
        row["entry_score"] = _entry_score(row, terms)
    rows = [row for row in rows if float(row.get("entry_score") or 0) >= 0.35]
    return sorted(rows, key=lambda row: float(row.get("entry_score") or 0), reverse=True)[:8]


def _fetch_edges(frontier: set[int], min_confidence: float) -> list[dict[str, Any]]:
    if not frontier:
        return []
    params: dict[str, Any] = {"min_confidence": min_confidence}
    placeholders = []
    for index, entity_id in enumerate(sorted(frontier)):
        key = f"entity_{index}"
        params[key] = int(entity_id)
        placeholders.append(f":{key}")
    joined = ",".join(placeholders)
    sql = f"""
    SELECT id, source_entity_id, target_entity_id, relation_type, source_chunk_id,
           source_ref, evidence_excerpt, extraction_method, confidence, valid_from, valid_to
    FROM legal_graph_edges
    WHERE confidence >= :min_confidence
      AND (source_entity_id IN ({joined}) OR target_entity_id IN ({joined}))
    LIMIT 500
    """
    with engine.connect() as conn:
        return [dict(row) for row in conn.execute(text(sql), params).mappings().all()]


def _traverse(entries: list[dict[str, Any]], max_hops: int, min_confidence: float) -> dict[int, dict[str, Any]]:
    paths: dict[int, dict[str, Any]] = {
        int(entry["id"]): {
            "entry_id": int(entry["id"]),
            "node_ids": [int(entry["id"])],
            "edges": [],
            "score": float(entry.get("entry_score") or 0.0),
            "hops": 0,
        }
        for entry in entries
    }
    frontier = set(paths)
    for hop in range(1, max_hops + 1):
        rows = _fetch_edges(frontier, min_confidence)
        next_frontier: set[int] = set()
        for edge in rows:
            source_id = int(edge.get("source_entity_id") or 0)
            target_id = int(edge.get("target_entity_id") or 0)
            for current_id, neighbor_id in ((source_id, target_id), (target_id, source_id)):
                if current_id not in frontier or not neighbor_id:
                    continue
                current_path = paths[current_id]
                if neighbor_id in current_path["node_ids"]:
                    continue
                edge_confidence = float(edge.get("confidence") or 0.0)
                score = float(current_path["score"]) * edge_confidence * (0.92 if hop == 1 else 0.82)
                existing = paths.get(neighbor_id)
                if existing is not None and float(existing.get("score") or 0.0) >= score:
                    continue
                paths[neighbor_id] = {
                    "entry_id": current_path["entry_id"],
                    "node_ids": list(current_path["node_ids"]) + [neighbor_id],
                    "edges": list(current_path["edges"]) + [dict(edge)],
                    "score": round(score, 6),
                    "hops": hop,
                }
                next_frontier.add(neighbor_id)
                if len(paths) >= 80:
                    return paths
        frontier = next_frontier
        if not frontier:
            break
    return paths


def _chunk_filter_sql(source_filter: str, filters: dict[str, str] | None, params: dict[str, Any]) -> str:
    clauses = []
    source = repair_text(source_filter or "all").lower()
    if source in {"case", "law"}:
        params["source_kind"] = source
        clauses.append("rc.source_kind=:source_kind")
    elif source not in {"", "all", "canada"}:
        params["source_code"] = source
        clauses.append("rc.source_code=:source_code")
    for key in ("jurisdiction", "document_type", "court_level", "language"):
        value = repair_text((filters or {}).get(key))
        if value:
            params[f"filter_{key}"] = value.lower()
            clauses.append(f"LOWER(rc.{key})=:filter_{key}")
    date_from = repair_text((filters or {}).get("date_from"))
    date_to = repair_text((filters or {}).get("date_to"))
    if date_from:
        params["date_from"] = date_from
        clauses.append("DATE(rc.published_at)>=DATE(:date_from)" if is_sqlite() else "rc.published_at>=CAST(:date_from AS timestamp)")
    if date_to:
        params["date_to"] = date_to
        clauses.append("DATE(rc.published_at)<=DATE(:date_to)" if is_sqlite() else "rc.published_at<=CAST(:date_to AS timestamp)")
    return " AND " + " AND ".join(clauses) if clauses else ""


def _path_payload(path: dict[str, Any], entities: dict[int, dict[str, Any]]) -> dict[str, Any]:
    return {
        "entry_entity_id": path.get("entry_id"),
        "hops": path.get("hops"),
        "score": path.get("score"),
        "nodes": [
            {
                "entity_id": entity_id,
                "entity_type": repair_text((entities.get(entity_id) or {}).get("entity_type")),
                "name": repair_text((entities.get(entity_id) or {}).get("canonical_name")),
            }
            for entity_id in path.get("node_ids") or []
        ],
        "relations": [
            {
                "relation_type": repair_text(edge.get("relation_type")),
                "confidence": round(float(edge.get("confidence") or 0.0), 4),
                "source_ref": repair_text(edge.get("source_ref")),
                "source_chunk_id": edge.get("source_chunk_id"),
            }
            for edge in path.get("edges") or []
        ],
    }


def search_legal_graph(
    query: str,
    *,
    keywords: list[str] | None = None,
    module: str = "canada",
    source_filter: str = "all",
    limit: int = 12,
    filters: dict[str, str] | None = None,
    max_hops: int | None = None,
) -> dict[str, Any]:
    del module  # The first graph version only contains legal case/law nodes from the shared RAG corpus.
    ensure_legal_graph_tables()
    clean_query = repair_text(query)
    if not clean_query:
        return {"status": "empty_query", "query": clean_query, "items": [], "total": 0}
    query_graph = build_case_query_graph(clean_query, keywords=keywords)
    terms = _candidate_terms(query_graph)
    jurisdiction = repair_text((filters or {}).get("jurisdiction"))
    entries = _find_entry_entities(terms, jurisdiction=jurisdiction)
    if not entries:
        return {
            "status": "no_graph_match", "query": clean_query, "query_graph": query_graph,
            "entry_entities": [], "items": [], "total": 0, "paths": 0,
        }

    max_hops = max(0, min(int(max_hops if max_hops is not None else getattr(settings, "rag_graph_max_hops", 3)), 4))
    min_confidence = max(0.0, min(float(getattr(settings, "rag_graph_edge_min_confidence", 0.65)), 1.0))
    paths = _traverse(entries, max_hops, min_confidence)
    entity_ids = sorted(paths)
    params: dict[str, Any] = {}
    placeholders = []
    for index, entity_id in enumerate(entity_ids):
        key = f"entity_{index}"
        params[key] = entity_id
        placeholders.append(f":{key}")
    params["row_limit"] = max(40, min(int(limit) * 20, 300))
    filter_sql = _chunk_filter_sql(source_filter, filters, params)
    sql = f"""
    SELECT gce.entity_id, gce.relation_role, gce.confidence AS link_confidence,
           rc.id, rc.source_kind, rc.source_table, rc.source_id, rc.source_code,
           rc.source_uid, rc.title, rc.source_url, rc.jurisdiction, rc.document_type,
           rc.court_level, rc.language, rc.citation, rc.published_at, rc.chunk_index,
           rc.text_content, rc.metadata_json
    FROM legal_graph_chunk_entities gce
    JOIN rag_chunks rc ON rc.id=gce.chunk_id
    WHERE gce.entity_id IN ({','.join(placeholders)})
      {filter_sql}
    ORDER BY gce.confidence DESC, rc.chunk_index ASC
    LIMIT :row_limit
    """
    with engine.connect() as conn:
        chunk_rows = [dict(row) for row in conn.execute(text(sql), params).mappings().all()]
        entity_rows = [
            dict(row)
            for row in conn.execute(
                text(
                    f"SELECT id, entity_type, canonical_name FROM legal_graph_entities "
                    f"WHERE id IN ({','.join(placeholders)})"
                ),
                {key: value for key, value in params.items() if key.startswith("entity_")},
            ).mappings().all()
        ]
    entities = {int(row["id"]): row for row in entity_rows}
    deduped: dict[str, dict[str, Any]] = {}
    for row in chunk_rows:
        entity_id = int(row.get("entity_id") or 0)
        path = paths.get(entity_id)
        if not path:
            continue
        graph_score = round(float(path.get("score") or 0.0) * float(row.get("link_confidence") or 0.0), 6)
        metadata = _json_value(row.get("metadata_json"))
        if not isinstance(metadata, dict):
            metadata = {}
        item = {
            "chunk_id": int(row.get("id") or 0),
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
            "metadata": metadata,
            "score": graph_score,
            "graph_score": graph_score,
            "graph_entity_id": entity_id,
            "graph_path": _path_payload(path, entities),
            "search_channels": ["graph"],
        }
        key = f"{item['source_table']}:{item['source_id']}"
        existing = deduped.get(key)
        if existing is None or graph_score > float(existing.get("graph_score") or 0.0):
            deduped[key] = item
    items = sorted(deduped.values(), key=lambda item: float(item.get("graph_score") or 0.0), reverse=True)
    items = items[: max(1, min(int(limit), 30))]
    return {
        "status": "ok",
        "query": clean_query,
        "query_graph": query_graph,
        "entry_entities": [
            {
                "entity_id": int(entry["id"]), "entity_type": entry.get("entity_type"),
                "name": entry.get("canonical_name"), "score": entry.get("entry_score"),
            }
            for entry in entries
        ],
        "items": items,
        "total": len(items),
        "paths": len(paths),
        "max_hops": max_hops,
        "min_edge_confidence": min_confidence,
        "strategy": "typed_legal_graph_1_to_2_hops",
    }


def get_legal_graph_status() -> dict[str, Any]:
    ensure_legal_graph_tables()
    with engine.connect() as conn:
        entity_count = int(conn.execute(text("SELECT COUNT(*) FROM legal_graph_entities")).scalar() or 0)
        edge_count = int(conn.execute(text("SELECT COUNT(*) FROM legal_graph_edges")).scalar() or 0)
        chunk_links = int(conn.execute(text("SELECT COUNT(*) FROM legal_graph_chunk_entities")).scalar() or 0)
        types = [
            dict(row)
            for row in conn.execute(
                text("SELECT entity_type, COUNT(*) AS total FROM legal_graph_entities GROUP BY entity_type ORDER BY total DESC")
            ).mappings().all()
        ]
    return {
        "status": "ready",
        "schema_version": GRAPH_SCHEMA_VERSION,
        "entities": entity_count,
        "edges": edge_count,
        "chunk_links": chunk_links,
        "entity_types": types,
    }
