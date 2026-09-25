from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.core.config import settings
from app.core.database import engine
from app.service.common_service import repair_text
from app.service.llm_service import LLMServiceError, create_structured_response, is_llm_configured


_GENERIC_TERMS = {
    "canada", "canadian", "ontario", "federal", "provincial", "legal", "law", "laws",
    "case", "cases", "issue", "issues", "dispute", "matter", "problem", "claim", "claims",
}

_JURISDICTION_HINTS = {
    "ontario": ["Ontario"],
    "onltb": ["Ontario", "ONLTB", "Landlord and Tenant Board"],
    "ltb": ["Ontario", "ONLTB", "Landlord and Tenant Board"],
    "canada": ["Canada"],
}

_DOMAIN_HINTS = {
    "tenant": ["residential tenancy", "tenant", "landlord", "lease"],
    "tenancy": ["residential tenancy", "tenant", "landlord", "lease"],
    "landlord": ["residential tenancy", "tenant", "landlord", "lease"],
    "lease": ["residential tenancy", "tenant", "landlord", "lease"],
    "commercial": ["commercial tenancy", "commercial lease"],
    "eviction": ["eviction", "termination", "possession", "Residential Tenancies Act"],
    "repair": ["repair", "maintenance", "habitability", "vital services"],
    "repairs": ["repair", "maintenance", "habitability", "vital services"],
    "habitability": ["repair", "maintenance", "habitability", "vital services"],
    "employment": ["employment", "termination", "dismissal"],
    "contract": ["contract", "breach", "damages"],
    "negligence": ["negligence", "duty of care", "causation", "damages"],
}

_LAW_EXPANSIONS = {
    "residential tenancy": ["Residential Tenancies Act", "landlord obligations", "tenant remedies"],
    "commercial tenancy": ["Commercial Tenancies Act", "Landlord and Tenant Act", "commercial lease"],
    "eviction": ["eviction", "termination order", "notice of termination"],
    "repair": ["repair", "maintenance", "vital services", "reasonable enjoyment"],
}

_CASE_EXPANSIONS = {
    "residential tenancy": ["ONLTB", "Landlord and Tenant Board", "lease and tenancy"],
    "commercial tenancy": ["commercial tenancy", "commercial lease"],
    "eviction": ["eviction ordered", "relief from eviction", "termination for cause"],
    "repair": ["repair", "maintenance", "unit uninhabitable", "vital services"],
}


class StructuredFact(BaseModel):
    fact_type: Literal["party", "timeline", "action", "amount", "document", "claim", "relationship", "location"]
    content: str
    source_span: str = Field(description="原文中对应的片段，用于可追溯")
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class DisputeIssue(BaseModel):
    issue: str = Field(description="法律争议焦点，不是原文短语")
    legal_domain: str
    our_position: str | None = None
    opposing_position: str | None = None
    supporting_facts: list[str] = Field(default_factory=list)
    issue_confidence: float = Field(default=0.5, ge=0.0, le=1.0)


class PriorCandidate(BaseModel):
    candidate_id: str
    candidate_type: Literal["rule", "case"]
    title: str
    matched_feature: str = Field(description="触发该候选的案情特征")
    prior_score: float = Field(default=0.5, ge=0.0, le=1.0)
    source: Literal["relation_history", "domain_mapping", "keyword_match"]


class QueryPlan(BaseModel):
    raw_case_text: str = ""
    structured_facts: list[StructuredFact] = Field(default_factory=list)
    dispute_issues: list[DisputeIssue] = Field(default_factory=list)
    legal_domains: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    rule_query: str = ""
    case_query: str = ""
    filters: dict = Field(default_factory=dict)
    prior_rule_candidates: list[PriorCandidate] = Field(default_factory=list)
    prior_case_candidates: list[PriorCandidate] = Field(default_factory=list)
    planning_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    planning_warnings: list[str] = Field(default_factory=list)

    # Compatibility fields used by hybrid retrieval and reranking.
    original_query: str = ""
    jurisdictions: list[str] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    meaningful_terms: list[str] = Field(default_factory=list)
    law_query: str = ""
    preferred_sources: list[str] = Field(default_factory=lambda: ["law", "case"])


