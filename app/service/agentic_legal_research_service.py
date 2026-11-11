from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError, as_completed
from dataclasses import dataclass, field
from typing import Any

from app.core.config import settings
from app.service.common_service import repair_text
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.legal_query_planner_service import build_deep_query_plan
from app.service.query_translation_service import translate_query


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


def _citation_link(citation: str) -> str:
    """远端语料的 source_url 列为空，用标准引文构造 CanLII 检索链接，保证证据可点开。"""
    clean = repair_text(citation)
    if not clean:
        return ""
    from urllib.parse import quote
    return f"https://www.canlii.org/en/search.do?q=%22{quote(clean)}%22"


def _normalize_evidence(item: dict[str, Any], query: str, round_no: int) -> ResearchEvidence | None:
    evidence_id = _evidence_id(item)
    if evidence_id == "::":
        return None
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    bundle = item.get("context_bundle") if isinstance(item.get("context_bundle"), dict) else {}
    citation = repair_text(item.get("citation") or metadata.get("citation"))
    return ResearchEvidence(
        evidence_id=evidence_id,
        title=repair_text(item.get("title")),
        source_kind=repair_text(item.get("source_kind")),
        citation=citation,
        source_url=repair_text(item.get("source_url")) or _citation_link(citation),
        excerpt=repair_text(bundle.get("text") or item.get("excerpt"))[:3000],
        score=float(item.get("score") or 0),
        query=query,
        round_no=round_no,
        graph_path=item.get("graph_path") if isinstance(item.get("graph_path"), dict) else {},
        metadata=metadata,
    )


def _initial_queries(plan: Any, query: str, limit: int) -> list[str]:
    issue_queries = [repair_text(f"{issue.issue} {issue.legal_domain}") for issue in plan.dispute_issues]
    domains = [issue.legal_domain for issue in plan.dispute_issues]
    authority_queries = []
    if any(domain.startswith("criminal ") and "child pornography" in domain for domain in domains):
        authority_queries.append("Criminal Code 163.1")
    if any(domain.startswith("real estate ") for domain in domains):
        authority_queries.append("real estate sale latent defect seller disclosure")
        translated = translate_query(query).get("translated") or query.lower()
        if "cannabis grow operation" in translated:
            authority_queries.insert(0, "cannabis grow operation real estate sale")
    queries = authority_queries + issue_queries + [repair_text(plan.rule_query), repair_text(plan.case_query)]
    return _unique(queries or [query], limit)


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
        matches: list[ResearchEvidence] = []
        for item in evidence:
            haystack = f"{item.title} {item.citation} {item.excerpt}".casefold()
            hits = {term for term in meaningful_terms if term in haystack}
            if len(hits) >= 2 or bool(hits & strong_terms):
                matches.append(item)
        laws = [item for item in matches if item.source_kind == "law"]
        cases = [item for item in matches if item.source_kind == "case"]
        status = "covered" if laws and cases else "partial" if matches else "missing"
        if not laws:
            gaps.append(f"{issue.issue}：缺少可直接绑定的正式法规或条文")
        if not cases:
            gaps.append(f"{issue.issue}：缺少可比较的裁判案例")
        rows.append({
            "issue": issue.issue,
            "legal_domain": issue.legal_domain,
            "status": status,
            "law_evidence_ids": [item.evidence_id for item in laws[:5]],
            "case_evidence_ids": [item.evidence_id for item in cases[:5]],
        })
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


def _event_state(
    progress: float, current_phase: str, next_action: str, **payload: Any
) -> dict[str, Any]:
    return {
        **payload,
        "progress": max(0.0, min(float(progress), 100.0)),
        "current_phase": current_phase,
        "next_action": next_action,
    }


