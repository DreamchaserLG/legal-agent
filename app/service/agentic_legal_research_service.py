from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.service.common_service import repair_text
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.legal_query_planner_service import build_deep_query_plan


@dataclass
class ResearchEvidence:
    evidence_id: str
    title: str
    source_kind: str
    citation: str
    source_url: str
    excerpt: str
    score: float
    query: str
    round_no: int
    graph_path: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def _unique(values: list[str], limit: int) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = repair_text(value)
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        output.append(clean)
        if len(output) >= limit:
            break
    return output


def _evidence_id(item: dict[str, Any]) -> str:
    return ":".join(
        str(value or "")
        for value in (item.get("source_table"), item.get("source_id"), item.get("chunk_id"))
    )


def _normalize_evidence(item: dict[str, Any], query: str, round_no: int) -> ResearchEvidence | None:
    evidence_id = _evidence_id(item)
    if evidence_id == "::":
        return None
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    bundle = item.get("context_bundle") if isinstance(item.get("context_bundle"), dict) else {}
    return ResearchEvidence(
        evidence_id=evidence_id,
        title=repair_text(item.get("title")),
        source_kind=repair_text(item.get("source_kind")),
        citation=repair_text(item.get("citation") or metadata.get("citation")),
        source_url=repair_text(item.get("source_url")),
        excerpt=repair_text(bundle.get("text") or item.get("excerpt"))[:3000],
        score=float(item.get("score") or 0),
        query=query,
        round_no=round_no,
        graph_path=item.get("graph_path") if isinstance(item.get("graph_path"), dict) else {},
        metadata=metadata,
    )


def _initial_queries(plan: Any, query: str, limit: int) -> list[str]:
    issue_queries = [repair_text(f"{issue.issue} {issue.legal_domain}") for issue in plan.dispute_issues]
    queries = issue_queries + [repair_text(plan.rule_query), repair_text(plan.case_query)]
    if not queries:
        queries = [query]
    return _unique(queries, limit)


def _coverage(plan: Any, evidence: list[ResearchEvidence]) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    gaps: list[str] = []
    for issue in plan.dispute_issues:
        terms = {
            token.casefold()
            for token in repair_text(f"{issue.issue} {issue.legal_domain}").replace("/", " ").split()
            if len(token) >= 3
        }
        generic = {
            "ontario", "canada", "canadian", "legal", "law", "case", "evidence",
            "burden", "issue", "civil", "procedure", "remedies", "obligation",
        }
        meaningful_terms = {term.strip(".,:;()[]") for term in terms if term.strip(".,:;()[]") not in generic}
        strong_terms = {
            "n12", "tenancy", "tenant", "landlord", "maintenance", "repair", "mould",
            "habitability", "eviction", "retaliatory", "bad", "faith", "residential",
        }
        matches = []
        for item in evidence:
            haystack = f"{item.title} {item.citation} {item.excerpt}".casefold()
            hits = {term for term in meaningful_terms if term in haystack}
            # Do not mark an issue covered merely because a result says Ontario
            # or contains generic procedural vocabulary. One strong domain term
            # or two independent issue terms are required.
            if len(hits) >= 2 or bool(hits & strong_terms):
                matches.append(item)
        laws = [item for item in matches if item.source_kind == "law"]
        cases = [item for item in matches if item.source_kind == "case"]
        status = "covered" if laws and cases else "partial" if matches else "missing"
        if not laws:
            gaps.append(f"{issue.issue}：缺少可直接绑定的正式法规或条文")
        if not cases:
            gaps.append(f"{issue.issue}：缺少可比较的裁判案例")
        rows.append(
            {
                "issue": issue.issue,
                "legal_domain": issue.legal_domain,
                "status": status,
                "law_evidence_ids": [item.evidence_id for item in laws[:5]],
                "case_evidence_ids": [item.evidence_id for item in cases[:5]],
            }
        )
    if not plan.dispute_issues and not evidence:
        gaps.append("尚未识别出可检索争点或支持材料")
    return rows, _unique(gaps, 20)


def _follow_up_queries(plan: Any, coverage: list[dict[str, Any]], gaps: list[str], limit: int) -> list[str]:
    queries: list[str] = []
    for row in coverage:
        issue = repair_text(row.get("issue"))
        domain = repair_text(row.get("legal_domain"))
        if row.get("status") != "covered":
            if not row.get("law_evidence_ids"):
                queries.append(f"{issue} {domain} statute regulation elements exceptions effective date")
            if not row.get("case_evidence_ids"):
                queries.append(f"{issue} {domain} case law applied distinguished sentencing factors")
    for gap in gaps:
        queries.append(f"{gap} 适用条件 例外 关联规则")
    return _unique(queries, limit)