class _FactExtractionPayload(BaseModel):
    facts: list[StructuredFact] = Field(default_factory=list)


def _terms(value: str) -> list[str]:
    cleaned = repair_text(value).lower()
    return re.findall(r"[a-z][a-z0-9'./-]{2,}", cleaned)


def _unique(values: list[str], limit: int = 24) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = repair_text(value)
        key = clean.lower()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= limit:
            break
    return result


def _base_plan_parts(query: str, keywords: list[str] | None = None) -> dict:
    clean_query = repair_text(query)
    keyword_text = " ".join(repair_text(item) for item in (keywords or []))
    all_terms = _terms(" ".join([clean_query, keyword_text]))
    term_set = set(all_terms)
    jurisdictions: list[str] = []
    domains: list[str] = []
    issues: list[str] = []
    law_expansions: list[str] = []
    case_expansions: list[str] = []

    for term in all_terms:
        jurisdictions.extend(_JURISDICTION_HINTS.get(term, []))
        hints = _DOMAIN_HINTS.get(term, [])
        domains.extend(hints)
        if term in {"eviction", "termination", "repair", "repairs", "habitability", "maintenance"}:
            issues.extend(hints or [term])
    for value in domains + issues:
        law_expansions.extend(_LAW_EXPANSIONS.get(value, []))
        case_expansions.extend(_CASE_EXPANSIONS.get(value, []))

    meaningful_terms = [term for term in all_terms if len(term) >= 4 and term not in _GENERIC_TERMS]
    base_terms = _unique([clean_query] + list(keywords or []) + meaningful_terms)
    law_terms = _unique(jurisdictions + domains + issues + law_expansions + base_terms)
    case_terms = _unique(jurisdictions + domains + issues + case_expansions + base_terms)
    preferred_sources = ["case", "law"] if {"tenant", "landlord", "eviction", "repair", "repairs"} & term_set else ["law", "case"]
    if "commercial" in term_set:
        preferred_sources = ["law", "case"]
    return {
        "original_query": clean_query,
        "keywords": _unique(list(keywords or [])),
        "jurisdictions": _unique(jurisdictions),
        "domains": _unique(domains),
        "issues": _unique(issues),
        "meaningful_terms": _unique(meaningful_terms),
        "law_query": " ".join(law_terms or base_terms),
        "case_query": " ".join(case_terms or base_terms),
        "preferred_sources": preferred_sources,
    }


def build_legal_query_plan(query: str, keywords: list[str] | None = None) -> dict:
    """Return the legacy retrieval plan plus empty, backwards-compatible deep fields."""
    legacy = _base_plan_parts(query, keywords)
    plan = QueryPlan(
        raw_case_text=legacy["original_query"],
        rule_query=legacy["law_query"],
        **legacy,
    )
    return plan.model_dump()


def _fact_key(fact: StructuredFact) -> tuple[str, str, str]:
    return fact.fact_type, fact.content.lower(), fact.source_span.lower()


def _append_fact(target: list[StructuredFact], fact_type: str, content: str, source_span: str, confidence: float) -> None:
    span = repair_text(source_span)
    normalized_content = repair_text(content)
    if not span or not normalized_content:
        return
    try:
        fact = StructuredFact(fact_type=fact_type, content=normalized_content, source_span=span, confidence=confidence)
    except ValidationError:
        return
    if _fact_key(fact) not in {_fact_key(item) for item in target}:
        target.append(fact)


