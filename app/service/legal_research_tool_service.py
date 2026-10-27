from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from app.core.config import settings
from app.service.canlii_service import search_canlii_by_keywords_realtime
from app.service.common_service import repair_text, sha256_text, split_keywords
from app.service.context_window_service import expand_context_windows, restrict_context_bundle_to_acl
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.legal_graph_rag_service import search_legal_graph
from app.service.multi_agent_service import assert_agent_tool


APPROVED_LEGAL_HOSTS = {
    "canlii.org",
    "www.canlii.org",
    "laws-lois.justice.gc.ca",
    "www.laws-lois.justice.gc.ca",
    "ontario.ca",
    "www.ontario.ca",
    "e-laws.gov.on.ca",
    "www.e-laws.gov.on.ca",
}

_HTTP = requests.Session()
_HTTP.trust_env = False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _redact(value: Any) -> str:
    text = repair_text(value)
    text = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[redacted-email]", text, flags=re.I)
    return re.sub(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)", "[redacted-phone]", text)


def _acl_allows(item: dict[str, Any], case_acl: list[str]) -> bool:
    if not case_acl:
        return True
    allowed = {repair_text(value) for value in case_acl if repair_text(value)}
    candidates = {
        repair_text(item.get("evidence_id")),
        repair_text(item.get("doc_id")),
        repair_text(item.get("source_uid")),
        f"{repair_text(item.get('source_table'))}:{item.get('source_id')}",
        f"{repair_text(item.get('source_table'))}:{item.get('source_id')}:{item.get('chunk_id')}",
    }
    return bool(allowed.intersection(candidates))


def _local_evidence(item: dict[str, Any]) -> dict[str, Any]:
    metadata = item.get("metadata") or {}
    doc_id = f"{item.get('source_table')}:{item.get('source_id')}:{item.get('chunk_id')}"
    context_bundle = item.get("context_bundle") or {}
    excerpt = _redact(context_bundle.get("text") or item.get("excerpt") or item.get("summary") or "")[:2400]
    source_url = repair_text(item.get("source_url"))
    citation = repair_text(item.get("citation") or metadata.get("citation"))
    return {
        "evidence_id": doc_id,
        "doc_id": doc_id,
        "source_uid": repair_text(item.get("source_uid")),
        "source_table": repair_text(item.get("source_table")),
        "source_id": item.get("source_id"),
        "chunk_id": item.get("chunk_id"),
        "title": repair_text(item.get("title")),
        "citation": citation,
        "court": repair_text(item.get("court_level") or metadata.get("court")),
        "decision_date": repair_text(item.get("published_at") or metadata.get("decision_date")),
        "authority_status": repair_text(metadata.get("authority_status") or metadata.get("in_force_status") or "unknown"),
        "source_url": source_url,
        "excerpt": excerpt,
        "score": round(float(item.get("score") or 0), 6),
        "retrieval_channels": list(item.get("search_channels") or []),
        "context_chunk_ids": list(context_bundle.get("chunk_ids") or []),
        "context_expanded": bool(item.get("context_expanded")),
        "graph_path": item.get("graph_path") or {},
        "source_kind": repair_text(item.get("source_kind")) or "legal_material",
        "retrieved_at": _utc_now(),
        "content_hash": sha256_text(f"{citation}|{source_url}|{excerpt}"),
        "verification_status": "verified_metadata" if citation or source_url else "needs_manual_citation_check",
        "origin": "local_legal_corpus",
    }


