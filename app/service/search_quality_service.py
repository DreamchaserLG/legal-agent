from __future__ import annotations

"""
Search quality verification agent.
Uses LLM to evaluate and re-rank search results for relevance.
"""
import json
import logging

from app.service.llm_service import create_structured_response, is_llm_configured

logger = logging.getLogger(__name__)

RERANK_SCHEMA = {
    "type": "object",
    "properties": {
        "ranked_indices": {
            "type": "array",
            "items": {"type": "integer"},
            "description": "Indices of results ordered by relevance (most relevant first)",
        },
        "relevance_scores": {
            "type": "array",
            "items": {"type": "number"},
            "description": "Relevance scores from 0.0 to 1.0 for each result",
        },
        "reasoning": {
            "type": "string",
            "description": "Brief explanation of ranking decisions",
        },
    },
    "required": ["ranked_indices", "relevance_scores"],
}


def rerank_results(query: str, results: list[dict], max_results: int = 20) -> list[dict]:
    """
    Re-rank search results using LLM for semantic relevance.
    Returns results sorted by relevance.
    """
    if not results or not is_llm_configured():
        return results

    if len(results) <= 3:
        return results

    results_to_rank = results[:max_results]

    result_summaries = []
    for i, r in enumerate(results_to_rank):
        title = r.get("title", "")
        summary = (r.get("summary") or r.get("snippet") or "")[:200]
        source = r.get("source_code", "")
        result_summaries.append(f"[{i}] ({source}) {title}: {summary}")

    user_input = (
        f"User query: {query}\n\n"
        f"Search results:\n" + "\n".join(result_summaries) + "\n\n"
        "Rank these results by relevance to the user's query. "
        "Focus on legal relevance - cases and laws that directly address the legal issues in the query."
    )

    try:
        response = create_structured_response(
            schema_name="search_rerank",
            schema=RERANK_SCHEMA,
            instructions=(
                "You are a legal search quality evaluator. "
                "Rank the provided search results by their relevance to the user's legal query. "
                "Consider: direct legal issue match, applicable jurisdiction, case law relevance, "
                "statute applicability. Return indices ordered by relevance."
            ),
            user_input=user_input,
        )

        data = response.get("data", {})
        ranked_indices = data.get("ranked_indices", [])
        relevance_scores = data.get("relevance_scores", [])

        if not ranked_indices or len(ranked_indices) != len(results_to_rank):
            return results

        reranked = []
        for idx in ranked_indices:
            if 0 <= idx < len(results_to_rank):
                result = dict(results_to_rank[idx])
                score_idx = ranked_indices.index(idx)
                if score_idx < len(relevance_scores):
                    result["semantic_relevance"] = relevance_scores[score_idx]
                reranked.append(result)

        remaining = results[max_results:]
        return reranked + remaining

    except Exception as e:
        logger.warning(f"Search reranking failed: {e}")
        return results