def _rule_extract_facts(case_text: str) -> list[StructuredFact]:
    facts: list[StructuredFact] = []
    patterns: list[tuple[str, re.Pattern, str, float]] = [
        ("timeline", re.compile(r"(?:19|20)\d{2}(?:年(?:0?[1-9]|1[0-2])月?(?:(?:0?[1-9]|[12]\d|3[01])日?)?|[-/](?:0?[1-9]|1[0-2])(?:[-/](?:0?[1-9]|[12]\d|3[01]))?)|\d{1,2}月\d{1,2}日", re.I), "案情明确提及时间节点：{}", 0.94),
        ("amount", re.compile(r"(?:人民币|加元|美元|CAD|CNY|USD|\$|￥)\s?[\d,]+(?:\.\d{1,2})?(?:元|万元)?|[\d,]+(?:\.\d{1,2})?\s?(?:元|万元|加元|美元)", re.I), "案情明确提及金额：{}", 0.94),
        ("party", re.compile(r"原告|被告|申请人|答辩人|房东|出租人|租客|承租人|雇主|雇员|员工|landlord|tenant|plaintiff|defendant|applicant|respondent", re.I), "识别到当事人角色：{}", 0.88),
        ("document", re.compile(r"租赁合同|劳动合同|合同|协议|通知书|催告函|发票|收据|邮件|聊天记录|照片|报告|lease|agreement|notice|invoice|receipt|email|report", re.I), "案情提及可能承担证明作用的材料：{}", 0.86),
        ("relationship", re.compile(r"租赁关系|劳动关系|买卖关系|借贷关系|雇佣关系|landlord.{0,12}tenant|employment relationship", re.I), "识别到法律关系线索：{}", 0.86),
        ("location", re.compile(r"安大略省|多伦多|温哥华|加拿大|Ontario|Toronto|Vancouver|Canada", re.I), "案情提及地域连接点：{}", 0.88),
    ]
    for fact_type, pattern, template, confidence in patterns:
        for match in pattern.finditer(case_text):
            _append_fact(facts, fact_type, template.format(match.group(0)), match.group(0), confidence)
            if len(facts) >= 16:
                return facts

    clauses = [repair_text(item) for item in re.split(r"[。！？；;\n]+|(?<=[.!?])\s+", case_text) if repair_text(item)]
    for clause in clauses:
        span = clause[:240]
        lowered = clause.lower()
        if re.search(r"请求|要求|主张|索赔|claim|seek|request", lowered, re.I):
            fact_type = "claim"
            content = f"一方提出的救济或主张涉及：{span[:100]}"
        elif re.search(r"签订|支付|拖欠|拒绝|解除|终止|维修|损坏|通知|解雇|受伤|breach|paid|failed|terminated|repaired|injured", lowered, re.I):
            fact_type = "action"
            content = f"需要依法评价的核心行为是：{span[:100]}"
        else:
            fact_type = "action"
            content = f"案情陈述了一项待核实事件：{span[:100]}"
        _append_fact(facts, fact_type, content, span, 0.68)
        if len(facts) >= 16:
            break
    return facts


def _llm_extract_facts(case_text: str) -> list[StructuredFact]:
    if (
        not case_text
        or int(getattr(settings, "deep_analysis_max_llm_calls", 5)) < 1
        or int(getattr(settings, "deep_analysis_timeout_seconds", 120)) < 1
        or not is_llm_configured()
    ):
        return []
    schema = _FactExtractionPayload.model_json_schema()
    try:
        response = create_structured_response(
            schema_name="deep_case_fact_extraction",
            schema=schema,
            instructions=(
                "Extract only facts explicitly present in the case text. Return 5-16 normalized facts. "
                "Every source_span must be an exact contiguous substring of the supplied text. "
                "Do not infer laws, outcomes, or missing events."
            ),
            user_input=case_text[:12000],
        )
        payload = _FactExtractionPayload.model_validate(response.get("data") or {})
    except (LLMServiceError, ValidationError, KeyError, TypeError, ValueError):
        return []
    return [fact for fact in payload.facts if fact.source_span and fact.source_span in case_text]


