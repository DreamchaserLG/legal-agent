from __future__ import annotations

import copy
import json
import re
import time

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine, fetch_all
from app.service.bilingual_service import build_bilingual_analysis_pack
from app.service.common_service import repair_text
from app.service.data_quality_service import build_data_readiness
from app.service.deep_analysis_service import build_deep_analysis_payload
from app.service.llm_service import (
    LLMServiceError,
    create_structured_response,
    get_llm_provider,
    is_llm_configured,
)
from app.service.legal_skill_service import enhance_legal_retrieval_keywords
from app.service.module_service import (
    build_module_packet,
    get_module_definition,
    normalize_module,
    resolve_source_for_module,
)
from app.service.rag_service import build_rag_context
from app.service.quality_gate_service import evaluate_quality_gate
from app.service.search_service import search_with_remote_hydration

EN_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "because", "been", "before",
    "by", "can", "could", "did", "do", "does", "for", "from", "had", "has",
    "have", "he", "her", "his", "if", "in", "into", "is", "it", "its", "may",
    "might", "of", "on", "or", "our", "should", "that", "the", "their",
    "them", "they", "this", "to", "was", "were", "what", "when", "where",
    "which", "who", "why", "will", "with", "would", "you", "your",
    "about", "against", "concern", "concerns",
    "event", "events", "fact",
}

LEGAL_CONCEPT_EXPANSIONS = {
    "fraud": {"fraud", "deceit", "dishonesty", "misrepresentation", "fraudulent", "deceive", "defraud", "s. 380", "section 380", "criminal code"},
    "breach": {"breach", "violation", "contravention", "non-compliance", "infringement", "default"},
    "negligence": {"negligence", "carelessness", "duty of care", "reasonable standard", "tort"},
    "contract": {"contract", "agreement", "covenant", "obligation", "consideration", "terms"},
    "property": {"property", "real estate", "land", "title", "ownership", "conveyance", "mortgage"},
    "trust": {"trust", "fiduciary", "trustee", "beneficiary", "estate", "equity"},
    "employment": {"employment", "labour", "labor", "wrongful dismissal", "termination", "severance"},
    "criminal": {"criminal", "crime", "offence", "offense", "prosecution", "sentencing", "conviction"},
    "sanctions": {"sanctions", "ofac", "designated", "blacklist", "embargo", "restricted party"},
    "insurance": {"insurance", "coverage", "claim", "policy", "indemnity", "bad faith"},
    "securities": {"securities", "stock", "share", "investor", "disclosure", "insider trading", "market manipulation"},
    "intellectual_property": {"trademark", "patent", "copyright", "trade secret", "infringement", "ip"},
    "constitutional": {"charter", "constitutional", "rights", "freedom", "section 1", "section 7", "section 15"},
    "immigration": {"immigration", "refugee", "deportation", "visa", "citizenship", "asylum"},
    "family": {"family", "divorce", "custody", "support", "marriage", "spousal", "child"},
    "bankruptcy": {"bankruptcy", "insolvency", "creditor", "debtor", "receivership", "restructuring"},
    "environmental": {"environmental", "pollution", "contamination", "remediation", "emissions"},
    "privacy": {"privacy", "data protection", "personal information", "pipeda", "consent"},
    "consumer": {"consumer", "warranty", "product liability", "unfair practice", "lemon"},
    "tax": {"tax", "taxation", "cra", "assessment", "deduction", "income tax"},
}

LEGAL_CONCEPT_WEIGHTS = {
    "fraud": 1.0, "deceit": 0.95, "dishonesty": 0.9, "misrepresentation": 0.95, "fraudulent": 0.95,
    "breach": 0.9, "violation": 0.85, "contravention": 0.85, "default": 0.8,
    "negligence": 0.9, "carelessness": 0.8, "duty of care": 0.95,
    "contract": 0.7, "agreement": 0.65, "obligation": 0.7,
    "property": 0.6, "real estate": 0.7, "title": 0.6, "ownership": 0.65,
    "trust": 0.8, "fiduciary": 0.9, "trustee": 0.75, "beneficiary": 0.7,
    "employment": 0.7, "wrongful dismissal": 0.9, "termination": 0.7, "severance": 0.7,
    "criminal": 0.85, "offence": 0.8, "prosecution": 0.8, "sentencing": 0.75,
    "sanctions": 0.9, "ofac": 0.95, "designated": 0.7,
    "insurance": 0.7, "coverage": 0.6, "bad faith": 0.85,
    "securities": 0.8, "insider trading": 0.95, "disclosure": 0.7,
    "trademark": 0.8, "patent": 0.8, "copyright": 0.8, "infringement": 0.85,
    "charter": 0.85, "constitutional": 0.85, "rights": 0.7,
    "immigration": 0.8, "refugee": 0.85, "deportation": 0.85,
    "family": 0.6, "divorce": 0.7, "custody": 0.8, "support": 0.6,
    "bankruptcy": 0.8, "insolvency": 0.8, "creditor": 0.65,
    "privacy": 0.75, "data protection": 0.85, "consent": 0.6,
    "consumer": 0.65, "warranty": 0.7, "product liability": 0.85,
    "tax": 0.7, "taxation": 0.7, "assessment": 0.6,
    "company": 0.3, "business": 0.3, "corporation": 0.35, "director": 0.5,
    "court": 0.2, "judge": 0.2, "trial": 0.3, "appeal": 0.4,
    "damage": 0.6, "damages": 0.7, "compensation": 0.7, "loss": 0.5,
    "claim": 0.5, "claims": 0.5, "issue": 0.4, "issues": 0.4,
    "matter": 0.3, "related": 0.2, "case": 0.3,
}

RELIEF_MARKERS = {
    "seek", "seeks", "request", "requests", "ask", "asks", "asked", "demand",
    "demands", "claim", "claims", "claimed", "relief", "compensation", "damages",
    "injunction", "declaration", "buyout", "restitution", "specific performance",
    "请求", "要求", "主张", "申请", "索赔", "赔偿", "返还", "回购", "分割", "确认",
}

ISSUE_MARKERS = {
    "whether", "validity", "breach", "oppression", "inheritance", "liability",
    "ownership", "division", "trust", "fiduciary", "sanction", "privacy",
    "consent", "data", "deletion", "contract", "dismissal", "removal",
    "是否", "效力", "责任", "继承", "侵权", "违约", "所有权", "分割", "信托", "制裁",
    "隐私", "同意", "删除", "除名", "申诉",
}

RETRYABLE_ERROR_CATEGORIES = {"timeout", "network", "invalid_json", "provider_busy"}
_ANALYSIS_CACHE: dict[tuple, tuple[float, dict]] = {}

