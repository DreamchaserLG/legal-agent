from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.legal_data_service import list_case_rule_relations
from app.service.module_service import normalize_module


SKILL_VERSION = "ca-local-1.0"
SKILL_REGISTRY = {
    "case_retrieval_ca": "检索加拿大相关案例，并返回可核验的原文证据。",
    "statute_retrieval_ca": "检索加拿大法规，并返回可核验的原文证据。",
    "issue_fact_extraction": "提取争点、事实、日期、法院和缺失信息，不作法律结论。",
    "authority_linking": "查询案例、法规和已有正式关联，保留关联来源与分数。",
    "evidence_verification": "验证输出仅基于本地检索证据，不生成未被证据支持的主张。",
    "risk_assessment_ca": "生成风险因素与事实缺口的复核草稿，不承诺裁判结果。",
    "research_memo_ca": "生成带证据清单和复核提示的加拿大法律研究备忘录草稿。",
}

_INJECTION_PATTERNS = (
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"system\s+prompt",
    r"reveal\s+(your\s+)?instructions",
    r"绕过.*(限制|规则|审核)",
    r"忽略.*(之前|上述).*(指令|规则)",
)


def ensure_skill_runtime_tables() -> None:
    statements = [
        """
        CREATE TABLE IF NOT EXISTS skill_runs (
            id BIGSERIAL PRIMARY KEY,
            skill_name TEXT NOT NULL,
            skill_version TEXT NOT NULL,
            status TEXT NOT NULL,
            input_text TEXT NOT NULL DEFAULT '',
            filters_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            result_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            duration_ms INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_skill_runs_created ON skill_runs (created_at DESC)",
        "CREATE INDEX IF NOT EXISTS idx_skill_runs_name ON skill_runs (skill_name, created_at DESC)",
    ]
    if is_sqlite():
        statements = [
            """
            CREATE TABLE IF NOT EXISTS skill_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                skill_name TEXT NOT NULL,
                skill_version TEXT NOT NULL,
                status TEXT NOT NULL,
                input_text TEXT NOT NULL DEFAULT '',
                filters_json TEXT NOT NULL DEFAULT '{}',
                evidence_json TEXT NOT NULL DEFAULT '[]',
                result_json TEXT NOT NULL DEFAULT '{}',
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """,
            "CREATE INDEX IF NOT EXISTS idx_skill_runs_created ON skill_runs (created_at DESC)",
            "CREATE INDEX IF NOT EXISTS idx_skill_runs_name ON skill_runs (skill_name, created_at DESC)",
        ]
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))


def list_legal_skills() -> list[dict[str, str]]:
    return [
        {"name": name, "version": SKILL_VERSION, "description": description}
        for name, description in SKILL_REGISTRY.items()
    ]


def _is_prompt_injection(text_value: str) -> bool:
    lowered = repair_text(text_value).lower()
    return any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in _INJECTION_PATTERNS)


def _safe_filters(filters: dict | None) -> dict[str, str]:
    allowed = {"jurisdiction", "document_type", "court_level", "language", "date_from", "date_to"}
    return {
        key: repair_text(value)
        for key, value in (filters or {}).items()
        if key in allowed and repair_text(value)
    }


def _evidence_item(item: dict) -> dict:
    metadata = item.get("metadata") or {}
    return {
        "source_table": repair_text(item.get("source_table")),
        "source_id": item.get("source_id"),
        "chunk_id": item.get("chunk_id"),
        "source_kind": repair_text(item.get("source_kind")),
        "source_code": repair_text(item.get("source_code")),
        "title": repair_text(item.get("title")),
        "source_url": repair_text(item.get("source_url")),
        "excerpt": repair_text(item.get("excerpt")),
        "score": float(item.get("score") or 0),
        "vector_score": float(item.get("vector_score") or 0),
        "lexical_score": float(item.get("lexical_score") or 0),
        "citation": repair_text(metadata.get("citation")),
        "article_no": repair_text(metadata.get("article_no")),
        "published_at": str(item.get("published_at") or ""),
    }


def _retrieve(query: str, source_filter: str, filters: dict, limit: int) -> dict:
    return hybrid_search(
        query,
        module="canada",
        source_filter=source_filter,
        limit=max(1, min(int(limit), 12)),
        filters=filters,
    )


def _extract_intake(query: str) -> dict:
    clean = repair_text(query)
    dates = re.findall(r"\b(?:19|20)\d{2}(?:[-/]\d{1,2}(?:[-/]\d{1,2})?)?\b", clean)
    courts = re.findall(r"\b(?:SCC|ONCA|ONSC|BCCA|BCSC|FCA|FC|QCCA|QCCS|NSCA)\b", clean, flags=re.IGNORECASE)
    issue_terms = re.findall(r"[A-Za-z][A-Za-z0-9'/-]{3,}", clean)
    fact_markers = ("contract", "dismiss", "employment", "injury", "appeal", "discrimination", "criminal", "租赁", "合同", "雇佣", "上诉")
    facts_present = [marker for marker in fact_markers if marker.lower() in clean.lower()]
    missing = []
    if not dates:
        missing.append("关键事件或裁判日期")
    if not courts:
        missing.append("适用法院或管辖区")
    if len(clean) < 80:
        missing.append("足以判断争点的事实细节")
    return {
        "query": clean,
        "issue_terms": issue_terms[:16],
        "dates": sorted(set(dates)),
        "courts": sorted({item.upper() for item in courts}),
        "fact_markers": facts_present,
        "missing_facts": missing,
    }


def _formal_relations(evidence: list[dict]) -> list[dict]:
    results = []
    seen = set()
    for item in evidence:
        if repair_text(item.get("source_table")) != "legal_cases" or not item.get("source_id"):
            continue
        for relation in list_case_rule_relations(case_id=int(item["source_id"]), limit=12):
            key = (relation.get("case_id"), relation.get("rule_id"))
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "case_id": relation.get("case_id"),
                    "case_title": repair_text(relation.get("case_title")),
                    "rule_id": relation.get("rule_id"),
                    "rule_title": repair_text(relation.get("rule_title")),
                    "relation_type": repair_text(relation.get("relation_type")),
                    "match_score": float(relation.get("match_score") or 0),
                    "match_reason": repair_text(relation.get("match_reason")),
                }
            )
    return sorted(results, key=lambda row: row["match_score"], reverse=True)


def _build_result(skill_name: str, query: str, filters: dict, limit: int) -> tuple[str, dict, list[dict]]:
    intake = _extract_intake(query)
    if skill_name == "issue_fact_extraction":
        return "ok", {"intake": intake, "legal_conclusion": None}, []

    if skill_name == "case_retrieval_ca":
        retrieval = _retrieve(query, "case", filters, limit)
        evidence = [_evidence_item(item) for item in retrieval.get("items") or []]
        return "ok", {"retrieval": retrieval, "evidence": evidence}, evidence

    if skill_name == "statute_retrieval_ca":
        retrieval = _retrieve(query, "law", filters, limit)
        evidence = [_evidence_item(item) for item in retrieval.get("items") or []]
        return "ok", {"retrieval": retrieval, "evidence": evidence}, evidence

    if skill_name == "authority_linking":
        cases = _retrieve(query, "case", filters, limit)
        laws = _retrieve(query, "law", filters, limit)
        case_evidence = [_evidence_item(item) for item in cases.get("items") or []]
        law_evidence = [_evidence_item(item) for item in laws.get("items") or []]
        evidence = case_evidence + law_evidence
        return "ok", {
            "case_retrieval": cases,
            "statute_retrieval": laws,
            "formal_relations": _formal_relations(case_evidence),
            "evidence": evidence,
        }, evidence

    if skill_name == "evidence_verification":
        retrieval = _retrieve(query, "canada", filters, limit)
        evidence = [_evidence_item(item) for item in retrieval.get("items") or []]
        return "ok", {
            "evidence": evidence,
            "verdict": "evidence_available" if evidence else "insufficient_evidence",
            "allowed_claims": ["仅可陈述上述证据直接支持的内容。"],
            "blocked_claims": ["不得虚构案例、法条、段落定位或胜诉结论。"],
        }, evidence

    retrieval = _retrieve(query, "canada", filters, limit)
    evidence = [_evidence_item(item) for item in retrieval.get("items") or []]
    risk_factors = [
        "现有证据相关度由本地混合检索排序，必须人工核验原文。",
        "未完成 A2AJ 全量同步前，新导入案例不会出现在正式案例关联中。",
    ]
    if intake["missing_facts"]:
        risk_factors.append("事实不完整会显著降低任何预测或建议的可靠性。")
    risk_payload = {
        "intake": intake,
        "evidence": evidence,
        "risk_factors": risk_factors,
        "countervailing_factors": ["相反先例、适用法版本和程序状态需要由律师逐项确认。"],
        "confidence_band": "low" if len(evidence) < 3 or intake["missing_facts"] else "medium",
        "review_required": True,
        "disclaimer": "这是基于本地证据的研究草稿，不构成法律意见或裁判预测。",
    }
    if skill_name == "risk_assessment_ca":
        return "ok", risk_payload, evidence
    if skill_name == "research_memo_ca":
        return "ok", {
            **risk_payload,
            "memo": {
                "issue": query,
                "facts_to_confirm": intake["missing_facts"],
                "authorities": evidence,
                "next_steps": ["核验每个引用的原文和生效状态。", "由加拿大执业律师复核研究结论。"],
            },
        }, evidence
    raise ValueError(f"Unknown skill: {skill_name}")


def _persist_run(skill_name: str, status: str, query: str, filters: dict, evidence: list[dict], result: dict, duration_ms: int) -> int | None:
    try:
        ensure_skill_runtime_tables()
        with engine.begin() as conn:
            if is_sqlite():
                row = conn.execute(
                    text(
                        """
                        INSERT INTO skill_runs (skill_name, skill_version, status, input_text, filters_json, evidence_json, result_json, duration_ms)
                        VALUES (:skill_name, :skill_version, :status, :input_text, :filters_json, :evidence_json, :result_json, :duration_ms)
                        RETURNING id
                        """
                    ),
                    {
                        "skill_name": skill_name, "skill_version": SKILL_VERSION, "status": status, "input_text": query,
                        "filters_json": json.dumps(filters, ensure_ascii=False, default=str),
                        "evidence_json": json.dumps(evidence, ensure_ascii=False, default=str),
                        "result_json": json.dumps(result, ensure_ascii=False, default=str), "duration_ms": duration_ms,
                    },
                ).mappings().first()
            else:
                row = conn.execute(
                    text(
                        """
                        INSERT INTO skill_runs (skill_name, skill_version, status, input_text, filters_json, evidence_json, result_json, duration_ms)
                        VALUES (:skill_name, :skill_version, :status, :input_text, CAST(:filters_json AS jsonb), CAST(:evidence_json AS jsonb), CAST(:result_json AS jsonb), :duration_ms)
                        RETURNING id
                        """
                    ),
                    {
                        "skill_name": skill_name, "skill_version": SKILL_VERSION, "status": status, "input_text": query,
                        "filters_json": json.dumps(filters, ensure_ascii=False, default=str),
                        "evidence_json": json.dumps(evidence, ensure_ascii=False, default=str),
                        "result_json": json.dumps(result, ensure_ascii=False, default=str), "duration_ms": duration_ms,
                    },
                ).mappings().first()
        return int(row["id"]) if row else None
    except Exception:
        return None


def run_legal_skill(skill_name: str, query: str, *, filters: dict | None = None, limit: int = 8) -> dict:
    clean_skill = repair_text(skill_name)
    clean_query = repair_text(query)
    safe_filters = _safe_filters(filters)
    started = time.perf_counter()
    if clean_skill not in SKILL_REGISTRY:
        raise ValueError(f"Unknown skill: {clean_skill}")
    if not clean_query:
        raise ValueError("query is required")

    if _is_prompt_injection(clean_query):
        result = {
            "skill": clean_skill,
            "status": "blocked",
            "reason": "输入包含可能改变系统行为的指令，未执行检索或外部操作。",
            "review_required": True,
        }
        duration_ms = int((time.perf_counter() - started) * 1000)
        result["run_id"] = _persist_run(clean_skill, "blocked", clean_query, safe_filters, [], result, duration_ms)
        result["duration_ms"] = duration_ms
        return result

    status, payload, evidence = _build_result(clean_skill, clean_query, safe_filters, limit)
    duration_ms = int((time.perf_counter() - started) * 1000)
    result = {
        "skill": clean_skill,
        "skill_version": SKILL_VERSION,
        "status": status,
        "module": normalize_module("canada"),
        "query": clean_query,
        "filters": safe_filters,
        "executed_at": datetime.now(timezone.utc).isoformat(),
        "review_required": clean_skill in {"risk_assessment_ca", "research_memo_ca"},
        "result": payload,
        "duration_ms": duration_ms,
    }
    result["run_id"] = _persist_run(clean_skill, status, clean_query, safe_filters, evidence, result, duration_ms)
    return result