_ISSUE_RULES = [
    (r"租客|承租|房东|出租|tenant|landlord|lease", "租赁合同义务及法定出租人义务是否履行", "residential tenancy"),
    (r"欠租|租金|付款|支付|arrears|rent|payment", "付款义务、欠款范围及抵扣抗辩能否成立", "contract and payment"),
    (r"维修|漏水|霉菌|供暖|适居|repair|mould|heat|habitability", "维修与适居义务违反是否达到法定救济条件", "housing standards and remedies"),
    (r"驱逐|腾退|解除|终止|eviction|terminate|termination", "合同终止、占有返还或驱逐程序是否合法有效", "termination and procedure"),
    (r"合同|协议|违约|contract|agreement|breach", "合同义务的内容、违反及可归责损失如何认定", "contract"),
    (r"雇主|员工|解雇|劳动|employment|employee|dismissal", "劳动关系终止的正当性及补偿范围如何认定", "employment"),
    (r"受伤|损害|过失|事故|injury|damage|negligence|accident", "注意义务、因果关系及可赔偿损失是否成立", "tort and negligence"),
]


def _issue_positions(issue_name: str) -> tuple[str, str]:
    if "维修" in issue_name or "适居" in issue_name:
        return (
            "若维修通知、房屋缺陷及未处理经过得到材料确认，可主张出租人未履行维修和适居义务并请求相应减租或损失救济。",
            "对方可能争议缺陷严重程度、通知是否实际送达、合理维修机会以及损失与缺陷之间的因果关系。",
        )
    if "终止" in issue_name or "驱逐" in issue_name:
        return (
            "若终止通知紧随维修投诉且缺少独立终止事由，可主张通知要件、动机或程序有效性需要严格审查。",
            "对方可能主张通知基于独立违约事实，且形式、送达、期限和申请程序均符合法定要求。",
        )
    if "付款" in issue_name or "欠款" in issue_name:
        return (
            "可依据付款凭证和合同约定核对实际已付、应付及可抵扣项目，限制对方扩大欠款范围。",
            "对方可能争议付款用途、抵扣依据、金额计算或主张仍有到期未付义务。",
        )
    if "劳动" in issue_name:
        return (
            "若劳动关系、解除通知和工资记录得到确认，可按解除依据、通知期及补偿义务逐项主张。",
            "对方可能主张存在正当解除原因、劳动关系分类不同或补偿已经支付。",
        )
    if "注意义务" in issue_name or "因果" in issue_name:
        return (
            "可围绕注意义务、违反行为、事实因果和损失凭证建立连续责任链。",
            "对方可能否认可预见性、因果关系或主张受害方自身行为及既存原因降低责任。",
        )
    if "合同" in issue_name:
        return (
            "可先由合同文本确定具体义务，再用履行记录、通知和损失凭证证明违反及救济范围。",
            "对方可能争议条款解释、先履行义务、免责约定、损失可预见性或减损情况。",
        )
    return (
        "主张已识别事实足以触发相应义务、责任或法定救济，但仍需原始材料确认。",
        "对方可能争议义务范围、事实真实性、因果关系或救济条件已经满足。",
    )


def _infer_dispute_issues(case_text: str, facts: list[StructuredFact]) -> list[DisputeIssue]:
    issues: list[DisputeIssue] = []
    fact_contents = [fact.content for fact in facts]
    for pattern, issue_name, domain in _ISSUE_RULES:
        if not re.search(pattern, case_text, re.I):
            continue
        support = [fact.content for fact in facts if re.search(pattern, f"{fact.content} {fact.source_span}", re.I)][:4]
        if not support:
            support = fact_contents[:2]
        our_position, opposing_position = _issue_positions(issue_name)
        issues.append(
            DisputeIssue(
                issue=issue_name,
                legal_domain=domain,
                our_position=our_position,
                opposing_position=opposing_position,
                supporting_facts=support,
                issue_confidence=0.78 if len(support) >= 2 else 0.58,
            )
        )
    fallback_issues = [
        ("关键事实由哪一方承担证明责任以及现有材料能否达到证明标准", "evidence and burden"),
        ("通知、时限、管辖及救济申请是否符合程序要求", "civil procedure"),
    ]
    for issue_name, domain in fallback_issues:
        if len(issues) >= 2:
            break
        issues.append(
            DisputeIssue(
                issue=issue_name,
                legal_domain=domain,
                our_position="主张现有时间线和材料能够满足相应证明或程序要求。",
                opposing_position="对方可能以材料缺失、通知瑕疵、时限或管辖问题提出抗辩。",
                supporting_facts=fact_contents[:3],
                issue_confidence=0.55 if fact_contents else 0.2,
            )
        )
    return issues[:6]


