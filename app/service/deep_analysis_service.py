from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, Field

from app.core.config import settings
from app.service.common_service import repair_text
from app.service.legal_query_planner_service import DisputeIssue, QueryPlan, StructuredFact, build_deep_query_plan


DISCLAIMER = "该分析基于当前输入和已检索资料生成，仅用于法律研究分流；需由具备管辖区资质的专业人员复核，不构成法律意见。"


class RiskPrediction(BaseModel):
    risk_type: Literal["substantive", "procedural", "evidence", "enforcement"]
    description: str
    trigger_condition: str = Field(description="什么条件下该风险会触发")
    potential_consequence: str
    supporting_rule_ids: list[str] = Field(default_factory=list)
    supporting_case_ids: list[str] = Field(default_factory=list)
    evidence_gap: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    manual_review_required: bool = False


class DisputeDetail(BaseModel):
    issue: str
    our_position: str
    opposing_position: str
    legal_basis: list[dict] = Field(default_factory=list)
    case_support: list[dict] = Field(default_factory=list)
    strength_score: float = Field(ge=0.0, le=1.0)
    strength_breakdown: dict = Field(default_factory=dict)
    unresolved_gaps: list[str] = Field(default_factory=list)


class CaseStrengthScore(BaseModel):
    evidence_completeness: float = Field(ge=0.0, le=1.0)
    legal_basis_sufficiency: float = Field(ge=0.0, le=1.0)
    case_support: float = Field(ge=0.0, le=1.0)
    procedural_compliance: float = Field(ge=0.0, le=1.0)
    opposing_strength: float = Field(ge=0.0, le=1.0)
    overall: float = Field(ge=0.0, le=1.0)
    breakdown: dict = Field(default_factory=dict)
    disclaimer: str = "该评分为模拟状态指标，未经校准，不构成法律意见"


class EvidenceGap(BaseModel):
    gap_type: Literal["fact", "document", "witness", "expert", "timeline"]
    description: str
    why_needed: str
    what_changes_if_filled: str
    priority: Literal["high", "medium", "low"]


class MissingMaterial(BaseModel):
    material_type: str
    description: str
    related_issue: str
    acquisition_hint: str


class DeepAnalysis(BaseModel):
    case_summary: str = Field(description="结构化案情摘要，不是原文复制")
    dispute_details: list[DisputeDetail] = Field(default_factory=list)
    risk_predictions: list[RiskPrediction] = Field(default_factory=list)
    case_strength: CaseStrengthScore
    evidence_gaps: list[EvidenceGap] = Field(default_factory=list)
    missing_materials: list[MissingMaterial] = Field(default_factory=list)
    confidence_boundary: dict = Field(default_factory=dict)
    manual_review_items: list[str] = Field(default_factory=list)
    references: list[dict] = Field(default_factory=list)
    disclaimer: str = DISCLAIMER


def _clamp(value: float) -> float:
    return round(max(0.0, min(float(value), 1.0)), 3)


def _fact_values(facts: list[StructuredFact], fact_type: str, limit: int = 4) -> list[str]:
    return [fact.content for fact in facts if fact.fact_type == fact_type][:limit]


def _structured_summary(plan: QueryPlan) -> str:
    facts = plan.structured_facts
    parties = _fact_values(facts, "party")
    timelines = _fact_values(facts, "timeline")
    relationships = _fact_values(facts, "relationship")
    actions = _fact_values(facts, "action", limit=5)
    claims = _fact_values(facts, "claim")
    locations = _fact_values(facts, "location")
    documents = _fact_values(facts, "document")
    sections = [
        f"当事人与关系：{'；'.join(parties + relationships) if parties or relationships else '现有输入未能稳定确认完整身份及法律关系'}。",
        f"时间线：{'；'.join(timelines) if timelines else '尚缺可核验的关键日期、通知时间和履行节点'}。",
        f"核心行为：{'；'.join(actions) if actions else '现有输入仅能确认存在争议，具体履行或违约行为仍待核实'}。",
        f"诉求与地域：{'；'.join(claims + locations) if claims or locations else '救济请求、管辖连接点及金额范围尚未完整说明'}。",
        f"现有材料：{'；'.join(documents) if documents else '尚未识别出可直接核验的合同、通知、付款或沟通材料'}。",
    ]
    summary = "".join(sections)
    if len(summary) > 400:
        summary = summary[:397].rstrip("；，。") + "。"
    return summary