KEYWORD_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "search_keywords": {"type": "array", "items": {"type": "string"}},
        "case_citations": {"type": "array", "items": {"type": "string"}},
        "statute_references": {"type": "array", "items": {"type": "string"}},
        "legal_concepts": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["search_keywords", "legal_concepts"],
    "additionalProperties": False,
}

ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "facts": {"type": "string"},
        "disputed_issues": {"type": "array", "items": {"type": "string"}},
        "requested_relief": {"type": "string"},
        "search_keywords": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "jurisdiction": {"type": "string"},
        "legal_topics": {"type": "array", "items": {"type": "string"}},
        "claims": {"type": "array", "items": {"type": "string"}},
        "risk_flags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "facts",
        "disputed_issues",
        "requested_relief",
        "search_keywords",
        "summary",
        "jurisdiction",
        "legal_topics",
        "claims",
        "risk_flags",
    ],
    "additionalProperties": False,
}


def _retry_count() -> int:
    return max(1, int(getattr(settings, "llm_retry_count", 2)))


def _retry_backoff_seconds(attempt: int) -> float:
    backoff_ms = max(0, int(getattr(settings, "llm_retry_backoff_ms", 800)))
    return (backoff_ms * attempt) / 1000.0


def _cache_ttl_seconds() -> int:
    return max(0, int(getattr(settings, "cache_ttl_seconds", 300)))


def _get_cached_analysis(cache_key: tuple):
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return None
    cached = _ANALYSIS_CACHE.get(cache_key)
    if not cached:
        return None
    expires_at, payload = cached
    if expires_at <= time.time():
        _ANALYSIS_CACHE.pop(cache_key, None)
        return None
    cloned = copy.deepcopy(payload)
    cloned["analysis_cache_status"] = "hit"
    return cloned


def _set_cached_analysis(cache_key: tuple, payload: dict):
    ttl = _cache_ttl_seconds()
    if ttl <= 0:
        return
    cached_payload = copy.deepcopy(payload)
    cached_payload["analysis_cache_status"] = "miss"
    _ANALYSIS_CACHE[cache_key] = (time.time() + ttl, cached_payload)


def clear_analysis_cache():
    _ANALYSIS_CACHE.clear()


def _safe_excerpt(text: str, limit: int = 140) -> str:
    raw = repair_text(text).replace("\n", " ")
    if len(raw) <= limit:
        return raw
    return raw[:limit] + "..."


def _expand_legal_concepts(keywords: list[str]) -> tuple[list[str], dict[str, float]]:
    expanded = set()
    weights = {}
    for kw in keywords:
        kw_lower = kw.lower().strip()
        expanded.add(kw_lower)
        weights[kw_lower] = LEGAL_CONCEPT_WEIGHTS.get(kw_lower, 0.5)
        for concept_key, synonyms in LEGAL_CONCEPT_EXPANSIONS.items():
            if kw_lower in synonyms or kw_lower == concept_key:
                for syn in synonyms:
                    expanded.add(syn)
                    weights[syn] = LEGAL_CONCEPT_WEIGHTS.get(syn, 0.6)
                weights[concept_key] = LEGAL_CONCEPT_WEIGHTS.get(concept_key, 0.8)
                expanded.add(concept_key)
    return list(expanded), weights