def search_local_legal_materials(
    query: str,
    *,
    source_filter: str,
    module: str,
    filters: dict[str, str],
    case_acl: list[str],
    limit: int = 6,
) -> dict[str, Any]:
    result = hybrid_search(
        repair_text(query),
        module=module,
        source_filter=source_filter,
        limit=max(1, min(int(limit), 10)),
        filters=filters,
    )
    safe_items = [restrict_context_bundle_to_acl(item, case_acl) for item in result.get("items") or []]
    evidence = [_local_evidence(item) for item in safe_items]
    evidence = [item for item in evidence if _acl_allows(item, case_acl)]
    return {
        "status": result.get("status", "ok"),
        "tool": "search_case_law" if source_filter == "case" else "search_statute",
        "query": repair_text(query),
        "evidence": evidence,
        "count": len(evidence),
        "strategy": result.get("strategy", "structured_hybrid"),
        "channels": result.get("channels") or {},
    }


def search_graph_legal_materials(
    query: str,
    *,
    module: str,
    filters: dict[str, str],
    case_acl: list[str],
    limit: int = 6,
) -> dict[str, Any]:
    result = search_legal_graph(
        repair_text(query),
        module=module,
        source_filter="all",
        limit=max(1, min(int(limit), 10)),
        filters=filters,
    )
    expanded = expand_context_windows(result.get("items") or [])
    safe_items = [restrict_context_bundle_to_acl(item, case_acl) for item in expanded.get("items") or []]
    evidence = [_local_evidence(item) for item in safe_items]
    evidence = [item for item in evidence if _acl_allows(item, case_acl)]
    return {
        "status": result.get("status", "ok"),
        "tool": "search_legal_graph",
        "query": repair_text(query),
        "query_graph": result.get("query_graph") or {},
        "entry_entities": result.get("entry_entities") or [],
        "path_count": int(result.get("paths") or 0),
        "evidence": evidence,
        "count": len(evidence),
        "strategy": result.get("strategy", "typed_legal_graph"),
    }


def search_approved_web(query: str, *, limit: int = 4) -> dict[str, Any]:
    if not getattr(settings, "remote_search_enabled", True):
        return {"status": "disabled", "tool": "search_approved_web", "query": query, "evidence": [], "count": 0}
    keywords = split_keywords(query)[:8]
    result = search_canlii_by_keywords_realtime(
        keywords,
        max_items=max(1, min(int(limit), 6)),
        use_api=True,
        use_rss=True,
    )
    evidence: list[dict[str, Any]] = []
    for item in result.get("items") or []:
        source_url = repair_text(item.get("url") or item.get("link"))
        title = repair_text(item.get("title"))
        citation = repair_text(item.get("citation"))
        excerpt = _redact(item.get("summary") or item.get("description") or "")[:1200]
        evidence_id = f"web:{sha256_text(source_url or f'{title}|{citation}')[:16]}"
        evidence.append(
            {
                "evidence_id": evidence_id,
                "doc_id": evidence_id,
                "title": title,
                "citation": citation,
                "court": repair_text(item.get("court") or item.get("database")),
                "decision_date": repair_text(item.get("date")),
                "authority_status": "unknown",
                "source_url": source_url,
                "excerpt": excerpt,
                "score": 0.45,
                "source_kind": "case",
                "retrieved_at": _utc_now(),
                "content_hash": sha256_text(f"{citation}|{source_url}|{excerpt}"),
                "verification_status": "verified_source_url" if source_url else "needs_manual_citation_check",
                "origin": "approved_web_canlii",
            }
        )
    return {
        "status": "ok",
        "tool": "search_approved_web",
        "query": repair_text(query),
        "evidence": evidence,
        "count": len(evidence),
        "strategy": result.get("method", "canlii"),
    }


