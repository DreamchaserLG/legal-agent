from __future__ import annotations

"""Pluggable vector retrieval backends with deterministic local fallback."""

from pathlib import Path
from typing import Any

from app.core.config import settings
from app.core.database import engine
from app.service.common_service import repair_text
from app.service.embedding_service import create_embedding
from app.service.faiss_ivfpq_service import faiss_available, search_ivfpq_artifact
from app.service.vector_store_service import _row_to_item, _vector_text, embedding_metadata, vector_search
from sqlalchemy import text


def backend_status() -> dict[str, Any]:
    faiss_path = repair_text(getattr(settings, "vector_faiss_index_path", ""))
    remote_url = repair_text(getattr(settings, "vector_remote_hnsw_url", ""))
    faiss_installed = faiss_available()
    return {
        "active": repair_text(getattr(settings, "vector_backend", "pgvector_ivfflat")),
        "backends": {
            "pgvector_ivfflat": {"available": True, "role": "local_low_memory_baseline"},
            "faiss_ivfpq": {
                "available": bool(faiss_installed and faiss_path and Path(faiss_path).exists()),
                "installed": faiss_installed,
                "index_path": faiss_path,
                "role": "compressed_candidate_generator",
            },
            "remote_hnsw": {
                "available": bool(remote_url), "url_configured": bool(remote_url),
                "role": "optional_high_recall_remote_backend",
            },
        },
        "fallback_order": ["remote_hnsw", "faiss_ivfpq", "pgvector_ivfflat", "lexical_graph"],
    }


def search_vector_backend(
    query: str, *, backend: str = "auto", module: str = "canada",
    source_filter: str = "all", limit: int = 20, filters: dict | None = None,
) -> dict:
    requested = repair_text(backend or "auto").lower()
    status = backend_status()
    selected = requested
    if requested == "auto":
        selected = next(
            (name for name in status["fallback_order"] if (status["backends"].get(name) or {}).get("available")),
            "pgvector_ivfflat",
        )
    if selected == "faiss_ivfpq":
        query_vector = create_embedding(query)
        candidates = search_ivfpq_artifact(
            query_vector, index_path=status["backends"]["faiss_ivfpq"]["index_path"],
            limit=max(100, int(limit) * 20), probes=int(settings.rag_ivfflat_probes),
        )
        candidate_ids = [row["chunk_id"] for row in candidates]
        if not candidate_ids:
            return {"query": query, "items": [], "total": 0, "status": "ok", "selected_backend": selected}
        meta = embedding_metadata()
        params = {
            "ids": candidate_ids, "query_vector": _vector_text(query_vector),
            "provider": meta["provider"], "model": meta["model"], "limit": max(1, int(limit)),
        }
        source_clause = ""
        if source_filter in {"law", "case"}:
            source_clause = "AND rc.source_kind=:source_kind"
            params["source_kind"] = source_filter
        rows = []
        with engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(text(f"""
                SELECT rc.*, 1 - (e.embedding_vector <=> CAST(:query_vector AS vector)) AS score
                FROM rag_chunk_embeddings e JOIN rag_chunks rc ON rc.id=e.chunk_id
                WHERE e.chunk_id=ANY(:ids) AND e.embedding_provider=:provider AND e.embedding_model=:model
                  {source_clause}
                ORDER BY e.embedding_vector <=> CAST(:query_vector AS vector)
                LIMIT :limit
            """), params).mappings().all()]
        items = [_row_to_item(row, float(row.get("score") or 0)) for row in rows]
        return {
            "query": query, "items": items, "total": len(items), "status": "ok",
            "requested_backend": requested, "selected_backend": selected,
            "candidate_count": len(candidate_ids), "reranker": "postgres_fp32_cosine",
        }

    # Remote HNSW is deliberately not silently emulated. Until its
    # health-checked adapter is configured, fall back and report why.
    fallback_reason = ""
    if selected not in {"pgvector", "pgvector_ivfflat"}:
        fallback_reason = f"{selected}_not_configured_or_not_ready"
        selected = "pgvector_ivfflat"
    result = vector_search(
        query, module=module, source_filter=source_filter, limit=limit, filters=filters,
    )
    result["requested_backend"] = requested
    result["selected_backend"] = selected
    result["fallback_reason"] = fallback_reason
    return result