def run_agentic_legal_research(
    query: str,
    *,
    keywords: list[str] | None = None,
    module: str = "canada",
    source_filter: str = "all",
    filters: dict[str, Any] | None = None,
    max_rounds: int | None = None,
    max_queries_per_round: int | None = None,
    max_evidence: int | None = None,
    timeout_seconds: int | None = None,
    event_callback: Any | None = None,
) -> dict[str, Any]:
    """Run bounded, gap-driven legal research; retrieved text is data, never instructions."""
    clean_query = repair_text(query)
    if not clean_query:
        return {"status": "empty_query", "rounds": [], "evidence": [], "coverage": [], "gaps": []}
    if repair_text(module).lower() != "canada":
        return {
            "status": "unsupported_jurisdiction",
            "jurisdiction": repair_text(module),
            "rounds": [], "evidence": [], "coverage": [],
            "unresolved_gaps": ["Agentic legal research is restricted to the installed Canadian legal corpus."],
            "review_required": True,
        }
    max_rounds = max(1, min(int(max_rounds or settings.legal_research_max_rounds), 5))
    per_round = max(1, min(int(max_queries_per_round or settings.legal_research_queries_per_round), 10))
    evidence_limit = max(5, min(int(max_evidence or settings.legal_research_max_evidence), 100))
    timeout = max(5, min(int(timeout_seconds or settings.legal_research_timeout_seconds), 300))
    started = time.monotonic()
    plan = build_deep_query_plan(clean_query, keywords=keywords or [], use_llm=True, load_priors=True)
    research_filters = dict(filters or {})
    if not research_filters and any(
        repair_text(value).lower() == "ontario" for value in (getattr(plan, "jurisdictions", []) or [])
    ):
        research_filters["jurisdiction_hint"] = "Ontario"
    queries = _initial_queries(plan, clean_query, per_round)
    if event_callback:
        event_callback("research_plan", "已完成争点拆分并生成首轮法规与案例查询。", {
            "issue_count": len(plan.dispute_issues), "queries": queries,
        })
    evidence_by_id: dict[str, ResearchEvidence] = {}
    rounds: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    gaps: list[str] = []
    stop_reason = "max_rounds_reached"

    for round_no in range(1, max_rounds + 1):
        if time.monotonic() - started >= timeout:
            stop_reason = "timeout"
            break
        round_started = time.monotonic()
        executed: list[dict[str, Any]] = []
        new_count = 0
        workers = min(4, len(queries))
        pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="legal-research")
        try:
            futures = {
                pool.submit(
                    hybrid_search,
                    search_query,
                    keywords=keywords or [],
                    module=module,
                    source_filter=source_filter,
                    limit=min(12, evidence_limit),
                    filters=research_filters,
                    include_vector=round_no <= max(0, int(settings.legal_research_vector_rounds)),
                ): search_query
                for search_query in queries
            }
            if event_callback:
                event_callback("research_round_started", f"开始第 {round_no} 轮并行检索。", {
                    "round_no": round_no, "queries": queries,
                })
            remaining = max(0.1, timeout - (time.monotonic() - started))
            done, pending = wait(futures, timeout=remaining)
            for future in done:
                search_query = futures[future]
                try:
                    result = future.result()
                    items = result.get("items") or []
                    executed.append({"query": search_query, "status": result.get("status", "ok"), "hits": len(items)})
                    for item in items:
                        normalized = _normalize_evidence(item, search_query, round_no)
                        if not normalized:
                            continue
                        current = evidence_by_id.get(normalized.evidence_id)
                        if current is None:
                            evidence_by_id[normalized.evidence_id] = normalized
                            new_count += 1
                        elif normalized.score > current.score:
                            evidence_by_id[normalized.evidence_id] = normalized
                except Exception as exc:
                    executed.append({"query": search_query, "status": "error", "error": repair_text(str(exc))[:240], "hits": 0})
            for future in pending:
                future.cancel()
                executed.append({"query": futures[future], "status": "timeout", "hits": 0})
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        all_ranked = sorted(evidence_by_id.values(), key=lambda item: item.score, reverse=True)
        # A single global cutoff allowed the much larger case corpus to evict
        # every statute from the evidence packet. Preserve a balanced authority
        # set before filling remaining slots by score.
        quota = max(1, evidence_limit // 3)
        selected = (
            [item for item in all_ranked if item.source_kind == "law"][:quota]
            + [item for item in all_ranked if item.source_kind == "case"][:quota]
        )
        selected_ids = {item.evidence_id for item in selected}
        selected.extend(item for item in all_ranked if item.evidence_id not in selected_ids)
        ranked = sorted(selected[:evidence_limit], key=lambda item: item.score, reverse=True)
        evidence_by_id = {item.evidence_id: item for item in ranked}
        coverage, gaps = _coverage(plan, ranked)
        next_queries = _follow_up_queries(plan, coverage, gaps, per_round)
        rounds.append(
            {
                "round_no": round_no,
                "queries": executed,
                "new_evidence_count": new_count,
                "total_evidence_count": len(ranked),
                "unresolved_gap_count": len(gaps),
                "duration_ms": int((time.monotonic() - round_started) * 1000),
            }
        )
        if event_callback:
            event_callback("research_round_completed", f"第 {round_no} 轮检索完成，正在评估证据缺口。", rounds[-1])
        if not gaps:
            stop_reason = "all_issues_covered"
            break
        if round_no > 1 and new_count < int(settings.legal_research_min_new_evidence):
            stop_reason = "diminishing_returns"
            break
        if not next_queries:
            stop_reason = "no_material_follow_up"
            break
        queries = next_queries

    evidence = sorted(evidence_by_id.values(), key=lambda item: item.score, reverse=True)
    return {
        "status": "ready" if evidence else "insufficient_evidence",
        "jurisdiction": "Canada",
        "strategy": "bounded_gap_driven_agentic_hybrid_graph_research",
        "query_plan": plan.model_dump(mode="json"),
        "rounds": rounds,
        "evidence": [item.__dict__ for item in evidence],
        "coverage": coverage,
        "unresolved_gaps": gaps,
        "stop_reason": stop_reason,
        "review_required": True,
        "external_text_policy": "untrusted_data_only",
        "duration_ms": int((time.monotonic() - started) * 1000),
    }
