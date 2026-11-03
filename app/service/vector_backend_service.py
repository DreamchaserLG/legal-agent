from __future__ import annotations

from typing import Any

from app.core.config import settings
from app.service.common_service import repair_text
from app.service.vector_index_management_service import get_hnsw_index_state
from app.service.vector_store_service import vector_search


def backend_status() -> dict[str, Any]:
    state = get_hnsw_index_state()
    ready = bool(state.get("valid") and state.get("ready"))
    return {
        "active": "pgvector_hnsw" if ready else "lexical_graph",
        "configured": repair_text(getattr(settings, "vector_backend", "pgvector_hnsw")),
        "backends": {
            "pgvector_hnsw": {
                "available": ready,
                "role": "local_semantic_ann",
                "index": state,
            },
            "lexical_graph": {
                "available": True,
                "role": "safe_fallback_without_vector_emulation",
            },
        },
        "fallback_order": ["pgvector_hnsw", "lexical_graph"],
        "migration_started": False,
    }


def search_vector_backend(
    query: str,
    *,
    module: str = "canada",
    source_filter: str = "all",
    limit: int = 10,
    filters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    status = backend_status()
    if not status["backends"]["pgvector_hnsw"]["available"]:
        return {
            "query": repair_text(query), "items": [], "total": 0,
            "status": "ann_unavailable", "backend": "lexical_graph",
            "reason": "A valid local HNSW index is required for vector retrieval.",
        }
    result = vector_search(
        query, module=module, source_filter=source_filter,
        limit=limit, filters=filters,
    )
    result["backend"] = "pgvector_hnsw"
    return result