def _feature_terms(case_text: str, keywords: list[str], domains: list[str], issues: list[DisputeIssue]) -> list[str]:
    known = re.findall(
        r"租赁|租客|房东|欠租|租金|维修|驱逐|终止|合同|违约|劳动|解雇|过失|损害|证据|通知|期限|"
        r"tenant|landlord|lease|rent|repair|eviction|termination|contract|breach|employment|dismissal|negligence|evidence",
        case_text,
        re.I,
    )
    issue_terms = [word for issue in issues for word in _terms(f"{issue.issue} {issue.legal_domain}") if len(word) >= 4]
    return _unique(keywords + known + domains + issue_terms, limit=10)


def _authority_terms(feature: str) -> list[str]:
    lowered = repair_text(feature).lower()
    groups = [
        (("tenant", "tenancy", "landlord", "lease", "rent", "租"), ["tenan", "landlord", "lease", "rental"]),
        (("repair", "habitability", "housing", "mould", "维修", "适居"), ["tenan", "housing", "building", "property standards"]),
        (("eviction", "termination", "possession", "驱逐", "终止"), ["tenan", "landlord", "lease"]),
        (("contract", "breach", "payment", "合同", "违约", "付款"), ["contract", "sale of goods", "consumer protection"]),
        (("employment", "dismissal", "employee", "劳动", "解雇"), ["employment", "labour", "labor", "employee"]),
        (("negligence", "tort", "injury", "过失", "损害"), ["negligence", "liability", "insurance", "compensation"]),
        (("evidence", "procedure", "notice", "证据", "程序", "通知"), ["evidence", "procedure", "court rules", "tribunal"]),
    ]
    for markers, terms in groups:
        if any(marker in lowered for marker in markers):
            return terms
    return [lowered] if len(lowered) >= 4 else []


def _authority_term_matches(value: str, terms: list[str]) -> bool:
    lowered = repair_text(value).lower()
    stem_patterns = {
        "tenan": r"\btenan(?:t|ts|cy|cies)\b",
        "contract": r"\bcontract(?:s|ual|ually|ing|ed)?\b",
    }
    for term in terms:
        if " " in term and term in lowered:
            return True
        pattern = stem_patterns.get(term, rf"\b{re.escape(term)}s?\b")
        if re.search(pattern, lowered):
            return True
    return False