def _normalized_rule(law: dict) -> dict | None:
    rule_id = law.get("rule_id") or law.get("law_id")
    title = repair_text(law.get("title"))
    relation_status = repair_text(law.get("relation_status")).lower()
    has_formal_relation = bool(
        int(law.get("linked_case_count") or 0) > 0
        or law.get("related_cases")
        or relation_status in {"verified_relation", "formal_relation", "relation_verified"}
    )
    if not rule_id or not title or repair_text(law.get("origin")).lower() == "fallback" or not has_formal_relation:
        return None
    if relation_status == "retrieved_pending_relation":
        return None
    status = repair_text(law.get("status") or law.get("effectiveness_status") or "relation_verified")
    return {
        "rule_id": str(rule_id),
        "title": title,
        "article_no": repair_text(law.get("article_no") or law.get("citation")),
        "excerpt": repair_text(law.get("article_summary") or law.get("reason") or law.get("evidence_excerpt")),
        "citation_status": "verified_relation",
        "effectiveness_status": status,
        "source_url": repair_text(law.get("source_url") or law.get("detail_url")),
    }


def _normalized_case(case: dict) -> dict | None:
    case_id = case.get("case_id") or case.get("id")
    title = repair_text(case.get("title"))
    if not case_id or not title:
        return None
    return {
        "case_id": str(case_id),
        "title": title,
        "court": repair_text(case.get("court_name") or case.get("court_level")),
        "date": repair_text(case.get("judgment_date") or case.get("published_at")),
        "excerpt": repair_text(case.get("summary") or case.get("facts") or case.get("excerpt"))[:600],
        "source_url": repair_text(case.get("source_url") or case.get("url")),
        "relation_status": repair_text(case.get("relation_status") or "retrieved_case"),
    }


