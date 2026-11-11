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

_PROVINCE_NAMES = {
    "Alberta": ("阿尔伯塔", "亚伯达"),
    "British Columbia": ("不列颠哥伦比亚", "卑诗"),
    "Manitoba": ("马尼托巴", "曼尼托巴"),
    "New Brunswick": ("新不伦瑞克",),
    "Newfoundland and Labrador": ("纽芬兰和拉布拉多",),
    "Northwest Territories": ("西北地区",),
    "Nova Scotia": ("新斯科舍",),
    "Nunavut": ("努纳武特",),
    "Ontario": ("安大略", "安省"),
    "Prince Edward Island": ("爱德华王子岛",),
    "Quebec": ("魁北克", "Québec"),
    "Saskatchewan": ("萨斯喀彻温",),
    "Yukon": ("育空",),
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

    for province, aliases in _PROVINCE_NAMES.items():
        if re.search(rf"\b{re.escape(province)}\b", clean_query, re.I) or any(
            alias.casefold() in clean_query.casefold() for alias in aliases
        ):
            jurisdictions.append(province)

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
        ("timeline", re.compile(r"(?:19|20)\d{2}年(?:0?[1-9]|1[0-2])月(?:0?[1-9]|[12]\d|3[01])日|(?:19|20)\d{2}[-/]\d{1,2}(?:[-/]\d{1,2})?", re.I), "案情明确提及时间节点：{}", 0.94),
        ("amount", re.compile(r"(?:人民币|加元|美元|CAD|CNY|USD|\$|￥)\s?[\d,]+(?:\.\d{1,2})?(?:元|万元)?", re.I), "案情明确提及金额：{}", 0.94),
        ("party", re.compile(r"原告|被告|申请人|答辩人|房东|出租人|租客|承租人|雇主|雇员|被害人|行为人|landlord|tenant|plaintiff|defendant|applicant|respondent", re.I), "识别到当事人角色：{}", 0.88),
        ("document", re.compile(r"合同|协议|通知书|判决书|发票|收据|邮件|聊天记录|照片|鉴定意见|报告|lease|agreement|notice|invoice|receipt|email|report", re.I), "案情提及可能承担证明作用的材料：{}", 0.86),
        ("relationship", re.compile(r"租赁关系|劳动关系|买卖关系|借贷关系|雇佣关系|侵权关系|landlord.{0,12}tenant|employment relationship", re.I), "识别到法律关系线索：{}", 0.86),
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


_MODEL_DOCUMENT_MENTION = re.compile(
    r"合同|协议|通知|书面|文件|账单|流水|凭证|收据|发票|邮件|聊天记录|照片|报告|"
    r"判决书|鉴定|录音|录像|视频|档案|信件|短信|截图|"
    r"\b(?:contract|agreement|lease|notice|document|bill|statement|receipt|invoice|email|message|"
    r"photo|report|judgment|recording|video|file|record|letter|screenshot)s?\b",
    re.I,
)


def _llm_extract_facts(case_text: str, *, timeout_seconds: int | None = None) -> list[StructuredFact]:
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
            model=settings.deep_analysis_llm_model or None,
            temperature=0.0 if settings.deep_analysis_llm_model else None,
            timeout_seconds=min(
                timeout_seconds if timeout_seconds is not None else settings.deep_analysis_timeout_seconds,
                settings.deep_analysis_timeout_seconds,
            ),
            enable_thinking=False if settings.deep_analysis_llm_non_thinking else None,
        )
        payload = _FactExtractionPayload.model_validate(response.get("data") or {})
    except (LLMServiceError, ValidationError, KeyError, TypeError, ValueError):
        return []
    return [
        fact for fact in payload.facts
        if fact.source_span and fact.source_span in case_text
        and (fact.fact_type != "document" or _MODEL_DOCUMENT_MENTION.search(fact.source_span))
    ]


_TENANCY_PATTERN = r"租客|承租|房东|出租|租房|租赁|\b(?:tenant|tenancy|landlord|lease)s?\b"
_ONTARIO_N12_PATTERN = r"(?<![A-Za-z0-9])N12(?![A-Za-z0-9])"
_EVICTION_PATTERN = r"驱逐|腾退|\beviction\b"
_REAL_ESTATE_PATTERN = r"房产|房屋买卖|购房|卖房|\b(?:real estate|home sale|property purchase)\b"
_DISCLOSURE_PATTERN = r"欺诈|隐瞒|披露|瑕疵|\b(?:fraud|misrepresentation|disclosure|latent defect)\b"
_CHILD_ABUSE_MATERIAL_PATTERN = r"儿童(?:情色|色情|性虐待)|未成年人色情|\bchild (?:pornography|sexual abuse material)\b"

_ISSUE_RULES = [
    (_TENANCY_PATTERN, "租赁合同义务及出租人法定义务是否履行", "tenancy and lease obligations"),
    (r"维修|漏水|霉菌|供暖|适居|\b(?:repairs?|mould|mold|habitability|maintenance)\b", "维修与适居义务违反是否达到救济条件", "housing maintenance and habitability"),
    (_EVICTION_PATTERN, "终止通知、占有返还或驱逐程序是否有效", "eviction and termination procedure"),
    (r"劳动|雇主|雇员|员工|解雇|\b(?:employment|employees?|dismissal|dismissed)\b", "劳动关系终止的正当性及补偿范围如何认定", "employment and dismissal"),
    (r"受伤|损害|过失|事故|\b(?:injury|damages?|negligence|accidents?)\b", "注意义务、因果关系及可赔偿损失是否成立", "tort and negligence"),
    (r"欠租|租金|付款|支付|\b(?:arrears|rent|payments?)\b", "付款义务、欠款范围及抵扣抗辩能否成立", "contract and payment"),
    (r"合同|协议|违约|\b(?:contracts?|agreements?|breach)\b", "合同义务的内容、违反及可归责损失如何认定", "contract and breach"),
]