def _query_prior_candidates(
    domain_features: list[str],
    keyword_features: list[str],
) -> tuple[list[PriorCandidate], list[PriorCandidate]]:
    features = _unique(domain_features + keyword_features, limit=8)
    if not features:
        return [], []
    rule_candidates: list[PriorCandidate] = []
    case_candidates: list[PriorCandidate] = []
    seen_rules: set[str] = set()
    seen_cases: set[str] = set()

    try:
        with engine.connect() as conn:
            feature_params = {f"feature_{index}": f"%{feature.lower()}%" for index, feature in enumerate(features)}
            feature_clause = " OR ".join(
                f"LOWER(COALESCE(lc.title, '') || ' ' || COALESCE(lc.summary, '') || ' ' || COALESCE(lc.facts, '')) LIKE :{name}"
                for name in feature_params
            )
            relation_rows = conn.execute(
                text(
                    f"""
                    SELECT lr.id AS rule_id, lr.title AS rule_title, lc.id AS case_id,
                           lc.title AS case_title, lc.summary AS case_summary, lc.facts AS case_facts,
                           crr.match_score
                    FROM case_rule_relations crr
                    JOIN legal_cases lc ON lc.id = crr.case_id
                    JOIN legal_rules lr ON lr.id = crr.rule_id
                    WHERE ({feature_clause})
                    ORDER BY crr.match_score DESC
                    LIMIT 60
                    """
                ),
                feature_params,
            ).mappings().all()
            for row in relation_rows:
                case_text = repair_text(f"{row.get('case_title', '')} {row.get('case_summary', '')} {row.get('case_facts', '')}").lower()
                feature = next((item for item in features if item.lower() in case_text), features[0])
                rule_id = str(row.get("rule_id") or "")
                case_id = str(row.get("case_id") or "")
                base_score = min(0.94, 0.72 + float(row.get("match_score") or 0) * 0.22)
                authority_match = _authority_term_matches(row.get("rule_title", ""), _authority_terms(feature))
                if rule_id and rule_id not in seen_rules and authority_match:
                    seen_rules.add(rule_id)
                    rule_candidates.append(PriorCandidate(candidate_id=rule_id, candidate_type="rule", title=repair_text(row.get("rule_title")), matched_feature=feature, prior_score=base_score, source="relation_history"))
                if case_id and case_id not in seen_cases:
                    seen_cases.add(case_id)
                    case_candidates.append(PriorCandidate(candidate_id=case_id, candidate_type="case", title=repair_text(row.get("case_title")), matched_feature=feature, prior_score=base_score, source="relation_history"))

            mapped_features = [(feature, "domain_mapping") for feature in _unique(domain_features, limit=4)] + [
                (feature, "keyword_match") for feature in _unique(keyword_features, limit=4)
            ]
            authority_search_terms = _unique(
                [term for feature, _source in mapped_features for term in _authority_terms(feature)],
                limit=16,
            )
            rule_params = {f"rule_{index}": f"%{term}%" for index, term in enumerate(authority_search_terms)}
            if rule_params:
                rule_clause = " OR ".join(f"LOWER(COALESCE(title, '')) LIKE :{name}" for name in rule_params)
                rule_rows = conn.execute(
                    text(f"SELECT id, title FROM legal_rules WHERE ({rule_clause}) ORDER BY updated_at DESC, id DESC LIMIT 120"),
                    rule_params,
                ).mappings().all()
                for row in rule_rows:
                    matched = next(
                        ((feature, source) for feature, source in mapped_features if _authority_term_matches(row.get("title", ""), _authority_terms(feature))),
                        None,
                    )
                    candidate_id = str(row.get("id") or "")
                    if matched and candidate_id and candidate_id not in seen_rules:
                        feature, source = matched
                        seen_rules.add(candidate_id)
                        rule_candidates.append(PriorCandidate(candidate_id=candidate_id, candidate_type="rule", title=repair_text(row.get("title")), matched_feature=feature, prior_score=0.66 if source == "domain_mapping" else 0.56, source=source))

            if len(case_candidates) < 12:
                case_rows = conn.execute(
                    text(
                        f"""
                        SELECT lc.id, lc.title, lc.summary, lc.facts
                        FROM legal_cases lc
                        WHERE ({feature_clause})
                        ORDER BY lc.court_rank DESC, lc.judgment_date DESC, lc.id DESC
                        LIMIT 50
                        """
                    ),
                    feature_params,
                ).mappings().all()
                domain_keys = {item.lower() for item in domain_features}
                for row in case_rows:
                    candidate_id = str(row.get("id") or "")
                    if not candidate_id or candidate_id in seen_cases:
                        continue
                    case_text = repair_text(f"{row.get('title', '')} {row.get('summary', '')} {row.get('facts', '')}").lower()
                    feature = next((item for item in features if item.lower() in case_text), features[0])
                    source = "domain_mapping" if feature.lower() in domain_keys else "keyword_match"
                    seen_cases.add(candidate_id)
                    case_candidates.append(PriorCandidate(candidate_id=candidate_id, candidate_type="case", title=repair_text(row.get("title")), matched_feature=feature, prior_score=0.62 if source == "domain_mapping" else 0.54, source=source))
    except (SQLAlchemyError, OSError, ValueError):
        return [], []
    return rule_candidates[:12], case_candidates[:12]