def extract_search_keywords(text: str, max_keywords: int = 8) -> tuple[list[str], dict[str, float]]:
    source_text = repair_text(text)
    if not source_text:
        return [], {}

    english_tokens = re.findall(r"[A-Za-z][A-Za-z'-]{2,}", source_text.lower())
    chinese_tokens = re.findall(r"[\u4e00-\u9fff]{2,}", source_text)

    english_terms = [token for token in english_tokens if token not in EN_STOPWORDS]
    phrase_terms = [
        f"{left} {right}"
        for left, right in zip(english_terms, english_terms[1:])
        if left != right
    ]

    phrase_budget = max(1, max_keywords // 2)
    ordered_terms = phrase_terms[:phrase_budget] + english_terms + chinese_tokens + phrase_terms[phrase_budget:]

    result = []
    seen = set()
    for term in ordered_terms:
        normalized = term.strip().lower()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(term.strip())
        if len(result) >= max_keywords:
            break

    expanded_keywords, weights = _expand_legal_concepts(result)
    return result, weights


def _split_clauses(text: str) -> list[str]:
    chunks = re.split(r"[。！？；;\n]+|(?<=[.!?])\s+", repair_text(text))
    return [chunk.strip(" ,;，；") for chunk in chunks if chunk.strip(" ,;，；")]


def _contains_marker(text: str, markers: set[str]) -> bool:
    raw_text = repair_text(text)
    lowered = raw_text.lower()
    return any(marker in raw_text or marker in lowered for marker in markers)


def _join_clauses(clauses: list[str], limit: int | None = None) -> str:
    values = [repair_text(item) for item in clauses if repair_text(item)]
    if limit:
        values = values[:limit]
    return "; ".join(values)


def _build_fallback_relief(clauses: list[str]) -> str:
    relief_lines = [clause for clause in clauses if _contains_marker(clause, RELIEF_MARKERS)]
    return _join_clauses(relief_lines, limit=2)


def _build_fallback_issues(clauses: list[str], keywords: list[str]) -> list[str]:
    issues = [clause for clause in clauses if _contains_marker(clause, ISSUE_MARKERS)]
    if issues:
        return issues[:4]
    return keywords[:4]


def _normalize_string_list(values, limit: int | None = None) -> list[str]:
    normalized = []
    seen = set()
    for value in values or []:
        text_value = repair_text(value)
        key = text_value.lower()
        if not text_value or key in seen:
            continue
        seen.add(key)
        normalized.append(text_value)
        if limit and len(normalized) >= limit:
            break
    return normalized


def build_intake_outline(analysis: dict) -> dict:
    facts = repair_text(analysis.get("facts") or analysis.get("summary") or "")
    issues = _normalize_string_list(
        analysis.get("disputed_issues") or analysis.get("claims") or analysis.get("legal_topics"),
        limit=6,
    )
    keywords = _normalize_string_list(analysis.get("search_keywords"), limit=8)
    return {
        "facts": facts,
        "disputed_issues": issues,
        "requested_relief": repair_text(analysis.get("requested_relief") or ""),
        "keywords": keywords,
    }


def _apply_retrieval_skills_to_analysis(analysis: dict, source_text: str, module: str) -> dict:
    base_keywords = _normalize_string_list(analysis.get("search_keywords"), limit=12)
    skill_profile = enhance_legal_retrieval_keywords(
        source_text or base_keywords,
        module=module,
        base_keywords=base_keywords,
        keyword_weights=analysis.get("keyword_weights") or {},
        max_keywords=14,
    )
    if skill_profile.get("keywords"):
        analysis["search_keywords"] = skill_profile["keywords"]
        analysis["keyword_weights"] = skill_profile.get("keyword_weights") or {}
    analysis["legal_skill_profile"] = skill_profile
    return skill_profile


def build_local_analysis(text: str, module: str = "canada") -> dict:
    cleaned = repair_text(text)
    normalized_module = normalize_module(module)
    keywords, keyword_weights = extract_search_keywords(cleaned)
    skill_profile = enhance_legal_retrieval_keywords(
        cleaned,
        module=normalized_module,
        base_keywords=keywords,
        keyword_weights=keyword_weights,
        max_keywords=14,
    )
    keywords = skill_profile.get("keywords") or keywords
    keyword_weights = skill_profile.get("keyword_weights") or keyword_weights
    clauses = _split_clauses(cleaned)
    requested_relief = _build_fallback_relief(clauses)

    relief_parts = {part.strip() for part in requested_relief.split(";") if part.strip()}
    fact_clauses = [clause for clause in clauses if clause not in relief_parts]
    facts = _join_clauses(fact_clauses, limit=3) or cleaned[:240]
    disputed_issues = _build_fallback_issues(clauses, keywords)

    jurisdiction = "Canada" if normalized_module == "canada" else "United States / OFAC"
    legal_topics = keywords[:4]
    if normalized_module == "us_sanctions":
        legal_topics = _normalize_string_list(
            keywords[:3] + ["OFAC sanctions", "delisting petition", "specific license"],
            limit=6,
        )
        if "company sanctions" not in {item.lower() for item in legal_topics}:
            legal_topics.insert(0, "company sanctions")
        risk_flags = [
            "先确认命中的到底是真实主体，还是名称相近、控制关系或受益所有人层面的误判。",
            "如果涉及冻结资金、受限交易或疑似协助规避制裁，通常需要把除名与许可证路径并行评估。",
        ]
    else:
        risk_flags = [
            "先区分应优先依赖国家/联邦层面的裁判，还是省级/地方层面的裁判，否则检索顺序会失真。",
        ]

    return {
        "facts": facts or cleaned[:240],
        "disputed_issues": disputed_issues,
        "requested_relief": requested_relief,
        "search_keywords": keywords,
        "keyword_weights": keyword_weights,
        "summary": cleaned[:240],
        "jurisdiction": jurisdiction,
        "legal_topics": legal_topics,
        "claims": disputed_issues[:4],
        "risk_flags": risk_flags,
        "legal_skill_profile": skill_profile,
    }


def _classify_model_error(message: str) -> str:
    lowered = str(message or "").lower()
    if any(token in lowered for token in ["timed out", "timeout", "time out"]):
        return "timeout"
    if any(token in lowered for token in ["connect failed", "receive failed", "connection", "refused", "reset"]):
        return "network"
    if "invalid json" in lowered:
        return "invalid_json"
    if any(token in lowered for token in ["429", "too many requests", "busy", "rate limit"]):
        return "provider_busy"
    if any(token in lowered for token in ["credential", "api key", "api secret", "appid", "not configured"]):
        return "configuration"
    return "provider_error"


def _call_analysis_model(cleaned: str, instructions: str) -> dict:
    attempt_log = []
    last_error = ""
    last_category = ""

    for attempt in range(1, _retry_count() + 1):
        started = time.monotonic()
        try:
            response = create_structured_response(
                schema_name="legal_event_analysis",
                schema=ANALYSIS_SCHEMA,
                instructions=instructions,
                user_input=cleaned,
            )
            duration_ms = int((time.monotonic() - started) * 1000)
            attempt_log.append(
                {
                    "stage": "analysis",
                    "attempt": attempt,
                    "status": "success",
                    "duration_ms": duration_ms,
                    "model": response.get("model", ""),
                    "response_id": response.get("response_id", ""),
                    "error_category": "",
                    "error_message": "",
                }
            )
            return {
                "ok": True,
                "response": response,
                "attempt_log": attempt_log,
                "error_message": "",
                "error_category": "",
            }
        except LLMServiceError as exc:
            duration_ms = int((time.monotonic() - started) * 1000)
            error_message = str(exc).strip()
            error_category = _classify_model_error(error_message)
            retryable = attempt < _retry_count() and error_category in RETRYABLE_ERROR_CATEGORIES
            attempt_log.append(
                {
                    "stage": "analysis",
                    "attempt": attempt,
                    "status": "retrying" if retryable else "failed",
                    "duration_ms": duration_ms,
                    "model": "",
                    "response_id": "",
                    "error_category": error_category,
                    "error_message": error_message,
                }
            )
            last_error = error_message
            last_category = error_category
            if retryable:
                time.sleep(_retry_backoff_seconds(attempt))
                continue
            break

    return {
        "ok": False,
        "response": None,
        "attempt_log": attempt_log,
        "error_message": last_error,
        "error_category": last_category,
    }


def extract_keywords_with_llm(text: str, max_keywords: int = 8) -> tuple[list[str], dict[str, float]]:
    """多Agent协作关键词提取 - 分3个维度提取更精准的关键词"""
    if not is_llm_configured() or not getattr(settings, "keyword_extraction_use_llm", True):
        return extract_search_keywords(text, max_keywords)

    cleaned = repair_text(text)
    if not cleaned or len(cleaned) < 20:
        return extract_search_keywords(text, max_keywords)

    all_keywords = []

    # Agent 1: 法律问题分析 - 提取核心法律概念和争议焦点
    try:
        response1 = create_structured_response(
            schema_name="legal_issue_extraction",
            schema={
                "type": "object",
                "properties": {
                    "legal_issues": {"type": "array", "items": {"type": "string"}},
                    "legal_claims": {"type": "array", "items": {"type": "string"}},
                    "party_roles": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["legal_issues", "legal_claims"],
            },
            instructions=(
                "Analyze this legal case and extract:\n"
                "1. legal_issues: The specific legal issues in dispute (e.g., 'failure to disclose latent defects', 'breach of fiduciary duty')\n"
                "2. legal_claims: The legal claims being made (e.g., 'fraudulent misrepresentation', 'negligent misrepresentation', 'breach of warranty')\n"
                "3. party_roles: The roles of the parties (e.g., 'vendor', 'purchaser', 'real estate agent', 'broker')\n"
                "Be SPECIFIC to this case. Do NOT use generic terms like 'fraud' alone - use 'real estate fraud', 'vendor disclosure fraud', etc.\n"
                "For Chinese input, translate to English legal terminology."
            ),
            user_input=cleaned,
        )
        data1 = response1.get("data", {})
        all_keywords.extend(_normalize_string_list(data1.get("legal_issues"), limit=4))
        all_keywords.extend(_normalize_string_list(data1.get("legal_claims"), limit=4))
        all_keywords.extend(_normalize_string_list(data1.get("party_roles"), limit=3))
    except Exception:
        pass

    # Agent 2: CanLII搜索优化 - 生成针对CanLII的搜索关键词
    try:
        response2 = create_structured_response(
            schema_name="canlii_search_keywords",
            schema={
                "type": "object",
                "properties": {
                    "search_terms": {"type": "array", "items": {"type": "string"}},
                    "canlii_tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["search_terms"],
            },
            instructions=(
                "Generate search keywords optimized for CanLII (Canadian Legal Information Institute) search.\n"
                "Return:\n"
                "1. search_terms: 4-6 English search terms that would find similar cases on CanLII. "
                "Use terms that appear in case headnotes and legal digests.\n"
                "2. canlii_tags: Legal topic tags used in CanLII case digests (e.g., 'Property — Real estate — Disclosure obligations', 'Civil liability — Fraud — Misrepresentation')\n"
                "Example for a real estate fraud case:\n"
                "- search_terms: ['vendor disclosure', 'latent defect', 'misrepresentation property', 'real estate agent duty']\n"
                "- canlii_tags: ['Property — Sale of land — Disclosure', 'Civil liability — Fraud — Non-disclosure']"
            ),
            user_input=cleaned,
        )
        data2 = response2.get("data", {})
        all_keywords.extend(_normalize_string_list(data2.get("search_terms"), limit=6))
        all_keywords.extend(_normalize_string_list(data2.get("canlii_tags"), limit=3))
    except Exception:
        pass

    # Agent 3: 法规引用提取 - 找出相关法规
    try:
        response3 = create_structured_response(
            schema_name="statute_extraction",
            schema={
                "type": "object",
                "properties": {
                    "statutes": {"type": "array", "items": {"type": "string"}},
                    "regulations": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["statutes"],
            },
            instructions=(
                "Identify Canadian statutes and regulations relevant to this case.\n"
                "Return:\n"
                "1. statutes: Full names of relevant Acts (e.g., 'Real Estate and Business Brokers Act', 'Sale of Goods Act', 'Fraudulent Conveyances Act')\n"
                "2. regulations: Relevant regulations (e.g., 'Real Estate Council of Ontario')\n"
                "Be specific. For real estate cases, consider: 'Real Estate and Business Brokers Act', 'Land Titles Act', 'Conveyancing and Law of Property Act', 'Statute of Frauds'.\n"
                "For fraud cases, consider: 'Criminal Code', 'Fraudulent Conveyances Act'."
            ),
            user_input=cleaned,
        )
        data3 = response3.get("data", {})
        all_keywords.extend(_normalize_string_list(data3.get("statutes"), limit=5))
        all_keywords.extend(_normalize_string_list(data3.get("regulations"), limit=3))
    except Exception:
        pass

    # 合并所有关键词
    if not all_keywords:
        return extract_search_keywords(text, max_keywords)

    # 去重并限制数量
    final_keywords = _normalize_string_list(all_keywords, limit=max_keywords + 6)
    _, weights = _expand_legal_concepts(final_keywords)
    return final_keywords, weights


def rerank_search_results_with_llm(query_text: str, results: list[dict], limit: int = 10) -> list[dict]:
    """用LLM重排序搜索结果，筛选最相关的案例"""
    if not is_llm_configured() or len(results) <= limit:
        return results[:limit]

    # 构建结果列表供LLM评估
    result_summaries = []
    for i, r in enumerate(results[:30]):  # 最多评估30条
        result_summaries.append({
            "index": i,
            "title": repair_text(r.get("title", ""))[:80],
            "summary": repair_text(r.get("summary", ""))[:120],
            "source": r.get("source_code", ""),
        })

    try:
        response = create_structured_response(
            schema_name="search_result_reranking",
            schema={
                "type": "object",
                "properties": {
                    "ranked_indices": {
                        "type": "array",
                        "items": {"type": "integer"},
                        "description": "Indices of results sorted by relevance to the query, most relevant first"
                    },
                    "relevance_reasons": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Brief reason for each result's relevance ranking"
                    }
                },
                "required": ["ranked_indices"],
            },
            instructions=(
                "You are a legal research assistant. Given a user's legal question and search results, "
                "rank the results by relevance to the question.\n\n"
                "Consider:\n"
                "1. Does the case involve similar legal issues? (e.g., real estate fraud, disclosure obligations)\n"
                "2. Does the case involve similar parties? (e.g., vendor, purchaser, agent)\n"
                "3. Does the case involve similar facts? (e.g., non-disclosure, misrepresentation)\n"
                "4. Is the case from a relevant jurisdiction?\n\n"
                "Return the indices of results sorted by relevance (most relevant first). "
                "Only include results that are genuinely relevant to the query. "
                "Skip results that are only tangentially related."
            ),
            user_input=f"User query: {query_text}\n\nSearch results:\n" + "\n".join(
                f"[{r['index']}] {r['title']} - {r['summary']}" for r in result_summaries
            ),
        )
        data = response.get("data", {})
        ranked_indices = data.get("ranked_indices", [])

        # 按LLM排序返回结果
        reranked = []
        seen = set()
        for idx in ranked_indices:
            if isinstance(idx, int) and 0 <= idx < len(results) and idx not in seen:
                reranked.append(results[idx])
                seen.add(idx)
            if len(reranked) >= limit:
                break

        # 如果LLM返回的结果不够，补充剩余的
        if len(reranked) < limit:
            for i, r in enumerate(results):
                if i not in seen:
                    reranked.append(r)
                    seen.add(i)
                if len(reranked) >= limit:
                    break

        return reranked
    except Exception:
        return results[:limit]


def _history_similarity_threshold() -> float:
    return 0.3


def _fetch_history_candidates(text_input: str, module_code: str, limit: int = 4) -> list[dict]:
    cleaned = repair_text(text_input)
    if not cleaned:
        return []
    from app.core.database import is_sqlite
    if is_sqlite():
        # SQLite: 不支持 similarity()，用 LIKE 匹配
        sql = """
        SELECT
            id,
            input_text,
            module_code,
            source_filter,
            sort_mode,
            extracted_keywords,
            structured_analysis,
            result_count,
            created_at,
            0 AS similarity_score
        FROM agent_runs
        WHERE module_code = :module_code
          AND (
                LOWER(input_text) = LOWER(:input_text)
             OR LOWER(input_text) LIKE '%' || LOWER(:input_text) || '%'
          )
        ORDER BY
            CASE WHEN LOWER(input_text) = LOWER(:input_text) THEN 1 ELSE 0 END DESC,
            created_at DESC
        LIMIT :limit
        """
    else:
        sql = """
        SELECT
            id,
            input_text,
            module_code,
            source_filter,
            sort_mode,
            extracted_keywords,
            structured_analysis,
            result_count,
            created_at,
            similarity(LOWER(input_text), LOWER(:input_text)) AS similarity_score
        FROM agent_runs
        WHERE module_code = :module_code
          AND (
                LOWER(input_text) = LOWER(:input_text)
             OR similarity(LOWER(input_text), LOWER(:input_text)) >= :threshold
          )
        ORDER BY
            CASE WHEN LOWER(input_text) = LOWER(:input_text) THEN 1 ELSE 0 END DESC,
            similarity_score DESC,
            created_at DESC
        LIMIT :limit
        """
    try:
        return fetch_all(
            sql,
            {
                "input_text": cleaned,
                "module_code": normalize_module(module_code),
                "threshold": _history_similarity_threshold(),
                "limit": max(1, int(limit)),
            },
        )
    except Exception:
        return []


def _build_history_matches(text_input: str, rows: list[dict]) -> list[dict]:
    cleaned = repair_text(text_input).lower()
    matches = []
    for row in rows:
        structured = row.get("structured_analysis") or {}
        analysis = structured.get("analysis") or {}
        intake_outline = structured.get("intake_outline") or {}
        bilingual_context = structured.get("bilingual_context") or {}
        similarity = max(0.0, min(float(row.get("similarity_score") or 0.0), 1.0))
        keywords = _normalize_string_list(
            row.get("extracted_keywords") or intake_outline.get("keywords") or analysis.get("search_keywords"),
            limit=6,
        )
        matches.append(
            {
                "run_id": row.get("id"),
                "module_code": row.get("module_code", "canada"),
                "is_exact": repair_text(row.get("input_text")).lower() == cleaned,
                "similarity": round(similarity, 2),
                "source_filter": row.get("source_filter", "all"),
                "sort_mode": row.get("sort_mode", "relevance"),
                "result_count": int(row.get("result_count") or 0),
                "created_at": str(row.get("created_at") or "")[:19],
                "input_excerpt": _safe_excerpt(row.get("input_text")),
                "summary": _safe_excerpt(
                    bilingual_context.get("summary", {}).get("zh")
                    or analysis.get("summary")
                    or intake_outline.get("facts")
                    or row.get("input_text")
                ),
                "summary_en": _safe_excerpt(
                    bilingual_context.get("summary", {}).get("en")
                    or analysis.get("summary")
                    or intake_outline.get("facts")
                    or row.get("input_text")
                ),
                "keywords": keywords,
            }
        )
    return matches


def _reuse_structured_analysis_from_history(text_input: str, rows: list[dict], module_code: str) -> dict | None:
    cleaned = repair_text(text_input).lower()
    normalized_module = normalize_module(module_code)
    for row in rows:
        if str(row.get("module_code") or "").strip().lower() != normalized_module:
            continue
        if repair_text(row.get("input_text")).lower() != cleaned:
            continue
        structured = row.get("structured_analysis") or {}
        analysis = structured.get("analysis") or {}
        if not analysis:
            continue
        intake_outline = structured.get("intake_outline") or build_intake_outline(analysis)
        bilingual_context = structured.get("bilingual_context") or build_bilingual_analysis_pack(text_input, analysis, normalized_module)
        return {
            "analysis": analysis,
            "intake_outline": intake_outline,
            "bilingual_context": bilingual_context,
            "retrieval_keywords": bilingual_context.get("retrieval_keywords") or intake_outline.get("keywords", []),
            "query_language": bilingual_context.get("query_language", "zh"),
            "analysis_mode": "history_reuse",
            "analysis_error": "",
            "analysis_error_category": "",
            "analysis_attempt_log": [],
            "llm_configured": is_llm_configured(),
            "llm_model": "",
            "llm_response_id": "",
            "history_reused": True,
            "history_match_id": row.get("id"),
            "history_similarity": 1.0,
        }
    return None


def _persist_analysis_run(payload: dict):
    module_packet = payload.get("module_packet", {})

    # 从 module_packet 构建 supporting_case_groups 和 linked_laws
    supporting_case_groups = []
    linked_laws = []

    # 构建 linked_laws 从 relevant_laws
    for law in module_packet.get("relevant_laws", [])[:6]:
        linked_laws.append({
            "rule_id": law.get("rule_id"),
            "title": law.get("title", ""),
            "article_no": law.get("article_no", ""),
            "article_summary": law.get("article_summary", ""),
            "detail_url": law.get("detail_url", ""),
            "source_url": law.get("source_url", ""),
            "legal_type": law.get("legal_type", ""),
            "country": law.get("country", ""),
            "linked_case_count": len(law.get("related_cases", [])),
        })

    # 构建 supporting_case_groups 从 case_law_rows
    for case in module_packet.get("case_law_rows", [])[:5]:
        supporting_case_groups.append({
            "case_id": case.get("case_id"),
            "title": case.get("title", ""),
            "court_level": case.get("court_level", ""),
            "summary": case.get("summary", ""),
            "source_url": case.get("source_url", ""),
            "match_score": case.get("match_score", 0),
            "match_reason": case.get("match_reason", ""),
            "linked_law_titles": [rule.get("title", "") for rule in case.get("rules", [])[:2]],
        })

    structured_analysis = {
        "analysis": payload.get("analysis", {}),
        "intake_outline": payload.get("intake_outline", {}),
        "analysis_mode": payload.get("analysis_mode", ""),
        "analysis_error": payload.get("analysis_error", ""),
        "analysis_error_category": payload.get("analysis_error_category", ""),
        "analysis_attempt_log": payload.get("analysis_attempt_log", []),
        "coverage_note": payload.get("coverage_note", ""),
        "history_matches": payload.get("history_matches", []),
        "module_packet": module_packet,
        "bilingual_context": payload.get("bilingual_context", {}),
        "query_language": payload.get("query_language", "zh"),
        "source_effective": payload.get("source", "all"),
        "supporting_case_groups": supporting_case_groups,
        "supporting_case_rows": module_packet.get("case_law_rows", []) or [],
        "linked_laws": linked_laws,
    }

    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    """
                    INSERT INTO agent_runs (
                        input_text,
                        module_code,
                        source_filter,
                        sort_mode,
                        extracted_keywords,
                        structured_analysis,
                        result_count
                    )
                    VALUES (
                        :input_text,
                        :module_code,
                        :source_filter,
                        :sort_mode,
                        :extracted_keywords,
                        CAST(:structured_analysis AS jsonb),
                        :result_count
                    )
                    """
                ),
                {
                    "input_text": payload.get("input_text", ""),
                    "module_code": payload.get("module_code", "canada"),
                    "source_filter": payload.get("source", "all"),
                    "sort_mode": payload.get("sort", "relevance"),
                    "extracted_keywords": payload.get("extracted_keywords", []),
                    "structured_analysis": json.dumps(structured_analysis, ensure_ascii=False),
                    "result_count": int(payload.get("total") or 0),
                },
            )
    except Exception:
        return


def _postprocess_analysis(analysis: dict, input_text: str) -> dict:
    """后处理: 检测并修复模型只是复制原文的问题"""
    input_lower = input_text.lower().strip()
    input_tokens = set(re.findall(r"[\w一-鿿]+", input_lower))

    # 检测 facts 是否只是复制了原文
    facts = repair_text(analysis.get("facts") or "")
    facts_lower = facts.lower().strip()
    if facts_lower and input_lower:
        facts_tokens = set(re.findall(r"[\w一-鿿]+", facts_lower))
        if facts_tokens and input_tokens:
            overlap = len(facts_tokens & input_tokens) / max(len(facts_tokens), 1)
            if overlap > 0.7:
                # facts 和原文高度重叠，用规则重新生成
                clauses = _split_clauses(input_text)
                relief = _build_fallback_relief(clauses)
                relief_parts = {part.strip() for part in relief.split(";") if part.strip()}
                fact_clauses = [c for c in clauses if c not in relief_parts]
                analysis["facts"] = _join_clauses(fact_clauses, limit=3) or input_text[:240]

    # 检测 summary 是否只是复制了原文
    summary = repair_text(analysis.get("summary") or "")
    if summary.lower().strip() == input_lower[:len(summary)]:
        analysis["summary"] = analysis["facts"][:240]

    # 检测 disputed_issues 是否太泛化
    issues = analysis.get("disputed_issues") or []
    generic_issues = [i for i in issues if len(i) < 10 or i.lower() in {"the facts", "the case", "the law"}]
    if len(generic_issues) == len(issues) and issues:
        analysis["disputed_issues"] = _build_fallback_issues(_split_clauses(input_text), analysis.get("search_keywords", []))

    return analysis


def build_structured_analysis(text: str, module: str = "canada") -> dict:
    cleaned = repair_text(text)
    normalized_module = normalize_module(module)
    fallback = build_local_analysis(cleaned, normalized_module)

    if not cleaned:
        bilingual_context = build_bilingual_analysis_pack(cleaned, fallback, normalized_module)
        return {
            "analysis": fallback,
            "intake_outline": build_intake_outline(fallback),
            "bilingual_context": bilingual_context,
            "retrieval_keywords": bilingual_context.get("retrieval_keywords") or fallback.get("search_keywords", []),
            "legal_skill_profile": fallback.get("legal_skill_profile", {}),
            "query_language": bilingual_context.get("query_language", "zh"),
            "analysis_mode": "empty",
            "analysis_error": "",
            "analysis_error_category": "",
            "analysis_attempt_log": [],
            "llm_configured": is_llm_configured(),
            "llm_model": "",
            "llm_response_id": "",
        }

    if bool(getattr(settings, "analysis_local_fast_mode", True)):
        llm_keywords, llm_weights = ([], {})
        if bool(getattr(settings, "analysis_fast_llm_keyword_enabled", False)):
            llm_keywords, llm_weights = extract_keywords_with_llm(cleaned)
        if llm_keywords and llm_keywords != fallback.get("search_keywords", []):
            fallback["search_keywords"] = llm_keywords
            fallback["keyword_weights"] = llm_weights
            fallback["legal_topics"] = _normalize_string_list(
                llm_keywords[:4] + fallback.get("legal_topics", []), limit=6
            )
            fallback["claims"] = _normalize_string_list(
                llm_keywords[:3] + fallback.get("claims", []), limit=6
            )
        skill_profile = _apply_retrieval_skills_to_analysis(fallback, cleaned, normalized_module)
        bilingual_context = build_bilingual_analysis_pack(cleaned, fallback, normalized_module)
        return {
            "analysis": fallback,
            "intake_outline": build_intake_outline(fallback),
            "bilingual_context": bilingual_context,
            "retrieval_keywords": bilingual_context.get("retrieval_keywords") or fallback.get("search_keywords", []),
            "legal_skill_profile": skill_profile,
            "query_language": bilingual_context.get("query_language", "zh"),
            "analysis_mode": "local_fast",
            "analysis_error": "",
            "analysis_error_category": "",
            "analysis_attempt_log": [],
            "llm_configured": is_llm_configured(),
            "llm_model": "local",
            "llm_response_id": "",
        }

    if not is_llm_configured():
        skill_profile = _apply_retrieval_skills_to_analysis(fallback, cleaned, normalized_module)
        bilingual_context = build_bilingual_analysis_pack(cleaned, fallback, normalized_module)
        return {
            "analysis": fallback,
            "intake_outline": build_intake_outline(fallback),
            "bilingual_context": bilingual_context,
            "retrieval_keywords": bilingual_context.get("retrieval_keywords") or fallback.get("search_keywords", []),
            "legal_skill_profile": skill_profile,
            "query_language": bilingual_context.get("query_language", "zh"),
            "analysis_mode": "heuristic",
            "analysis_error": f"LLM credentials are not configured for provider={get_llm_provider()}.",
            "analysis_error_category": "configuration",
            "analysis_attempt_log": [],
            "llm_configured": False,
            "llm_model": "",
            "llm_response_id": "",
        }

    if normalized_module == "us_sanctions":
        instructions = (
            "You are assisting with a U.S. sanctions research module focused only on companies that are sanctioned, "
            "their possible delisting path, the reasons for designation, and the procedures for reconsideration or licensing. "
            "Split the user's event into facts, disputed issues, requested relief, and 4 to 8 search keywords. "
            "Also provide a concise summary, likely jurisdiction, legal topics, claims, and risk flags. "
            "Keep close to the user's facts. Do not invent cases, statutes, or outcomes."
        )
    else:
        instructions = (
            "Analyze this Canadian legal event. Return JSON with:\n"
            "1. facts: Key facts in 2-3 sentences (rewrite, don't copy)\n"
            "2. disputed_issues: 2-4 specific legal issues\n"
            "3. requested_relief: What remedy is sought\n"
            "4. search_keywords: 4-8 English legal terms for Canadian law\n"
            "5. summary: 1-2 sentence legal summary\n"
            "6. jurisdiction: Canadian jurisdiction (Ontario/Federal/etc)\n"
            "7. legal_topics: Areas of law involved\n"
            "8. claims: Specific legal claims\n"
            "9. risk_flags: 2-3 risk factors\n"
            "For Chinese input, translate legal concepts to English (欺诈→fraud, 房产→property)."
        )

    model_call = _call_analysis_model(cleaned, instructions)
    if not model_call["ok"]:
        skill_profile = _apply_retrieval_skills_to_analysis(fallback, cleaned, normalized_module)
        bilingual_context = build_bilingual_analysis_pack(cleaned, fallback, normalized_module)
        return {
            "analysis": fallback,
            "intake_outline": build_intake_outline(fallback),
            "bilingual_context": bilingual_context,
            "retrieval_keywords": bilingual_context.get("retrieval_keywords") or fallback.get("search_keywords", []),
            "legal_skill_profile": skill_profile,
            "query_language": bilingual_context.get("query_language", "zh"),
            "analysis_mode": "heuristic_fallback",
            "analysis_error": model_call["error_message"],
            "analysis_error_category": model_call["error_category"],
            "analysis_attempt_log": model_call["attempt_log"],
            "llm_configured": True,
            "llm_model": "",
            "llm_response_id": "",
        }

    response = model_call["response"]
    analysis = dict(response["data"])
    analysis["search_keywords"] = _normalize_string_list(analysis.get("search_keywords"), limit=8) or fallback["search_keywords"]
    analysis["disputed_issues"] = _normalize_string_list(analysis.get("disputed_issues"), limit=6) or fallback["disputed_issues"]
    analysis["legal_topics"] = _normalize_string_list(analysis.get("legal_topics"), limit=6) or fallback["legal_topics"]
    analysis["claims"] = _normalize_string_list(analysis.get("claims"), limit=6) or analysis["disputed_issues"]
    analysis["risk_flags"] = _normalize_string_list(analysis.get("risk_flags"), limit=6) or fallback["risk_flags"]
    analysis["facts"] = repair_text(analysis.get("facts") or analysis.get("summary") or fallback["facts"])
    analysis["requested_relief"] = repair_text(analysis.get("requested_relief") or fallback["requested_relief"])
    analysis["summary"] = repair_text(analysis.get("summary") or analysis["facts"][:240])
    analysis["jurisdiction"] = repair_text(analysis.get("jurisdiction") or fallback["jurisdiction"])

    # 后处理: 检测模型是否只是复制了原文
    analysis = _postprocess_analysis(analysis, cleaned)

    _, keyword_weights = _expand_legal_concepts(analysis["search_keywords"])
    analysis["keyword_weights"] = keyword_weights
    skill_profile = _apply_retrieval_skills_to_analysis(analysis, cleaned, normalized_module)

    bilingual_context = build_bilingual_analysis_pack(cleaned, analysis, normalized_module)
    return {
        "analysis": analysis,
        "intake_outline": build_intake_outline(analysis),
        "bilingual_context": bilingual_context,
        "retrieval_keywords": bilingual_context.get("retrieval_keywords") or analysis.get("search_keywords", []),
        "legal_skill_profile": skill_profile,
        "query_language": bilingual_context.get("query_language", "zh"),
        "analysis_mode": "model",
        "analysis_error": "",
        "analysis_error_category": "",
        "analysis_attempt_log": model_call["attempt_log"],
        "llm_configured": True,
        "llm_model": response.get("model", ""),
        "llm_response_id": response.get("response_id", ""),
    }


def _build_retrieval_summary(module_packet: dict, keywords: list[str]) -> dict:
    packet = module_packet or {}
    laws = packet.get("relevant_laws") or []
    case_rows = packet.get("case_law_rows") or []
    grouped_laws = [
        law
        for law in laws
        if (law.get("case_columns") or law.get("related_cases") or law.get("linked_case_count"))
    ]
    return {
        "strategy": "local_keyword_relation",
        "keywords": _normalize_string_list(keywords, limit=10),
        "law_count": len(laws),
        "grouped_law_count": len(grouped_laws),
        "case_count": len(case_rows),
    }


def _dynamic_new_case_target_count(limit: int) -> int:
    normalized_limit = max(1, int(limit or 1))
    configured = int(getattr(settings, "analysis_new_case_target_count", 0) or 0)
    if configured > 0:
        return max(normalized_limit, configured)
    buffer = max(4, min(12, normalized_limit))
    hard_cap = max(normalized_limit, int(getattr(settings, "remote_search_max_items_per_source", 12)) * 2)
    return min(hard_cap, normalized_limit + buffer)


def enrich_with_deep_analysis(
    payload: dict,
    *,
    audit: bool = True,
    tenant_id: str = "",
    user_id: int | None = None,
) -> dict:
    if not bool(getattr(settings, "deep_analysis_enabled", False)) or payload.get("deep_analysis"):
        return payload
    try:
        plan, deep_analysis = build_deep_analysis_payload(
            repair_text(payload.get("input_text")),
            keywords=payload.get("retrieval_keywords") or payload.get("extracted_keywords") or [],
            module_packet=payload.get("module_packet") or {},
            retrieval_items=(payload.get("rag_context") or {}).get("items") or payload.get("results") or [],
        )
        quality_gate = evaluate_quality_gate(
            deep_analysis,
            input_summary=deep_analysis.case_summary,
            tenant_id=tenant_id,
            user_id=user_id,
            audit=audit,
        )
        payload["deep_query_plan"] = plan.model_dump()
        payload["deep_analysis"] = deep_analysis.model_dump()
        payload["quality_gate"] = quality_gate.model_dump()
        payload["deep_analysis_status"] = "ready" if quality_gate.passed else "downgraded"
    except Exception as exc:
        payload["deep_analysis_status"] = "failed"
        payload["deep_analysis_error"] = repair_text(str(exc))[:400]
    return payload


def analyze_sentence_search(
    text: str,
    limit: int = 30,
    offset: int = 0,
    source: str = "all",
    sort: str = "relevance",
    module: str = "canada",
    refresh: bool = False,
    origin_page: str = "analyze",
    local_only: bool = True,
    tenant_id: str = "",
    user_id: int | None = None,
):
    normalized_module = normalize_module(module)
    cleaned_text = repair_text(text)
    effective_source = resolve_source_for_module(normalized_module, source)
    cache_key = ("analyze-structured", normalized_module, cleaned_text.lower())
    analysis_cache_status = "miss"
    structured = None
    if not refresh:
        structured = _get_cached_analysis(cache_key)
        if structured is not None:
            analysis_cache_status = "hit"

    history_rows = _fetch_history_candidates(cleaned_text, normalized_module)
    history_matches = _build_history_matches(cleaned_text, history_rows)
    has_exact_history = any(
        repair_text(row.get("input_text")).lower() == cleaned_text.lower()
        and str(row.get("module_code") or "").strip().lower() == normalized_module
        for row in history_rows
    )
    is_new_case = not has_exact_history
    new_case_hydration_enabled = bool(getattr(settings, "analysis_new_case_hydration_enabled", True)) and not local_only
    new_case_target_count = _dynamic_new_case_target_count(limit)

    if structured is None:
        structured = (
            _reuse_structured_analysis_from_history(cleaned_text, history_rows, normalized_module)
            or build_structured_analysis(cleaned_text, normalized_module)
        )
        _set_cached_analysis(cache_key, structured)

    analysis = structured["analysis"]
    intake_outline = structured["intake_outline"]
    bilingual_context = structured.get("bilingual_context", {})
    query_language = structured.get("query_language", "zh")
    extracted_keywords = intake_outline.get("keywords", [])
    retrieval_keywords = structured.get("retrieval_keywords") or extracted_keywords

    if local_only and bool(getattr(settings, "analysis_local_fast_mode", True)):
        keyword_weights = analysis.get("keyword_weights") or {}
        from app.service.search_service import search_with_remote_hydration

        result = search_with_remote_hydration(
            cleaned_text,
            limit=limit,
            offset=offset,
            source=effective_source,
            sort=sort,
            module=normalized_module,
            refresh=refresh,
            origin_page=origin_page,
            display_language=query_language,
            local_only=True,
            keyword_weights=keyword_weights,
        )
        result["search_strategy"] = "local_fast_rag"
        result["canlii_realtime"] = {"method": "disabled", "count": 0}
    else:
        keyword_weights = analysis.get("keyword_weights") or {}
        use_realtime_search = (
            getattr(settings, "canlii_realtime_search_enabled", True)
            and normalized_module in ("canada", "all")
            and effective_source in ("all", "canlii", "canada")
        )
        if use_realtime_search:
            from app.service.search_service import search_with_canlii_realtime
            result = search_with_canlii_realtime(
                retrieval_keywords,
                limit=limit,
                offset=offset,
                source=effective_source,
                sort=sort,
                module=normalized_module,
                refresh=True,
                display_language=query_language,
                keyword_weights=keyword_weights,
            )
            # 用LLM重排序搜索结果，筛选最相关的案例（暂时禁用，避免超时）
            # if is_llm_configured() and result.get("results"):
            #     reranked = rerank_search_results_with_llm(cleaned_text, result["results"], limit=limit)
            #     result["results"] = reranked
            #     # 重建grouped_results
            #     from app.service.search_service import _prepare_cards
            #     result["grouped_results"] = _prepare_cards(reranked, query_language)
        else:
            result = search_with_remote_hydration(
                retrieval_keywords,
                limit=limit,
                offset=offset,
                source=effective_source,
                sort=sort,
                module=normalized_module,
                refresh=True,
                origin_page=origin_page,
                force_hydration=is_new_case and new_case_hydration_enabled,
                hydration_target_count=new_case_target_count if is_new_case and new_case_hydration_enabled else limit,
                hydration_reason="new_case_enrichment" if is_new_case and new_case_hydration_enabled else "",
                display_language=query_language,
                local_only=local_only,
                keyword_weights=keyword_weights,
            )

    coverage_note = ""
    if not result["total"]:
        if local_only:
            coverage_note = "当前分析仅基于本地数据库中的案例、法律法规和既有关联关系完成，不会实时抓取远程网页。"
        elif normalized_module == "us_sanctions":
            coverage_note = "当前已经完成关键词提取并检索 OFAC 本地材料，但暂时没有命中对应的公司记录或程序材料。"
        else:
            coverage_note = "当前已经根据提取出的关键词完成检索，但本地加拿大案例材料里暂时没有命中结果。"

    response_payload = {
        "input_text": cleaned_text,
        "module_code": normalized_module,
        "module_profile": get_module_definition(normalized_module),
        "query_language": query_language,
        "source": effective_source,
        "analysis": analysis,
        "intake_outline": intake_outline,
        "bilingual_context": bilingual_context,
        "analysis_mode": structured.get("analysis_mode", "heuristic"),
        "analysis_error": structured.get("analysis_error", ""),
        "analysis_error_category": structured.get("analysis_error_category", ""),
        "analysis_attempt_log": structured.get("analysis_attempt_log", []),
        "llm_configured": structured.get("llm_configured", False),
        "llm_model": structured.get("llm_model", ""),
        "llm_response_id": structured.get("llm_response_id", ""),
        "history_reused": structured.get("history_reused", False),
        "history_match_id": structured.get("history_match_id"),
        "history_similarity": structured.get("history_similarity", 0),
        "history_matches": history_matches,
        "is_new_case": is_new_case,
        "new_case_hydration_enabled": is_new_case and new_case_hydration_enabled,
        "new_case_hydration_target_count": new_case_target_count if is_new_case and new_case_hydration_enabled else 0,
        "extracted_keywords": extracted_keywords,
        "retrieval_keywords": retrieval_keywords,
        "legal_skill_profile": structured.get("legal_skill_profile") or analysis.get("legal_skill_profile") or result.get("legal_skill_profile", {}),
        "coverage_note": coverage_note,
        "analysis_cache_status": analysis_cache_status,
        **result,
    }
    fast_module_packet = result.get("module_packet") if result.get("search_backend") == "rag_chunks_tsvector" else None
    response_payload["module_packet"] = fast_module_packet or build_module_packet(
        normalized_module,
        response_payload,
        refresh=refresh,
    )
    response_payload["retrieval_summary"] = _build_retrieval_summary(
        response_payload["module_packet"],
        retrieval_keywords,
    )
    if result.get("search_backend") == "rag_chunks_tsvector":
        response_payload["rag_context"] = {
            "enabled": True,
            "status": "reused_fast_search",
            "items": result.get("results", [])[: max(1, min(int(limit or 8), 12))],
            "total": len(result.get("results", [])),
        }
    else:
        response_payload["rag_context"] = build_rag_context(
            cleaned_text,
            keywords=retrieval_keywords,
            module=normalized_module,
        )
    response_payload["data_readiness"] = build_data_readiness(
        module=normalized_module,
        module_packet=response_payload["module_packet"],
        retrieval_summary=response_payload["retrieval_summary"],
        rag_context=response_payload["rag_context"],
        local_result_total=int(response_payload.get("total") or 0),
    )

    # 从 module_packet 构建 supporting_case_groups 和 linked_laws
    module_packet = response_payload["module_packet"]
    supporting_case_groups = []
    linked_laws = []

    # 构建 linked_laws 从 relevant_laws
    for law in module_packet.get("relevant_laws", [])[:6]:
        linked_laws.append({
            "rule_id": law.get("rule_id"),
            "title": law.get("title", ""),
            "article_no": law.get("article_no", ""),
            "article_summary": law.get("article_summary", ""),
            "detail_url": law.get("detail_url", ""),
            "source_url": law.get("source_url", ""),
            "legal_type": law.get("legal_type", ""),
            "country": law.get("country", ""),
            "linked_case_count": len(law.get("related_cases", [])),
        })

    # 构建 supporting_case_groups 从 case_law_rows
    for case in module_packet.get("case_law_rows", [])[:5]:
        supporting_case_groups.append({
            "case_id": case.get("case_id"),
            "title": case.get("title", ""),
            "court_level": case.get("court_level", ""),
            "summary": case.get("summary", ""),
            "source_url": case.get("source_url", ""),
            "match_score": case.get("match_score", 0),
            "match_reason": case.get("match_reason", ""),
            "linked_law_titles": [rule.get("title", "") for rule in case.get("rules", [])[:2]],
        })

    response_payload["supporting_case_groups"] = supporting_case_groups
    response_payload["supporting_case_rows"] = module_packet.get("case_law_rows", []) or []
    response_payload["linked_laws"] = linked_laws
    if response_payload["data_readiness"].get("status") != "ready":
        readiness_note = "；".join(response_payload["data_readiness"].get("warnings") or [])
        if readiness_note:
            response_payload["coverage_note"] = (
                f"{response_payload.get('coverage_note') or ''} {readiness_note}"
            ).strip()

    enrich_with_deep_analysis(response_payload, tenant_id=tenant_id, user_id=user_id)

    if (
        analysis_cache_status != "hit"
        and not structured.get("history_reused")
        and structured.get("analysis_mode") != "heuristic_fallback"
    ):
        _persist_analysis_run(response_payload)
    return response_payload
