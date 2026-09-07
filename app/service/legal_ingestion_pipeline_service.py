from __future__ import annotations

from datetime import datetime

from app.service.canlii_service import sync_canlii_by_keywords
from app.service.common_service import repair_text
from app.service.legal_query_planner_service import build_legal_query_plan
from app.service.module_service import normalize_module
from app.service.rag_service import rebuild_rag_index
from app.service.vector_store_service import rebuild_chunk_embeddings


def hydrate_canlii_and_rebuild(
    query: str = "",
    *,
    keywords: list[str] | None = None,
    target_count: int | None = None,
    module: str = "canada",
    skip_ingest: bool = False,
    skip_rebuild: bool = False,
) -> dict:
    started_at = datetime.utcnow()
    clean_query = repair_text(query)
    plan = build_legal_query_plan(clean_query, keywords=keywords or [])
    hydration_keywords = []
    for value in (
        list(keywords or [])
        + list(plan.get("meaningful_terms") or [])
        + list(plan.get("domains") or [])
        + list(plan.get("issues") or [])
    ):
        clean = repair_text(value)
        if clean and clean.lower() not in {item.lower() for item in hydration_keywords}:
            hydration_keywords.append(clean)
        if len(hydration_keywords) >= 12:
            break

    steps: list[dict] = []
    status = "completed"
    if not hydration_keywords and not skip_ingest:
        status = "failed"
        steps.append({"step": "hydrate_canlii", "status": "failed", "error_message": "No keywords generated."})
    elif skip_ingest:
        steps.append({"step": "hydrate_canlii", "status": "skipped", "keywords": hydration_keywords})
    else:
        ingest_result = sync_canlii_by_keywords(hydration_keywords, target_count=target_count)
        steps.append({"step": "hydrate_canlii", "keywords": hydration_keywords, **ingest_result})
        if ingest_result.get("status") not in {"success", "partial_success"} and ingest_result.get("error_type") not in {
            "no_match",
            "missing_keywords",
        }:
            status = "partial_failure"

    if skip_rebuild:
        steps.append({"step": "rebuild_rag", "status": "skipped"})
        steps.append({"step": "rebuild_vectors", "status": "skipped"})
    else:
        rag_result = rebuild_rag_index(source_filter="canlii")
        steps.append({"step": "rebuild_rag", **rag_result})
        if rag_result.get("status") != "completed":
            status = "partial_failure"

        vector_result = rebuild_chunk_embeddings(source_filter="canlii", module=module)
        steps.append({"step": "rebuild_vectors", **vector_result})
        if vector_result.get("status") != "completed":
            status = "partial_failure"

    return {
        "status": status,
        "query": clean_query,
        "module": normalize_module(module),
        "structured_query": plan,
        "hydration_keywords": hydration_keywords,
        "steps": steps,
        "duration_seconds": round((datetime.utcnow() - started_at).total_seconds(), 3),
    }