def build_deep_query_plan(
    query: str,
    keywords: list[str] | None = None,
    *,
    use_llm: bool = True,
    load_priors: bool = True,
) -> QueryPlan:
    clean_query = repair_text(query)
    warnings: list[str] = []
    if len(clean_query) > 20000:
        clean_query = clean_query[:20000]
        warnings.append("输入超过 20000 字符，规划阶段仅处理前 20000 字符。")
    legacy = _base_plan_parts(clean_query, keywords)
    if not clean_query:
        return QueryPlan(planning_warnings=["案情为空，无法提取事实或推断争点。"], **legacy)

    llm_facts = _llm_extract_facts(clean_query) if use_llm else []
    fallback_facts = _rule_extract_facts(clean_query)
    facts = list(llm_facts)
    existing = {_fact_key(item) for item in facts}
    for fact in fallback_facts:
        if _fact_key(fact) not in existing:
            facts.append(fact)
            existing.add(_fact_key(fact))
        if len(facts) >= 16:
            break
    if not llm_facts:
        warnings.append("模型事实抽取不可用或校验失败，已使用规则化事实抽取。")

    dispute_issues = _infer_dispute_issues(clean_query, facts)
    legal_domains = _unique([issue.legal_domain for issue in dispute_issues] + legacy["domains"], limit=10)
    extracted_keywords = _feature_terms(clean_query, list(keywords or []), legal_domains, dispute_issues)
    prior_rules, prior_cases = (
        _query_prior_candidates(legal_domains, extracted_keywords)
        if load_priors
        else ([], [])
    )
    if load_priors and not prior_rules:
        warnings.append("数据库未返回可追溯的先验法规候选，未生成法规编号。")
    if load_priors and not prior_cases:
        warnings.append("数据库未返回可追溯的先验类案候选。")

    issue_queries = [issue.issue for issue in dispute_issues]
    rule_titles = [item.title for item in prior_rules[:4]]
    case_titles = [item.title for item in prior_cases[:3]]
    rule_query = "；".join(_unique(issue_queries + legal_domains + rule_titles, limit=12)) or clean_query
    case_query = "；".join(_unique(issue_queries + case_titles + legacy["meaningful_terms"], limit=12)) or clean_query
    fact_score = min(len(facts) / 5, 1.0)
    issue_score = min(len(dispute_issues) / 2, 1.0)
    prior_score = min((len(prior_rules) + len(prior_cases)) / 6, 1.0)
    planning_confidence = round(0.4 * fact_score + 0.35 * issue_score + 0.25 * prior_score, 3)

    return QueryPlan(
        raw_case_text=clean_query,
        structured_facts=facts,
        dispute_issues=dispute_issues,
        legal_domains=legal_domains,
        keywords=_unique(list(keywords or []) + extracted_keywords, limit=20),
        rule_query=rule_query,
        case_query=case_query,
        filters={"jurisdictions": legacy["jurisdictions"], "module": "canada"},
        prior_rule_candidates=prior_rules,
        prior_case_candidates=prior_cases,
        planning_confidence=planning_confidence,
        planning_warnings=warnings,
        original_query=clean_query,
        jurisdictions=legacy["jurisdictions"],
        domains=legal_domains,
        issues=[issue.issue for issue in dispute_issues],
        meaningful_terms=_unique(legacy["meaningful_terms"] + extracted_keywords),
        law_query=rule_query,
        preferred_sources=legacy["preferred_sources"],
    )