def _authority_sets(module_packet: dict, retrieval_items: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    rules: list[dict] = []
    cases: list[dict] = []
    seen_rules: set[str] = set()
    seen_cases: set[str] = set()
    for law in module_packet.get("relevant_laws") or []:
        normalized = _normalized_rule(law)
        if normalized and normalized["rule_id"] not in seen_rules:
            seen_rules.add(normalized["rule_id"])
            rules.append(normalized)
    for case in list(module_packet.get("case_law_rows") or []) + list(retrieval_items or []):
        if repair_text(case.get("source_kind")) == "law":
            continue
        normalized = _normalized_case(case)
        if normalized and normalized["case_id"] not in seen_cases:
            seen_cases.add(normalized["case_id"])
            cases.append(normalized)
    return rules[:12], cases[:12]


def _authority_overlap(issue: DisputeIssue, item: dict) -> int:
    issue_text = f"{issue.issue} {issue.legal_domain}".lower()
    item_text = f"{item.get('title', '')} {item.get('excerpt', '')}".lower()
    english_terms = set(re.findall(r"[a-z][a-z0-9-]{3,}", issue_text))
    chinese_terms = set(re.findall(r"租赁|租金|维修|驱逐|终止|合同|付款|劳动|解雇|证据|程序|通知|损害|过失", issue_text))
    return sum(term in item_text for term in english_terms | chinese_terms)


def _issue_authorities(issue: DisputeIssue, rules: list[dict], cases: list[dict]) -> tuple[list[dict], list[dict]]:
    ranked_rules = sorted(rules, key=lambda item: _authority_overlap(issue, item), reverse=True)
    ranked_cases = sorted(cases, key=lambda item: _authority_overlap(issue, item), reverse=True)
    return ranked_rules[:4], ranked_cases[:4]


def _build_dispute_details(plan: QueryPlan, rules: list[dict], cases: list[dict]) -> list[DisputeDetail]:
    details: list[DisputeDetail] = []
    for issue in plan.dispute_issues:
        issue_rules, issue_cases = _issue_authorities(issue, rules, cases)
        fact_ratio = min(len(issue.supporting_facts) / 3, 1.0)
        legal_ratio = 1.0 if issue_rules else 0.0
        case_ratio = 1.0 if issue_cases else 0.0
        strength = _clamp(0.4 * fact_ratio + 0.35 * legal_ratio + 0.25 * case_ratio)
        gaps: list[str] = []
        if not issue_rules:
            gaps.append("未检索到直接依据")
        if not issue_cases:
            gaps.append("未检索到可绑定的类案支持")
        if len(issue.supporting_facts) < 2:
            gaps.append("支撑该争点的可确认事实不足")
        details.append(
            DisputeDetail(
                issue=issue.issue,
                our_position=issue.our_position or "现有事实可能支持该项主张，但仍需核验原始材料。",
                opposing_position=issue.opposing_position or "对方立场尚未提供，需人工补充并核验。",
                legal_basis=issue_rules,
                case_support=issue_cases,
                strength_score=strength,
                strength_breakdown={"supporting_facts": fact_ratio, "legal_basis": legal_ratio, "case_support": case_ratio},
                unresolved_gaps=gaps,
            )
        )
    return details


def _risk_item(
    risk_type: Literal["substantive", "procedural", "evidence", "enforcement"],
    description: str,
    trigger: str,
    consequence: str,
    rule_ids: list[str],
    case_ids: list[str],
    gaps: list[str],
    base_confidence: float,
) -> RiskPrediction:
    traceable = bool(rule_ids or case_ids)
    confidence = _clamp(base_confidence if traceable else min(base_confidence, 0.29))
    return RiskPrediction(
        risk_type=risk_type,
        description=description,
        trigger_condition=trigger,
        potential_consequence=consequence,
        supporting_rule_ids=rule_ids,
        supporting_case_ids=case_ids,
        evidence_gap=gaps,
        confidence=confidence,
        manual_review_required=not traceable or confidence < 0.5,
    )


def _build_risks(plan: QueryPlan, rules: list[dict], cases: list[dict]) -> list[RiskPrediction]:
    rule_ids = [item["rule_id"] for item in rules[:4]]
    case_ids = [item["case_id"] for item in cases[:4]]
    fact_types = {fact.fact_type for fact in plan.structured_facts}
    issue_name = plan.dispute_issues[0].issue if plan.dispute_issues else "核心实体争议"
    return [
        _risk_item(
            "substantive",
            f"{issue_name}的构成要件可能无法全部由现有事实证明。",
            "当检索法规要求的义务、违反、因果或损失要件中任一项缺少可核验事实时触发。",
            "相关主张可能被缩减、驳回，或仅获得部分救济。",
            rule_ids,
            case_ids,
            ["逐项对应构成要件的事实和原始材料"],
            0.68,
        ),
        _risk_item(
            "procedural",
            "通知、申请期限、管辖或送达步骤可能存在程序瑕疵。",
            "当关键通知日期、送达凭证、法定申请期限或管辖连接点无法确认时触发。",
            "案件可能延期、被要求补正，或因程序问题无法进入实体审理。",
            rule_ids[:2],
            case_ids[:2],
            ["关键程序日期及送达凭证"] if "timeline" not in fact_types else ["核对适用程序规则与期限"],
            0.58,
        ),
        _risk_item(
            "evidence",
            "现有陈述与可采文件之间可能缺少完整证据链。",
            "当合同、付款、通知、沟通或损失材料不能与争议事实逐项对应时触发。",
            "事实可能仅被视为单方陈述，证明力和可信度下降。",
            rule_ids[:1],
            case_ids[:3],
            ["合同、付款、通知和沟通原件"] if "document" not in fact_types else ["核验文件真实性、完整性及形成时间"],
            0.62,
        ),
        _risk_item(
            "enforcement",
            "即使获得有利决定，救济金额、执行对象或可执行财产仍可能不明确。",
            "当诉求金额、责任主体身份、资产线索或具体履行方式未被确认时触发。",
            "裁判或和解结果可能难以量化、登记或实际执行。",
            rule_ids[:2],
            case_ids[:2],
            ["明确救济金额、责任主体和执行线索"] if "amount" not in fact_types else ["核验金额计算和执行对象信息"],
            0.52,
        ),
    ]


def _case_strength(plan: QueryPlan, details: list[DisputeDetail]) -> CaseStrengthScore:
    issue_total = max(len(details), 1)
    evidence = _clamp(len(plan.structured_facts) / max(issue_total * 4, 5))
    legal = _clamp(sum(bool(item.legal_basis) for item in details) / issue_total)
    case_support = _clamp(sum(bool(item.case_support) for item in details) / issue_total)
    fact_types = {fact.fact_type for fact in plan.structured_facts}
    checks = ["timeline" in fact_types, bool(plan.filters.get("jurisdictions")), "document" in fact_types, "claim" in fact_types]
    procedural = _clamp(sum(checks) / len(checks))
    opposing = _clamp(sum(bool(item.opposing_position) and bool(item.case_support) for item in details) / issue_total)
    weights = {
        "evidence": float(getattr(settings, "case_strength_weight_evidence", 0.25)),
        "legal_basis": float(getattr(settings, "case_strength_weight_legal_basis", 0.25)),
        "case_support": float(getattr(settings, "case_strength_weight_case_support", 0.20)),
        "procedural": float(getattr(settings, "case_strength_weight_procedural", 0.15)),
        "opposing": float(getattr(settings, "case_strength_weight_opposing", 0.15)),
    }
    weight_total = sum(weights.values()) or 1.0
    weighted = (
        evidence * weights["evidence"]
        + legal * weights["legal_basis"]
        + case_support * weights["case_support"]
        + procedural * weights["procedural"]
        + (1.0 - opposing) * weights["opposing"]
    ) / weight_total
    overall = _clamp(1.0 / (1.0 + math.exp(-4.0 * (weighted - 0.5))))
    return CaseStrengthScore(
        evidence_completeness=evidence,
        legal_basis_sufficiency=legal,
        case_support=case_support,
        procedural_compliance=procedural,
        opposing_strength=opposing,
        overall=overall,
        breakdown={"weights": weights, "weighted_input": round(weighted, 3), "method": "weighted_sigmoid"},
    )


def _gaps_and_materials(plan: QueryPlan, details: list[DisputeDetail], risks: list[RiskPrediction]) -> tuple[list[EvidenceGap], list[MissingMaterial]]:
    fact_types = {fact.fact_type for fact in plan.structured_facts}
    primary_issue = details[0].issue if details else "核心争议"
    gap_specs = []
    if "timeline" not in fact_types:
        gap_specs.append(("timeline", "关键事件、通知、履行与争议发生日期尚未形成完整时间线", "用于判断时限、先后因果和程序合规", "可改变时效、通知有效性及因果关系判断", "high", "时间线材料", "整理带日期的通知、邮件、付款记录和事件清单"))
    if "document" not in fact_types:
        gap_specs.append(("document", "未识别到合同、通知、付款或沟通文件", "用于把单方陈述转化为可核验事实", "可提高事实证明力并明确权利义务内容", "high", "原始书证", "取得合同原件、完整往来记录、付款凭证及送达证明"))
    if "amount" not in fact_types:
        gap_specs.append(("fact", "诉求金额、损失项目或计算方法不完整", "用于确定救济范围和争议标的", "可改变赔偿、抵扣或和解区间", "medium", "金额计算表", "按日期、项目、凭证编号列明金额并附原始凭证"))
    if any("未检索到直接依据" in detail.unresolved_gaps for detail in details):
        gap_specs.append(("expert", "至少一个争点尚未检索到可直接绑定的正式法规依据", "用于确认适用法、法条效力和构成要件", "可改变争点分类、主张路径和风险等级", "high", "律师核验记录", "由专业人员核对管辖区、现行法及正式引注"))
    if any(risk.risk_type == "evidence" and risk.manual_review_required for risk in risks):
        gap_specs.append(("witness", "关键事件的经办人或在场人员信息尚未确认", "用于补强文件不能完整解释的事实经过", "可改变可信度、因果关系和责任分配判断", "medium", "证人及经办人清单", "记录姓名、联系方式、知情范围和可提供材料"))
    if not gap_specs:
        gap_specs.append(("fact", "对方完整答辩事实及证据尚未纳入", "用于避免只依据单方陈述评估", "可改变对方抗辩强度及整体评分", "low", "对方答辩材料", "取得答辩、附件及其引用的原始记录"))

    gaps: list[EvidenceGap] = []
    materials: list[MissingMaterial] = []
    seen: set[str] = set()
    for gap_type, description, why, change, priority, material_type, hint in gap_specs:
        if description in seen:
            continue
        seen.add(description)
        gaps.append(EvidenceGap(gap_type=gap_type, description=description, why_needed=why, what_changes_if_filled=change, priority=priority))
        materials.append(MissingMaterial(material_type=material_type, description=description, related_issue=primary_issue, acquisition_hint=hint))
    return gaps, materials


def _references(rules: list[dict], cases: list[dict]) -> list[dict]:
    result = []
    for rule in rules:
        result.append({"reference_type": "rule", "reference_id": rule["rule_id"], "title": rule["title"], "citation": rule.get("article_no", ""), "source_url": rule.get("source_url", ""), "verification_status": rule.get("citation_status", "unverified"), "effectiveness_status": rule.get("effectiveness_status", "unknown")})
    for case in cases:
        result.append({"reference_type": "case", "reference_id": case["case_id"], "title": case["title"], "citation": " ".join(item for item in [case.get("court", ""), case.get("date", "")] if item), "source_url": case.get("source_url", ""), "verification_status": case.get("relation_status", "retrieved_case"), "effectiveness_status": "not_applicable"})
    return result


def build_deep_analysis(
    plan: QueryPlan,
    *,
    module_packet: dict | None = None,
    retrieval_items: list[dict] | None = None,
) -> DeepAnalysis:
    packet = module_packet or {}
    rules, cases = _authority_sets(packet, retrieval_items)
    details = _build_dispute_details(plan, rules, cases)
    risks = _build_risks(plan, rules, cases)
    strength = _case_strength(plan, details)
    gaps, materials = _gaps_and_materials(plan, details, risks)
    manual_items = [f"复核风险：{item.description}" for item in risks if item.manual_review_required]
    manual_items.extend(f"补充证据：{gap.description}" for gap in gaps if gap.priority == "high")
    low_confidence = sum(item.confidence < 0.5 for item in risks)
    conclusion_count = len(details) + len(risks)
    overall_confidence = _clamp((plan.planning_confidence + strength.overall) / 2)
    return DeepAnalysis(
        case_summary=_structured_summary(plan),
        dispute_details=details,
        risk_predictions=risks,
        case_strength=strength,
        evidence_gaps=gaps,
        missing_materials=materials,
        confidence_boundary={
            "overall_confidence": overall_confidence,
            "planning_confidence": plan.planning_confidence,
            "low_confidence_items": low_confidence,
            "conclusion_items": conclusion_count,
            "manual_review_items": len(manual_items),
            "boundary_note": "置信度只反映当前材料完整性和检索可追溯性，不代表胜诉概率。",
        },
        manual_review_items=manual_items,
        references=_references(rules, cases),
        disclaimer=DISCLAIMER,
    )


def build_deep_analysis_payload(
    case_text: str,
    *,
    keywords: list[str] | None = None,
    module_packet: dict | None = None,
    retrieval_items: list[dict] | None = None,
    use_llm: bool = True,
    load_priors: bool = True,
) -> tuple[QueryPlan, DeepAnalysis]:
    plan = build_deep_query_plan(case_text, keywords, use_llm=use_llm, load_priors=load_priors)
    analysis = build_deep_analysis(plan, module_packet=module_packet, retrieval_items=retrieval_items)
    return plan, analysis