def _take_control(control_callback: Any | None, pending: list[str]) -> dict[str, Any]:
    control = control_callback() or {} if control_callback else {}
    pending.extend(repair_text(value) for value in (control.get("additional_queries") or []))
    return control


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
    control_callback: Any | None = None,
) -> dict[str, Any]:
    """Run bounded, gap-driven legal research; retrieved text is data, never instructions."""
    clean_query = repair_text(query)
    if not clean_query:
        return {"status": "empty_query", "rounds": [], "evidence": [], "coverage": [], "gaps": []}
    if repair_text(module).lower() != "canada":
        return {
            "status": "unsupported_jurisdiction",
            "jurisdiction": repair_text(module),
            "rounds": [],
            "evidence": [],
            "coverage": [],
            "unresolved_gaps": ["Agentic legal research is restricted to the installed Canadian legal corpus."],
            "review_required": True,
        }

    max_rounds = max(1, min(int(max_rounds or settings.legal_research_max_rounds), 5))
    per_round = max(1, min(int(max_queries_per_round or settings.legal_research_queries_per_round), 10))
    evidence_limit = max(5, min(int(max_evidence or settings.legal_research_max_evidence), 100))
    timeout = max(5, min(int(timeout_seconds or settings.legal_research_timeout_seconds), 300))
    started = time.monotonic()
    plan = build_deep_query_plan(
        clean_query,
        keywords=keywords or [],
        llm_timeout_seconds=min(
            max(1, int(settings.deep_analysis_timeout_seconds)),
            max(1, timeout // 3),
        ),
    )
    research_filters = dict(filters or {})
    if not research_filters and any(
        repair_text(value).lower() == "ontario" for value in (getattr(plan, "jurisdictions", []) or [])
    ):
        research_filters["jurisdiction_hint"] = "Ontario"
    queries = _initial_queries(plan, clean_query, per_round)
    if event_callback:
        event_callback("research_plan", "已完成争点拆分并生成首轮法规与案例查询。", _event_state(
            8, "planning", "start_parallel_retrieval",
            issue_count=len(plan.dispute_issues), queries=queries,
        ))

    evidence_by_id: dict[str, ResearchEvidence] = {}
    rounds: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    gaps: list[str] = []
    stop_reason = "max_rounds_reached"
    pending_refinements: list[str] = []

    for round_no in range(1, max_rounds + 1):
        if control_callback:
            control = _take_control(control_callback, pending_refinements)
            if control.get("cancelled"):
                stop_reason = "cancelled_by_user"
                break
            pause_announced = False
            while control.get("paused") and time.monotonic() - started < timeout:
                if event_callback and not pause_announced:
                    event_callback("research_paused", "研究已暂停，等待人工继续或调整。", _event_state(
                        8 + ((round_no - 1) / max_rounds) * 82,
                        "paused", "resume_or_refine",
                        round_no=round_no,
                    ))
                    pause_announced = True
                time.sleep(0.25)
                control = _take_control(control_callback, pending_refinements)
                if control.get("cancelled"):
                    stop_reason = "cancelled_by_user"
                    break
            if stop_reason == "cancelled_by_user":
                break
            if pending_refinements:
                queries = _unique(pending_refinements + queries, per_round)
                pending_refinements.clear()

        if time.monotonic() - started >= timeout:
            stop_reason = "timeout"
            break

        round_started = time.monotonic()
        executed: list[dict[str, Any]] = []
        new_count = 0
        workers = min(4, len(queries))
        round_progress = 10 + ((round_no - 1) / max_rounds) * 80
        round_progress_end = 10 + (round_no / max_rounds) * 80
        if event_callback:
            event_callback("research_round_started", f"开始第 {round_no} 轮并行检索。", _event_state(
                round_progress, "parallel_retrieval", "wait_for_query_results",
                round_no=round_no, queries=queries,
            ))

        pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="legal-research")
        try:
            futures = {}
            for query_index, search_query in enumerate(queries, start=1):
                if event_callback:
                    event_callback("research_query_started", f"开始并行查询：{search_query}", _event_state(
                        round_progress, "parallel_retrieval", "evaluate_query_result",
                        round_no=round_no, query=search_query,
                        query_index=query_index, query_count=len(queries),
                    ))
                future = pool.submit(
                    hybrid_search,
                    search_query,
                    keywords=keywords or [],
                    module=module,
                    source_filter=source_filter,
                    limit=min(12, evidence_limit),
                    filters=research_filters,
                    include_vector=round_no <= max(0, int(settings.legal_research_vector_rounds)),
                )
                futures[future] = search_query

            remaining = max(0.1, timeout - (time.monotonic() - started))
            done = set()
            completed_count = 0
            try:
                # Yield each query as soon as it completes. This makes the SSE
                # timeline reflect real retrieval progress instead of waiting
                # for the slowest query in the whole round.
                for future in as_completed(futures, timeout=remaining):
                    done.add(future)
                    search_query = futures[future]
                    try:
                        result = future.result()
                        items = result.get("items") or []
                        query_status = repair_text(result.get("status")) or "ok"
                        executed.append({"query": search_query, "status": query_status, "hits": len(items)})
                        completed_count += 1
                        failed = query_status.lower() in {"error", "failed", "timeout"}
                        # 让 SSE 时间线直接可读：附带前几条命中证据的标题与类型。
                        evidence_preview = [
                            {
                                "title": repair_text(item.get("title"))[:160],
                                "kind": repair_text(item.get("source_kind")),
                                "score": round(float(item.get("score") or 0), 4),
                            }
                            for item in items[:4]
                            if repair_text(item.get("title"))
                        ]
                        if event_callback:
                            event_callback(
                                "research_query_failed" if failed else "research_query_completed",
                                f"查询{'失败' if failed else '完成'}：{search_query}",
                                _event_state(
                                    round_progress + (completed_count / max(1, len(queries))) * (
                                        round_progress_end - round_progress
                                    ) * 0.8,
                                    "parallel_retrieval", "evaluate_round_coverage",
                                    round_no=round_no, query=search_query,
                                    status=query_status, hits=len(items),
                                    evidence_preview=evidence_preview,
                                ),
                            )
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
                        clean_error = repair_text(str(exc))[:240]
                        executed.append({"query": search_query, "status": "error", "error": clean_error, "hits": 0})
                        completed_count += 1
                        if event_callback:
                            event_callback("research_query_failed", f"查询失败：{search_query}", _event_state(
                                round_progress + (completed_count / max(1, len(queries))) * (
                                    round_progress_end - round_progress
                                ) * 0.8,
                                "parallel_retrieval", "evaluate_round_coverage",
                                round_no=round_no, query=search_query,
                                status="error", error=clean_error, hits=0,
                            ))
            except FuturesTimeoutError:
                pass
            unfinished = set(futures) - done
            for future in unfinished:
                future.cancel()
                search_query = futures[future]
                executed.append({"query": search_query, "status": "timeout", "hits": 0})
                completed_count += 1
                if event_callback:
                    event_callback("research_query_failed", f"查询超时：{search_query}", _event_state(
                        round_progress + (completed_count / max(1, len(queries))) * (
                            round_progress_end - round_progress
                        ) * 0.8,
                        "parallel_retrieval", "evaluate_round_coverage",
                        round_no=round_no, query=search_query, status="timeout", hits=0,
                    ))
        finally:
            pool.shutdown(wait=False, cancel_futures=True)

        all_ranked = sorted(evidence_by_id.values(), key=lambda item: item.score, reverse=True)
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
        round_result = {
            "round_no": round_no,
            "queries": executed,
            "new_evidence_count": new_count,
            "total_evidence_count": len(ranked),
            "unresolved_gap_count": len(gaps),
            "unresolved_gaps": gaps[:10],
            "next_queries": next_queries,
            "duration_ms": int((time.monotonic() - round_started) * 1000),
        }
        rounds.append(round_result)
        if event_callback:
            event_callback("research_round_completed", f"第 {round_no} 轮检索完成，正在评估证据缺口。", _event_state(
                round_progress_end, "coverage_evaluation",
                "refine_queries" if gaps else "finalize_research",
                **round_result,
            ))

        if control_callback:
            control = _take_control(control_callback, pending_refinements)
            if control.get("cancelled"):
                stop_reason = "cancelled_by_user"
                break
        # A human refinement has priority over the automatic stop rules. It is
        # consumed exactly once and becomes the next round's leading query.
        if pending_refinements and round_no < max_rounds:
            queries = _unique(pending_refinements + next_queries, per_round)
            pending_refinements.clear()
            continue
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
    final_status = "cancelled" if stop_reason == "cancelled_by_user" else (
        "ready" if evidence else "insufficient_evidence"
    )
    if event_callback:
        event_callback(
            "research_cancelled" if final_status == "cancelled" else "research_completed",
            "研究已按用户请求停止。" if final_status == "cancelled" else "多轮检索已完成，正在生成结构化分析。",
            _event_state(
                95, "cancelled" if final_status == "cancelled" else "research_complete",
                "review_partial_result" if final_status == "cancelled" else "generate_structured_analysis",
                status=final_status, stop_reason=stop_reason, evidence_count=len(evidence),
            ),
        )
    return {
        "status": final_status,
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