def fetch_source_snapshot(url: str) -> dict[str, Any]:
    clean_url = repair_text(url)
    parsed = urlparse(clean_url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in APPROVED_LEGAL_HOSTS:
        raise PermissionError("Only approved HTTPS legal sources may be fetched.")
    response = _HTTP.get(
        clean_url,
        timeout=min(max(int(getattr(settings, "request_timeout", 30)), 5), 30),
        headers={"User-Agent": "legal-demo-research/1.0 (+read-only legal research)"},
    )
    response.raise_for_status()
    final_host = (urlparse(response.url).hostname or "").lower()
    if final_host not in APPROVED_LEGAL_HOSTS:
        raise PermissionError("Approved source redirected outside the legal-source allowlist.")
    content_type = repair_text(response.headers.get("content-type")).lower()
    if "html" not in content_type and "text" not in content_type and "xml" not in content_type:
        raise ValueError("Approved source did not return readable legal text.")
    soup = BeautifulSoup(response.text, "lxml")
    for element in soup(["script", "style", "noscript"]):
        element.decompose()
    content = repair_text(soup.get_text(" ", strip=True))[:12000]
    title = repair_text(soup.title.get_text(" ", strip=True) if soup.title else parsed.hostname)
    evidence_id = f"snapshot:{sha256_text(clean_url)[:16]}"
    evidence = {
        "evidence_id": evidence_id,
        "doc_id": evidence_id,
        "title": title,
        "citation": "",
        "court": "",
        "decision_date": "",
        "authority_status": "unknown",
        "source_url": clean_url,
        "excerpt": _redact(content[:1600]),
        "score": 0.4,
        "source_kind": "approved_web",
        "retrieved_at": _utc_now(),
        "content_hash": sha256_text(content),
        "verification_status": "verified_source_snapshot",
        "origin": "approved_web_snapshot",
    }
    return {"status": "ok", "tool": "fetch_source_snapshot", "evidence": [evidence], "count": 1}


def execute_research_tool(
    *,
    agent: str,
    tool_name: str,
    query: str,
    module: str,
    filters: dict[str, str],
    case_acl: list[str],
    limit: int = 6,
) -> dict[str, Any]:
    assert_agent_tool(agent, tool_name)
    if tool_name == "search_case_law":
        return search_local_legal_materials(
            query,
            source_filter="case",
            module=module,
            filters=filters,
            case_acl=case_acl,
            limit=limit,
        )
    if tool_name == "search_statute":
        return search_local_legal_materials(
            query,
            source_filter="law",
            module=module,
            filters=filters,
            case_acl=case_acl,
            limit=limit,
        )
    if tool_name == "search_legal_graph":
        return search_graph_legal_materials(
            query,
            module=module,
            filters=filters,
            case_acl=case_acl,
            limit=limit,
        )
    if tool_name == "search_approved_web":
        return search_approved_web(query, limit=min(limit, 4))
    if tool_name == "fetch_source_snapshot":
        return fetch_source_snapshot(query)
    raise PermissionError(f"Unsupported research tool: {tool_name}")


def verify_evidence_pool(evidence_pool: list[dict[str, Any]]) -> list[dict[str, Any]]:
    verification: list[dict[str, Any]] = []
    for item in evidence_pool:
        evidence_id = repair_text(item.get("evidence_id") or item.get("doc_id"))
        citation = repair_text(item.get("citation"))
        source_url = repair_text(item.get("source_url"))
        content_hash = repair_text(item.get("content_hash"))
        if source_url and content_hash:
            verdict = "verified_source_snapshot" if item.get("origin") == "approved_web_snapshot" else "verified_source_url"
        elif citation:
            verdict = "verified_metadata"
        else:
            verdict = "needs_manual_citation_check"
        verification.append(
            {
                "evidence_id": evidence_id,
                "citation_present": bool(citation),
                "source_url_present": bool(source_url),
                "content_hash_present": bool(content_hash),
                "verdict": verdict,
            }
        )
    return verification


def merge_evidence(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in list(existing or []) + list(incoming or []):
        evidence_id = repair_text(item.get("evidence_id") or item.get("doc_id"))
        if not evidence_id:
            continue
        current = merged.get(evidence_id)
        if current is None or float(item.get("score") or 0) > float(current.get("score") or 0):
            merged[evidence_id] = item
    return sorted(merged.values(), key=lambda item: float(item.get("score") or 0), reverse=True)[:30]