def _issue_positions(issue_name: str) -> tuple[str, str]:
    if "房产卖方" in issue_name:
        return (
            "逐项核对卖方陈述或披露、对房屋历史的知情情况，以及买方是否因此作出交易决定并产生损失。",
            "卖方可能争议自己是否知情、是否作过相关陈述、历史用途是否构成重大问题，以及买方有无因此受损。",
        )
    if "房产中介" in issue_name:
        return (
            "分开审查中介是否知道相关情况、曾如何回应买方要求，以及其角色和文件约定下有哪些核查或披露事项。",
            "中介可能争议委托范围、实际知情程度、是否有理由发现问题，以及推荐与买方决定之间的联系。",
        )
    if "浏览或访问" in issue_name:
        return (
            "核对访问经过、材料内容和行为人的知情情况，区分主动访问与误触或自动加载。",
            "可核对设备和网络记录能否识别操作者，以及记录是否足以反映有意识的访问。",
        )
    if "保存或持有" in issue_name:
        return (
            "查明文件如何写入、存放位置、行为人是否知情和能否控制文件，区分主动保存与自动缓存。",
            "可核对是否存在共享设备、自动缓存或同步，以及能否可靠认定文件内容及控制者。",
        )
    if "维修" in issue_name or "适居" in issue_name:
        return (
            "若维修通知、房屋缺陷及未处理经过得到材料确认，可主张出租人未履行维修和适居义务并请求相应减租或损失救济。",
            "对方可能争议缺陷严重程度、通知是否实际送达、合理维修机会以及损失与缺陷之间的因果关系。",
        )
    if "N12" in issue_name:
        return (
            "可核验自用事由、通知内容、送达和时间线，并查明哪些材料支持或削弱善意主张。",
            "对方可能主张确有自用需要，并以原始通知和相关材料说明动机与程序。",
        )
    if "驱逐" in issue_name:
        return (
            "可结合适用管辖区、终止事由和送达材料，核验通知及程序是否有效。",
            "对方可能以通知内容、送达记录和程序材料证明其主张。",
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
    rules = _ISSUE_RULES
    if re.search(_REAL_ESTATE_PATTERN, case_text, re.I) and re.search(_DISCLOSURE_PATTERN, case_text, re.I):
        rules = [
            (r"卖方|售房人|\b(?:seller|vendor)\b", "房产卖方是否作出重大失实陈述或应披露而未披露", "real estate seller material misrepresentation disclosure"),
            (r"中介|经纪|\b(?:realtor|broker|real estate agent)\b", "房产中介的陈述、核查与披露是否涉及独立责任", "real estate broker misrepresentation disclosure"),
            *rules,
        ]
    if re.search(_CHILD_ABUSE_MATERIAL_PATTERN, case_text, re.I):
        rules = [
            (r"浏览|观看|访问|\b(?:access|view|browse)\w*\b", "浏览或访问相关材料时的行为及知情情况如何认定", "criminal accessing child pornography"),
            (r"存储|储存|保存|下载|持有|\b(?:possess|sav|download|stor)\w*\b", "保存或持有相关材料时的知情与控制如何认定", "criminal possession child pornography"),
            *rules,
        ]
    if re.search(_ONTARIO_N12_PATTERN, case_text, re.I):
        ontario_domains = {
            "tenancy and lease obligations": "Ontario residential tenancy",
            "housing maintenance and habitability": "Ontario tenancy maintenance and remedies",
        }
        rules = [
            (_ONTARIO_N12_PATTERN, "N12 自用终止通知的事由、善意与程序是否成立", "Ontario N12 eviction and bad faith"),
            *(
                (pattern, issue, ontario_domains.get(domain, domain))
                for pattern, issue, domain in rules
                if pattern != _EVICTION_PATTERN
            ),
        ]
    for pattern, issue_name, domain in rules:
        if not re.search(pattern, case_text, re.I):
            continue
        support = [fact.content for fact in facts if re.search(pattern, f"{fact.content} {fact.source_span}", re.I)][:4]
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
    llm_timeout_seconds: int | None = None,
) -> QueryPlan:
    clean_query = repair_text(query)
    warnings: list[str] = []
    if len(clean_query) > 20000:
        clean_query = clean_query[:20000]
        warnings.append("输入超过 20000 字符，规划阶段仅处理前 20000 字符。")
    legacy = _base_plan_parts(clean_query, keywords)
    if not clean_query:
        return QueryPlan(planning_warnings=["案情为空，无法提取事实或推断争点。"], **legacy)
    if not legacy["jurisdictions"] and not re.search(_ONTARIO_N12_PATTERN, clean_query, re.I):
        warnings.append("案情未明确适用管辖区，请补充相关地区及行为时间，再核对适用规则。")

    llm_facts = _llm_extract_facts(clean_query, timeout_seconds=llm_timeout_seconds) if use_llm else []
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
