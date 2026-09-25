from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from app.core.config import settings
from app.service.agent_service import get_dashboard_metrics, predict_legal_outcome
from app.service.analysis_service import analyze_sentence_search, enrich_with_deep_analysis
from app.service.archive_service import (
    export_source_items_snapshot,
    get_archive_status,
    rebuild_local_archive_from_db,
)
from app.service.data_quality_service import get_source_quality_snapshot
from app.service.crawler_service import sync_all_sources
from app.service.ingestion_task_service import get_ingestion_task
from app.service.legal_data_service import (
    delete_case_vote,
    get_bulk_case_votes,
    get_canada_rule_detail_packet,
    get_case,
    get_case_vote_details,
    get_case_votes,
    get_rule,
    get_user_votes_for_cases,
    import_from_url,
    import_manual_entry,
    list_case_rule_relations,
    list_cases,
    list_import_tasks,
    list_rules,
    run_canada_crawler_import,
    upsert_case_vote,
)
from app.service.module_service import (
    answer_module_question,
    get_canada_law_detail_packet,
    get_module_definition,
    get_source_options_for_module,
    normalize_module,
    resolve_source_for_module,
)
from app.service.multi_agent_service import MultiAgentRequest, run_multi_agent_analysis
from app.service.mcp_tool_service import MCPToolCall, call_readonly_mcp_tool, list_mcp_tools
from app.service.skill_collaboration_service import SkillCollaborationRequest, run_skill_collaboration
from app.service.hearing_workflow_service import (
    HearingRunCreate,
    HearingTurnInput,
    create_hearing_run,
    get_hearing_run,
    rollback_hearing_run,
    submit_hearing_turn,
)
from app.service.ofac_service import sync_ofac_demo
from app.service.canlii_service import sync_canlii_demo
from app.service.common_service import looks_mojibake, repair_text
from app.service.pdf_service import PDFRenderError, render_legal_memo_pdf
from app.service.hybrid_retrieval_service import hybrid_search, rebuild_hybrid_index
from app.service.rag_service import export_rag_chunks, get_rag_status, rag_search, rebuild_rag_index
from app.service.risk_assessment_service import list_risk_training_samples, record_risk_feedback_label
from app.service.search_service import search_and_optionally_sync
from app.service.skill_runtime_service import list_legal_skills, run_legal_skill
from app.service.vector_store_service import get_vector_status, rebuild_chunk_embeddings, vector_search
from app.service.user_service import (
    authenticate_user,
    build_case_history_payload,
    build_history_display_payload,
    build_history_snapshot,
    build_user_history_graph,
    clear_session_user,
    count_user_histories,
    delete_history,
    get_current_user,
    get_history,
    get_last_query,
    list_all_histories,
    list_all_users,
    list_user_histories,
    record_search_history,
    register_user,
    remember_last_query,
    require_admin,
    require_user,
    set_session_user,
    normalize_case_text,
    upsert_case_history,
    update_user_profile,
    update_user_password,
    update_user_status,
)

router = APIRouter()

BASE_DIR = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _rag_structured_filters(
    jurisdiction: str | None,
    document_type: str | None,
    court_level: str | None,
    language: str | None,
    date_from: str | None,
    date_to: str | None,
) -> dict:
    values = {
        "jurisdiction": jurisdiction,
        "document_type": document_type,
        "court_level": court_level,
        "language": language,
        "date_from": date_from,
        "date_to": date_to,
    }
    return {key: repair_text(value) for key, value in values.items() if repair_text(value)}

_MODULE_PROFILE_OVERRIDES = {
    "canada": {
        "label": "加拿大法规与案例模块",
        "picker_label": "加拿大法规与案例",
        "subtitle": "先定位法律法规，再匹配本地案例与对应关系，适合做法规锚定、案例比对和历史分析。",
        "agent_title": "关联深度分析",
        "agent_placeholder": "例如：哪一部法规最关键？哪些地方判例更接近当前事实？",
        "source_options": [
            {
                "value": "canada",
                "label": "官方法规 + CanLII 案例",
                "label_en": "Official laws + CanLII cases",
                "picker_label": "法规 + 案例",
                "picker_label_en": "Laws + Cases",
            },
            {
                "value": "canlii",
                "label": "仅 CanLII 案例",
                "label_en": "CanLII cases only",
                "picker_label": "仅案例",
                "picker_label_en": "Cases Only",
            },
        ],
    },
    "us_sanctions": {
        "label": "美国 OFAC 制裁模块",
        "picker_label": "美国 OFAC 制裁",
        "subtitle": "聚焦 OFAC 规则、程序路径和合规整改材料，不扩展到普通美国判例。",
        "agent_title": "OFAC 研判助手",
        "agent_placeholder": "例如：如果公司需要准备除名申请，材料应如何排序？",
        "source_options": [
            {
                "value": "ofac",
                "label": "OFAC 规则与记录",
                "label_en": "OFAC rules and records",
                "picker_label": "OFAC 规则与记录",
                "picker_label_en": "OFAC Rules",
            },
        ],
    },
}


def _presentation_module_profile(module: str) -> dict:
    module_code = normalize_module(module)
    profile = get_module_definition(module_code)
    profile.update(_MODULE_PROFILE_OVERRIDES.get(module_code, {}))
    return profile


def _presentation_source_options(module: str) -> list[dict]:
    profile = _presentation_module_profile(module)
    return list(profile.get("source_options") or [])


def _trim_copy(value: str, limit: int = 110) -> str:
    text_value = repair_text(value)
    if not text_value:
        return ""
    if len(text_value) <= limit:
        return text_value
    return text_value[:limit].rstrip() + "..."


def _needs_regeneration(items: list[str]) -> bool:
    cleaned = [repair_text(item) for item in items if repair_text(item)]
    if not cleaned:
        return True
    for item in cleaned:
        stripped = item.replace(" ", "")
        if looks_mojibake(item):
            return True
        if "�" in item:
            return True
        if stripped and stripped.count("?") >= max(2, len(stripped) // 2):
            return True
    return False


def _is_broken_text(value: str) -> bool:
    text_value = repair_text(value)
    if not text_value:
        return True
    stripped = text_value.replace(" ", "")
    if looks_mojibake(text_value) or "�" in text_value:
        return True
    return stripped.count("?") >= max(2, len(stripped) // 2)


def _needs_structured_line_refresh(items: list[str]) -> bool:
    cleaned = [repair_text(item) for item in items if repair_text(item)]
    if _needs_regeneration(cleaned):
        return True
    noisy = 0
    for item in cleaned:
        lowered = item.lower().strip()
        alpha_only = re.sub(r"[^a-z ]", "", lowered).strip()
        if alpha_only and len(alpha_only) <= 18 and len(alpha_only.split()) <= 3 and "《" not in item:
            noisy += 1
    return noisy >= max(2, len(cleaned))


def _clean_risk_level(value: str) -> str:
    text_value = repair_text(value).lower()
    if any(token in text_value for token in ["high", "高"]):
        return "高风险"
    if any(token in text_value for token in ["low", "低"]):
        return "低风险"
    if any(token in text_value for token in ["medium", "mid", "中"]):
        return "中风险"
    return "未知风险"


def _split_text_fragments_clean(text: str, limit: int = 5) -> list[str]:
    source = repair_text(text)
    if not source:
        return []
    chunks = re.split(r"[。！？!?\n；;]+", source)
    items = []
    for chunk in chunks:
        value = repair_text(chunk).strip(" ,，；;")
        if not value:
            continue
        items.append(value)
        if len(items) >= limit:
            break
    return items


def _fallback_key_facts_clean(item: dict) -> list[str]:
    summary = repair_text(item.get("case_summary") or item.get("analysis_summary") or "")
    query_text = repair_text(item.get("query_text") or "")
    fragments = _split_text_fragments_clean(summary, limit=3) + _split_text_fragments_clean(query_text, limit=4)
    cleaned: list[str] = []
    seen = set()
    for fragment in fragments:
        key = fragment.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(fragment)
        if len(cleaned) >= 5:
            break
    return cleaned or ["当前需要回到原始案情文本重新提炼关键事实。"]


def _fallback_dispute_focus_clean(item: dict) -> list[str]:
    focus: list[str] = []
    case_type = repair_text(item.get("case_type"))
    if case_type and not _is_broken_text(case_type):
        focus.append(f"{case_type}中的责任边界如何认定")
    for relation in (item.get("legal_relations") or [])[:2]:
        value = repair_text(relation)
        if value and not _is_broken_text(value):
            focus.append(value)
    for risk in (item.get("risk_points") or [])[:2]:
        risk_name = repair_text(risk.get("name"))
        if risk_name and not _is_broken_text(risk_name):
            focus.append(f"{risk_name}是否会影响最终判断")
    rules = item.get("legal_rules") or []
    if rules:
        title = repair_text(rules[0].get("title"))
        if title:
            focus.append(f"《{title}》如何适用于当前案情")
    return focus[:5] or ["当前争议焦点需要结合案情原文进一步拆解。"]


def _fallback_legal_relations_clean(item: dict) -> list[str]:
    relations: list[str] = []
    case_type = repair_text(item.get("case_type"))
    if case_type and not _is_broken_text(case_type):
        relations.append(f"{case_type}核心法律关系")
    for law in (item.get("legal_rules") or [])[:3]:
        title = repair_text(law.get("title"))
        if title:
            relations.append(f"与《{title}》直接相关")
    prediction_label = repair_text((item.get("prediction") or {}).get("label"))
    if prediction_label and not _is_broken_text(prediction_label):
        relations.append(f"当前预测标签：{prediction_label}")
    return relations[:5]


def _fallback_evidence_focus_clean(item: dict) -> list[str]:
    focus: list[str] = []
    risk_points = item.get("risk_points") or []
    for risk in risk_points[:2]:
        description = repair_text(risk.get("description"))
        if description:
            focus.append(description[:34])
    dispute_focus = item.get("dispute_focus") or []
    for point in dispute_focus[:2]:
        text_value = repair_text(point)
        if text_value and not _is_broken_text(text_value):
            focus.append(f"围绕“{text_value[:20]}”补强证据")
    if not focus:
        focus = ["先补齐时间线、主体关系和关键书证", "优先核对能直接支持主张的证据链"]
    return focus[:5]


def _fallback_actions_clean(item: dict) -> list[str]:
    actions: list[str] = []
    risk_level = repair_text((item.get("prediction") or {}).get("risk_level") or item.get("risk_level"))
    if risk_level == "高风险":
        actions.append("优先补强关键证据，再重新评估主张强度")
    elif risk_level == "中风险":
        actions.append("围绕争议焦点补充书面证据与时间线")
    else:
        actions.append("保留现有论证结构，继续补充针对性佐证")
    laws = item.get("legal_rules") or []
    if laws:
        title = repair_text(laws[0].get("title"))
        if title:
            actions.append(f"围绕《{title}》准备对应论证与证据")
    case_type = repair_text(item.get("case_type"))
    if "劳动" in case_type:
        actions.append("整理劳动关系、工资和考勤材料")
    elif "合同" in case_type:
        actions.append("核对合同条款、补充协议和履约凭证")
    elif "侵权" in case_type:
        actions.append("补强因果关系和损失金额证据")
    return actions[:4]


def _fallback_legal_relations(item: dict) -> list[str]:
    relations: list[str] = []
    case_type = repair_text(item.get("case_type"))
    if case_type:
        relations.append(f"{case_type}核心法律关系")
    for law in (item.get("legal_rules") or [])[:3]:
        title = repair_text(law.get("title"))
        if title:
            relations.append(f"与《{title}》直接相关")
    prediction_label = repair_text((item.get("prediction") or {}).get("label"))
    if prediction_label:
        relations.append(f"当前预测标签：{prediction_label}")
    return relations[:5]


def _fallback_evidence_focus(item: dict) -> list[str]:
    focus: list[str] = []
    risk_points = item.get("risk_points") or []
    for risk in risk_points[:2]:
        description = repair_text(risk.get("description"))
        if description:
            focus.append(description[:34])
    dispute_focus = item.get("dispute_focus") or []
    for point in dispute_focus[:2]:
        text_value = repair_text(point)
        if text_value:
            focus.append(f"围绕“{text_value[:20]}”补强证据")
    if not focus:
        focus = ["先补齐时间线、主体关系和关键书证", "优先核对能直接支持主张的证据链"]
    return focus[:5]


def _fallback_actions(item: dict) -> list[str]:
    actions: list[str] = []
    risk_level = repair_text((item.get("prediction") or {}).get("risk_level") or item.get("risk_level"))
    if risk_level == "高风险":
        actions.append("优先补强关键证据，再重新评估主张强度")
    elif risk_level == "中风险":
        actions.append("围绕争议焦点补充书面证据与时间线")
    else:
        actions.append("保留现有论证结构，继续补充针对性佐证")
    laws = item.get("legal_rules") or []
    if laws:
        title = repair_text(laws[0].get("title"))
        if title:
            actions.append(f"围绕《{title}》准备对应论证与证据")
    case_type = repair_text(item.get("case_type"))
    if "劳动" in case_type:
        actions.append("整理劳动关系、工资和考勤材料")
    elif "合同" in case_type:
        actions.append("核对合同条款、补充协议和履约凭证")
    elif "侵权" in case_type:
        actions.append("补强因果关系和损失金额证据")
    return actions[:4]

def _fallback_key_facts_clean(item: dict) -> list[str]:
    summary = repair_text(item.get("case_summary") or item.get("analysis_summary") or "")
    query_text = repair_text(item.get("query_text") or "")
    fragments = _split_text_fragments_clean(summary, limit=3) + _split_text_fragments_clean(query_text, limit=4)
    cleaned: list[str] = []
    seen = set()
    for fragment in fragments:
        key = fragment.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(fragment)
        if len(cleaned) >= 5:
            break
    return cleaned or ["当前需要回到原始案情文本，重新提炼关键事实。"]


def _fallback_dispute_focus_clean(item: dict) -> list[str]:
    focus: list[str] = []
    case_type = repair_text(item.get("case_type"))
    if case_type and not _is_broken_text(case_type):
        focus.append(f"{case_type}中的责任边界如何认定")
    for relation in (item.get("legal_relations") or [])[:2]:
        value = repair_text(relation)
        if value and not _is_broken_text(value):
            focus.append(value)
    for risk in (item.get("risk_points") or [])[:2]:
        risk_name = repair_text(risk.get("name"))
        if risk_name and not _is_broken_text(risk_name):
            focus.append(f"{risk_name}是否会影响最终判断")
    rules = item.get("legal_rules") or []
    if rules:
        title = repair_text(rules[0].get("title"))
        if title:
            focus.append(f"《{title}》如何适用于当前案情")
    return focus[:5] or ["当前争议焦点需要结合案情原文进一步拆解。"]


def _fallback_legal_relations_clean(item: dict) -> list[str]:
    relations: list[str] = []
    case_type = repair_text(item.get("case_type"))
    if case_type and not _is_broken_text(case_type):
        relations.append(f"{case_type}案件的核心法律关系")
    for law in (item.get("legal_rules") or [])[:3]:
        title = repair_text(law.get("title"))
        if title:
            relations.append(f"与《{title}》直接相关")
    prediction_label = repair_text((item.get("prediction") or {}).get("label"))
    if prediction_label and not _is_broken_text(prediction_label):
        relations.append(f"当前预测标签：{prediction_label}")
    return relations[:5]


def _fallback_evidence_focus_clean(item: dict) -> list[str]:
    focus: list[str] = []
    risk_points = item.get("risk_points") or []
    for risk in risk_points[:2]:
        description = repair_text(risk.get("description"))
        if description:
            focus.append(description[:34])
    dispute_focus = item.get("dispute_focus") or []
    for point in dispute_focus[:2]:
        text_value = repair_text(point)
        if text_value and not _is_broken_text(text_value):
            focus.append(f"围绕“{text_value[:20]}”补强证据")
    if not focus:
        focus = ["先补齐时间线、主体关系和关键书证", "优先核对能够直接支持主张的证据链"]
    return focus[:5]


def _fallback_actions_clean(item: dict) -> list[str]:
    actions: list[str] = []
    risk_level = repair_text((item.get("prediction") or {}).get("risk_level") or item.get("risk_level"))
    if risk_level == "高风险":
        actions.append("优先补强关键证据，再重新评估主张强度")
    elif risk_level == "中风险":
        actions.append("围绕争议焦点补充书面证据和时间线")
    else:
        actions.append("保留现有论证结构，继续补充针对性佐证")
    laws = item.get("legal_rules") or []
    if laws:
        title = repair_text(laws[0].get("title"))
        if title:
            actions.append(f"围绕《{title}》准备对应论证与证据")
    case_type = repair_text(item.get("case_type"))
    if "劳动" in case_type:
        actions.append("整理劳动关系、工资和考勤材料")
    elif "合同" in case_type:
        actions.append("核对合同条款、补充协议和履约凭证")
    elif "侵权" in case_type:
        actions.append("补强因果关系和损失金额证据")
    return actions[:4]


def _sanitize_history_display(item: dict) -> dict:
    payload = dict(item or {})
    payload["analysis_summary"] = repair_text(payload.get("analysis_summary"))
    payload["case_summary"] = repair_text(payload.get("case_summary"))
    payload["query_text"] = repair_text(payload.get("query_text"))
    key_facts = [repair_text(entry) for entry in (payload.get("key_facts") or []) if repair_text(entry)]
    dispute_focus = [repair_text(entry) for entry in (payload.get("dispute_focus") or []) if repair_text(entry)]
    legal_relations = [repair_text(entry) for entry in (payload.get("legal_relations") or []) if repair_text(entry)]
    evidence_focus = [repair_text(entry) for entry in (payload.get("evidence_focus") or []) if repair_text(entry)]
    suggested_actions = [repair_text(entry) for entry in ((payload.get("prediction") or {}).get("suggested_actions") or []) if repair_text(entry)]
    if _needs_regeneration(key_facts):
        key_facts = _fallback_key_facts_clean(payload)
    if _needs_structured_line_refresh(dispute_focus):
        dispute_focus = _fallback_dispute_focus_clean(payload)
    payload["key_facts"] = key_facts
    payload["dispute_focus"] = dispute_focus
    if _needs_structured_line_refresh(legal_relations):
        legal_relations = _fallback_legal_relations_clean(payload)
    if _needs_structured_line_refresh(evidence_focus):
        evidence_focus = _fallback_evidence_focus_clean(payload)
    if _needs_regeneration(suggested_actions):
        suggested_actions = _fallback_actions_clean(payload)
    payload["legal_relations"] = legal_relations
    payload["evidence_focus"] = evidence_focus
    payload.setdefault("prediction", {})
    payload["prediction"]["suggested_actions"] = suggested_actions
    payload["prediction"]["label"] = repair_text(payload["prediction"].get("label"))
    payload["prediction"]["conclusion"] = repair_text(payload["prediction"].get("conclusion"))
    payload["prediction"]["explanation"] = repair_text(payload["prediction"].get("explanation"))
    payload["prediction"]["risk_level"] = _clean_risk_level(payload["prediction"].get("risk_level") or payload.get("risk_level"))
    payload["risk_level"] = _clean_risk_level(payload.get("risk_level") or payload["prediction"].get("risk_level"))
    payload["module_code"] = repair_text(payload.get("module_code"))
    payload["supporting_case_groups"] = payload.get("supporting_case_groups") or []
    payload["supporting_case_rows"] = payload.get("supporting_case_rows") or []
    return payload


def _sanitize_history_graph(graph: dict) -> dict:
    payload = dict(graph or {})
    nodes = []
    for node in payload.get("nodes", []) or []:
        node_entry = dict(node)
        case_type = repair_text(node_entry.get("caseType"))
        country = repair_text(node_entry.get("country"))
        if country.lower() in {"canada", "ca"}:
            country = "加拿大"
        elif country.lower() in {"united states", "usa", "us", "u.s.", "u.s"}:
            country = "美国"
        court_level = repair_text(node_entry.get("courtLevel"))
        if not court_level or court_level == country or court_level.lower() in {"canada", "united states", "usa", "us", "u.s.", "u.s"}:
            court_level = "未标注级别"
        node_label = repair_text(node_entry.get("nodeLabel"))
        if (
            not node_label
            or looks_mojibake(node_label)
            or (re.fullmatch(r"[A-Za-z0-9 ./_-]{4,}", node_label) and len(node_label.strip()) > 4)
        ):
            node_label = case_type[:6] or _clean_risk_level(node_entry.get("riskLevel"))[:6] or "案件"
        node_entry["nodeLabel"] = node_label or "案件"
        node_entry["caseTitle"] = repair_text(node_entry.get("caseTitle")) or "未命名案件"
        node_entry["caseType"] = case_type or "综合"
        node_entry["country"] = country or "未标注国家"
        node_entry["courtLevel"] = court_level
        node_entry["riskLevel"] = _clean_risk_level(node_entry.get("riskLevel"))
        node_entry["prediction"] = repair_text(node_entry.get("prediction"))
        node_entry["laws"] = [repair_text(item) for item in (node_entry.get("laws") or []) if repair_text(item) and not looks_mojibake(item)]
        node_entry["riskPoints"] = [
            repair_text(item)
            for item in (node_entry.get("riskPoints") or [])
            if repair_text(item)
            and not looks_mojibake(item)
            and all(token not in repair_text(item) for token in ["当前结果基于", "本地案例库", "国家/联邦", "省级/地方", "不替代正式法律意见"])
        ]
        nodes.append(node_entry)
    payload["nodes"] = nodes
    return payload


def _sanitize_module_packet(module_packet: dict, module_code: str) -> dict:
    packet = dict(module_packet or {})
    if module_code == "canada":
        packet["focus_title"] = "相关法律法规与案例"
        packet["focus_copy"] = "以下内容按照法规与案例的对应关系整理展示。"
        packet["focus_copy_en"] = ""
        packet["notice"] = "案例与法规的对应关系优先依据本地已建立的 case_rule_relations，而不是只凭关键词相似强行归类。"
    elif module_code == "us_sanctions":
        packet["focus_title"] = "OFAC 规则与相关材料"
        packet["focus_copy"] = "以下内容围绕 OFAC 规则、程序路径和本地命中的相关材料整理展示。"
        packet["focus_copy_en"] = ""
        packet["notice"] = "该模块聚焦 OFAC 制裁、许可、除名和合规整改路径，不扩展到泛化美国案例。"
    laws = []
    for law in packet.get("relevant_laws", []) or []:
        law_entry = dict(law)
        law_entry["article_summary"] = _trim_copy(
            law_entry.get("article_summary")
            or law_entry.get("article_text")
            or law_entry.get("reason")
            or "",
            140,
        )
        if not law_entry["article_summary"]:
            law_title = repair_text(law_entry.get("title"))
            article_no = repair_text(law_entry.get("article_no"))
            legal_type = repair_text(law_entry.get("legal_type") or law_entry.get("rule_level"))
            law_entry["article_summary"] = _trim_copy(
                f"{law_title} {article_no} {legal_type} 的核心内容需要结合条文全文核对。".strip(),
                140,
            )
        related_cases = []
        for case in law_entry.get("related_cases", []) or []:
            case_entry = dict(case)
            case_entry["summary"] = _trim_copy(case_entry.get("summary") or case_entry.get("facts") or "", 88)
            related_cases.append(case_entry)
        law_entry["related_cases"] = related_cases[:4]
        laws.append(law_entry)
    packet["relevant_laws"] = laws
    case_rows = []
    for case in packet.get("case_law_rows", []) or []:
        case_entry = dict(case)
        case_entry["summary"] = _trim_copy(case_entry.get("summary") or case_entry.get("facts") or "", 180)
        if module_code == "canada" and case_entry.get("scope") not in {"national_federal", "provincial_local"}:
            case_entry["scope"] = "provincial_local"
        rules = []
        for law in case_entry.get("rules", []) or []:
            law_row = dict(law)
            law_row["article_summary"] = _trim_copy(law_row.get("article_summary") or "", 96)
            rules.append(law_row)
        case_entry["rules"] = rules
        case_rows.append(case_entry)
    packet["case_law_rows"] = case_rows
    packet["module_label"] = _presentation_module_profile(module_code).get("label")
    return packet


def _sanitize_prediction_payload(payload: dict, module_code: str) -> dict:
    clean_payload = dict(payload or {})
    clean_payload["module_packet"] = _sanitize_module_packet(clean_payload.get("module_packet") or {}, module_code)
    if isinstance(clean_payload.get("prediction"), dict):
        prediction = dict(clean_payload.get("prediction") or {})
        prediction["confidence"] = _safe_float(prediction.get("confidence"))
        prediction["confidence_percent"] = int(round(prediction["confidence"] * 100))
        support_groups = []
        for group in prediction.get("supporting_case_groups", []) or []:
            group_entry = dict(group)
            group_entry["article_summary"] = _trim_copy(group_entry.get("article_summary") or "", 120)
            cases = []
            for case in group_entry.get("cases", []) or []:
                case_entry = dict(case)
                case_entry["summary"] = _trim_copy(case_entry.get("summary") or "", 120)
                case_entry["match_reason"] = _trim_copy(case_entry.get("match_reason") or "", 96)
                cases.append(case_entry)
            group_entry["cases"] = cases
            support_groups.append(group_entry)
        prediction["supporting_case_groups"] = support_groups
        linked_laws = []
        for law in prediction.get("linked_laws", []) or []:
            law_entry = dict(law)
            law_entry["article_summary"] = _trim_copy(law_entry.get("article_summary") or "", 110)
            linked_laws.append(law_entry)
        prediction["linked_laws"] = linked_laws
        evidence = dict(prediction.get("prediction_evidence") or {})
        evidence_laws = []
        for law in evidence.get("laws", []) or []:
            law_entry = dict(law)
            law_entry["article_summary"] = _trim_copy(law_entry.get("article_summary") or "", 120)
            evidence_laws.append(law_entry)
        evidence_cases = []
        for case in evidence.get("cases", []) or []:
            case_entry = dict(case)
            case_entry["summary"] = _trim_copy(case_entry.get("summary") or "", 140)
            case_entry["match_reason"] = _trim_copy(case_entry.get("match_reason") or "", 100)
            case_entry["similarities"] = [_trim_copy(item, 90) for item in (case_entry.get("similarities") or [])]
            case_entry["differences"] = [_trim_copy(item, 90) for item in (case_entry.get("differences") or [])]
            evidence_cases.append(case_entry)
        evidence["laws"] = evidence_laws
        evidence["cases"] = evidence_cases
        prediction["prediction_evidence"] = evidence
        clean_payload["prediction"] = prediction
    return clean_payload


class ChatRequest(BaseModel):
    module: str = "canada"
    text: str
    question: str
    limit: int = settings.default_search_limit
    offset: int = 0
    source: str = "all"
    sort: str = "relevance"
    refresh: bool = False


class AuthPayload(BaseModel):
    username: str = ""
    email: str = ""
    password: str
    confirmPassword: str = ""


class LoginPayload(BaseModel):
    login: str = ""
    username: str = ""
    email: str = ""
    password: str


class AnalyzePayload(BaseModel):
    text: str
    limit: int = settings.default_search_limit
    offset: int = 0
    source: str = "all"
    sort: str = "relevance"
    module: str = "canada"
    refresh: bool = False


class ProfilePayload(BaseModel):
    email: str = ""
    phone: str = ""
    organization: str = ""
    real_name: str = ""
    country_preference: str = ""
    legal_type_preference: str = ""
    note: str = ""


class PasswordPayload(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


class UserStatusPayload(BaseModel):
    status: str


class ImportUrlPayload(BaseModel):
    source_url: str
    country: str
    data_type: str
    legal_type: str = ""
    case_type: str = ""
    court_level: str = ""
    auto_link: bool = False


class ImportManualPayload(BaseModel):
    data_type: str
    country: str
    title: str
    legal_type: str = ""
    case_type: str = ""
    court_name: str = ""
    court_level: str = ""
    summary: str = ""
    facts: str = ""
    judgment_result: str = ""
    article_no: str = ""
    article_text: str = ""
    article_summary: str = ""
    source_url: str = ""
    auto_link: bool = False


class RiskFeedbackPayload(BaseModel):
    sample_id: int
    human_risk_level: str
    human_outcome: str = ""
    human_notes: str = ""
    label_json: dict = Field(default_factory=dict)


class SkillRunPayload(BaseModel):
    skill_name: str
    query: str
    filters: dict = Field(default_factory=dict)
    limit: int = Field(default=8, ge=1, le=12)


def _base_context(request: Request, page_id: str) -> dict:
    metrics = get_dashboard_metrics()
    module_options = [
        {"value": "canada", **_presentation_module_profile("canada")},
        {"value": "us_sanctions", **_presentation_module_profile("us_sanctions")},
    ]
    current_user = get_current_user(request)
    return {
        "request": request,
        "app_name": settings.app_name,
        "page_id": page_id,
        "dashboard": metrics,
        "module_options": module_options,
        "module_source_map": {item["value"]: item.get("source_options", []) for item in module_options},
        "current_user": current_user,
        "last_query": get_last_query(request),
        "page_notice": str(request.query_params.get("message") or "").strip(),
    }


def _to_bool(value) -> bool:
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _safe_float(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _build_query_string(**kwargs) -> str:
    clean = {key: value for key, value in kwargs.items() if value not in {None, ""}}
    return urlencode(clean)


def _landing_url_for_user(user: dict | None) -> str:
    if user and user.get("is_admin"):
        return "/admin"
    return "/home"


def _normalize_next_url(next_url: str | None, user: dict | None = None) -> str:
    clean = str(next_url or "").strip()
    fallback = _landing_url_for_user(user)
    if not clean or clean == "/" or not clean.startswith("/") or clean.startswith("//"):
        return fallback
    if clean.startswith(("/login", "/register", "/auth/login", "/auth/register", "/api/auth/")):
        return fallback
    if user and not user.get("is_admin") and clean.startswith("/admin"):
        return "/home?message=" + quote("当前账号无权限访问该页面")
    return clean


def _login_redirect(next_url: str = "/home", message: str = "登录状态已过期，请重新登录") -> RedirectResponse:
    query = _build_query_string(next=_normalize_next_url(next_url), message=message)
    return RedirectResponse(url=f"/login?{query}", status_code=303)


def _forbidden_redirect() -> RedirectResponse:
    return RedirectResponse(url="/home?message=" + quote("当前账号无权限访问该页面"), status_code=303)


def _require_page_user(request: Request, next_url: str) -> dict | RedirectResponse:
    try:
        return require_user(request)
    except HTTPException as exc:
        return _login_redirect(next_url=next_url, message=str(exc.detail))


def _require_page_admin(request: Request, next_url: str) -> dict | RedirectResponse:
    page_user = _require_page_user(request, next_url)
    if isinstance(page_user, RedirectResponse):
        return page_user
    if not page_user.get("is_admin"):
        return _forbidden_redirect()
    return page_user


def _cache_safe_template(template_name: str, context: dict, status_code: int = 200) -> HTMLResponse:
    request = context.get("request")
    if request is None:
        raise RuntimeError("Template context must include request")
    try:
        response = templates.TemplateResponse(
            request=request,
            name=template_name,
            context=context,
            status_code=status_code,
        )
    except TypeError:
        response = templates.TemplateResponse(template_name, context, status_code=status_code)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


def _memo_url(text: str, limit: int, offset: int, source: str, sort: str, module: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    return f"/memo?{query}"


def _analysis_url(text: str, limit: int, offset: int, source: str, sort: str, module: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    return f"/analysis?{query}"


def _predict_url(text: str, limit: int, offset: int, source: str, sort: str, module: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    return f"/predict?{query}"


def _memo_download_url(text: str, limit: int, offset: int, source: str, sort: str, module: str, refresh: bool = False) -> str:
    query = _build_query_string(
        text=text,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    return f"/memo/download?{query}"


def _task_context(remote_fetch: dict | None, refresh_url: str) -> dict:
    task = (remote_fetch or {}).get("task") if isinstance(remote_fetch, dict) else None
    return {
        "ingestion_task": task,
        "task_refresh_url": refresh_url,
        "task_poll_ms": max(1000, int(getattr(settings, "ingestion_page_poll_seconds", 3000))),
    }


def _remember_query(
    request: Request,
    *,
    query_text: str,
    restore_url: str,
):
    remember_last_query(request, restore_url, query_text[:80])


def _save_case_history_if_possible(
    request: Request,
    *,
    query_text: str,
    module_code: str,
    result_payload: dict,
) -> int:
    user = get_current_user(request)
    if not user or not query_text.strip():
        return 0

    # 获取 prediction_payload，如果没有则使用 result_payload 本身
    prediction_payload = result_payload.get("prediction") or {}
    if not prediction_payload:
        payload = build_case_history_payload(
            query_text=query_text,
            analysis_payload=result_payload,
            prediction_payload={},
            module_packet=result_payload.get("module_packet") or {},
            module_code=module_code,
        )
        history_id = upsert_case_history(user_id=int(user["id"]), payload=payload)
        if history_id:
            remember_last_query(request, f"/histories/{history_id}", payload.get("case_title") or query_text[:80])
        return history_id

    # 确保 prediction_payload 包含所有必要的字段
    if not prediction_payload.get("linked_laws"):
        prediction_payload["linked_laws"] = result_payload.get("linked_laws") or []
    if not prediction_payload.get("supporting_case_groups"):
        prediction_payload["supporting_case_groups"] = result_payload.get("supporting_case_groups") or []

    # 确保预测数据完整 - 从 result_payload 中获取正确的置信度
    if not prediction_payload.get("predicted_outcome"):
        prediction_payload["predicted_outcome"] = result_payload.get("analysis", {}).get("summary") or "待预测"
    if not prediction_payload.get("reasoning"):
        prediction_payload["reasoning"] = result_payload.get("analysis", {}).get("summary") or ""

    # 从嵌套的 prediction 字典中获取正确的置信度
    confidence = prediction_payload.get("confidence")
    original_confidence = confidence
    if confidence is None or confidence == 0:
        # 尝试从嵌套的 prediction 字典中获取置信度
        nested_prediction = prediction_payload.get("prediction") or {}
        confidence_from_nested = nested_prediction.get("confidence")
        if confidence_from_nested is not None and confidence_from_nested > 0:
            prediction_payload["confidence"] = confidence_from_nested
        else:
            prediction_payload["confidence"] = 0.5  # 默认置信度

    if _safe_float(original_confidence) <= 0 and _safe_float((prediction_payload.get("prediction") or {}).get("confidence")) <= 0 and _safe_float(prediction_payload.get("confidence")) == 0.5:
        prediction_payload["confidence"] = 0

    payload = build_case_history_payload(
        query_text=query_text,
        analysis_payload=result_payload,
        prediction_payload=prediction_payload,
        module_packet=result_payload.get("module_packet") or {},
        module_code=module_code,
    )
    history_id = upsert_case_history(user_id=int(user["id"]), payload=payload)
    if history_id:
        remember_last_query(request, f"/histories/{history_id}", payload.get("case_title") or query_text[:80])
    return history_id


def _matching_history_id_for_query(*, user_id: int, query_text: str, module_code: str) -> int:
    normalized_query = normalize_case_text(query_text)
    if not normalized_query:
        return 0
    country = "United States" if normalize_module(module_code) == "us_sanctions" else "Canada"
    for item in list_user_histories(user_id=user_id, country=country, limit=200):
        candidate = normalize_case_text(item.get("query_text") or "")
        if candidate == normalized_query:
            try:
                return int(item.get("id") or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def _exact_history_display_for_query(*, user_id: int, query_text: str, module_code: str) -> tuple[int, dict | None]:
    history_id = _matching_history_id_for_query(user_id=user_id, query_text=query_text, module_code=module_code)
    if not history_id:
        return 0, None
    history = get_history(history_id, user_id=user_id, admin=False, touch=False)
    if not history:
        return 0, None
    return history_id, _sanitize_history_display(build_history_display_payload(history))


def _build_analysis_view_payload(
    *,
    query_text: str,
    module_code: str,
    result_payload: dict,
    history_id: int = 0,
    user_id: int | None = None,
) -> dict:
    if history_id and user_id:
        history = get_history(history_id, user_id=int(user_id), admin=False, touch=False)
        if history:
            return build_history_display_payload(history)

    synthetic = build_case_history_payload(
        query_text=query_text,
        analysis_payload=result_payload,
        prediction_payload=result_payload.get("prediction") or {},
        module_packet=result_payload.get("module_packet") or {},
        module_code=module_code,
    )
    synthetic_row = {
        **synthetic,
        "id": history_id,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "last_viewed_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "view_count": 1,
    }
    return build_history_display_payload(synthetic_row)


def _history_case_columns_from_display(history_item: dict, law: dict) -> list[dict]:
    rule_id = int(law.get("rule_id") or 0) if str(law.get("rule_id") or "").isdigit() else 0
    title = repair_text(law.get("title"))
    national_items: list[dict] = []
    local_items: list[dict] = []
    for case in history_item.get("supporting_case_rows") or []:
        matched = False
        for linked_law in case.get("rules") or []:
            linked_rule_id = int(linked_law.get("rule_id") or 0) if str(linked_law.get("rule_id") or "").isdigit() else 0
            linked_title = repair_text(linked_law.get("title"))
            if (rule_id and linked_rule_id == rule_id) or (title and linked_title == title):
                matched = True
                break
        if not matched:
            continue
        case_item = {
            "title": repair_text(case.get("title")),
            "court_level": repair_text(case.get("court_level")),
            "case_type": repair_text(case.get("case_type")),
            "source_url": repair_text(case.get("source_url")),
        }
        scope = repair_text(case.get("scope"))
        if scope == "national_federal":
            national_items.append(case_item)
        else:
            local_items.append(case_item)
    return [
        {"key": "national_federal", "label": "国家 / 联邦", "items": national_items[:4]},
        {"key": "provincial_local", "label": "地区 / 地方", "items": local_items[:4]},
    ]


def _history_module_packet_from_display(history_item: dict, module_code: str) -> dict:
    module_profile = _presentation_module_profile(module_code)
    relevant_laws = []
    for law in history_item.get("legal_rules") or []:
        relevant_laws.append(
            {
                "rule_id": law.get("rule_id"),
                "title": repair_text(law.get("title")),
                "article_no": repair_text(law.get("article_no")),
                "article_summary": repair_text(law.get("summary")),
                "country": repair_text(law.get("country")),
                "legal_type": repair_text(law.get("legal_type")),
                "detail_url": repair_text(law.get("detail_url")),
                "source_url": repair_text(law.get("source_url")),
                "linked_case_count": sum(
                    1
                    for case in (history_item.get("supporting_case_rows") or [])
                    if any(
                        (
                            str(rule.get("rule_id") or "") == str(law.get("rule_id") or "")
                            and str(law.get("rule_id") or "")
                        )
                        or repair_text(rule.get("title")) == repair_text(law.get("title"))
                        for rule in (case.get("rules") or [])
                    )
                ),
                "case_columns": _history_case_columns_from_display(history_item, law),
            }
        )
    packet = {
        "module_label": module_profile.get("label"),
        "module_label_en": "",
        "focus_title": "",
        "focus_copy": "",
        "notice": "",
        "relevant_laws": relevant_laws,
        "case_law_rows": history_item.get("supporting_case_rows") or [],
    }
    return _sanitize_module_packet(packet, module_code)


def _history_prediction_process_from_display(history_item: dict) -> list[dict]:
    laws = history_item.get("legal_rules") or []
    cases = history_item.get("supporting_case_rows") or []
    dispute_focus = history_item.get("dispute_focus") or []
    prediction = history_item.get("prediction") or {}
    risk_points = history_item.get("risk_points") or []

    # 构建法规与案例的详细描述
    law_titles = [law.get("title", "") for law in laws[:3]]
    case_titles = [case.get("title", "") for case in cases[:3]]
    law_detail = f"已关联法规 {len(laws)} 条" + (f"：{'、'.join(law_titles)}" if law_titles else "")
    case_detail = f"相关案例 {len(cases)} 条" + (f"：{'、'.join(case_titles)}" if case_titles else "")

    # 构建风险点描述
    risk_detail = "；".join([risk.get("description") or risk.get("name") for risk in risk_points[:3]]) if risk_points else "当前没有额外提炼出显著的不确定因素。"

    return [
        {
            "kicker": "Facts",
            "title": "先把当前案情拆成事实、争议和请求事项",
            "detail": history_item.get("case_summary") or history_item.get("query_text") or "当前记录缺少完整案情摘要。",
            "status": "已复用历史记录中的结构化分析。",
        },
        {
            "kicker": "Materials",
            "title": "再按拆分关键词去本地检索法规和相关案例",
            "detail": f"{law_detail}；{case_detail}。系统继续按法规与案例的对应关系展示。",
            "status": "全部材料来自本地已保存记录。",
        },
        {
            "kicker": "Comparison",
            "title": "把当前案情与已归到对应法规下的案例逐条比照",
            "detail": f"已对 {len(cases)} 个案例进行了相似点和差异点分析。" + (" 案例比照结果：" + "；".join([f"{case.get('title', '')}：{case.get('match_reason', '')}" for case in cases[:2]]) if cases else ""),
            "status": "按历史记录中的案例对比继续展示。",
        },
        {
            "kicker": "Risk",
            "title": "最后看哪些不确定因素会左右结论",
            "detail": risk_detail,
            "status": "按历史记录中的风险点继续展示。",
        },
        {
            "kicker": "Preliminary View",
            "title": "据此给出一个面向律师阅读顺序的初步判断",
            "detail": prediction.get("explanation") or history_item.get("analysis_summary") or "当前没有保存完整结论说明。",
            "status": "本次页面未重新调用模型，直接复用已保存的预测结论。",
        },
    ]


def _history_prediction_is_reusable(history_item: dict) -> bool:
    prediction = history_item.get("prediction") or {}
    if not history_item.get("has_prediction"):
        return False
    if not (history_item.get("supporting_case_rows") or []):
        return False

    conclusion = repair_text(prediction.get("conclusion"))
    explanation = repair_text(prediction.get("explanation"))
    confidence = _safe_float(prediction.get("confidence"))
    if not conclusion or not explanation or confidence <= 0:
        return False

    analysis_summary = repair_text(history_item.get("analysis_summary"))
    case_summary = repair_text(history_item.get("case_summary"))
    query_text = repair_text(history_item.get("query_text"))
    copied_analysis = conclusion and conclusion in {analysis_summary, case_summary, query_text[: len(conclusion)]}
    copied_explanation = explanation and explanation in {analysis_summary, case_summary, query_text[: len(explanation)]}
    if confidence == 0.5 and copied_analysis and copied_explanation:
        return False

    return True


def _prediction_payload_from_history_display(history_item: dict, module_code: str) -> dict:
    keywords = []
    if history_item.get("case_type"):
        keywords.append(history_item["case_type"])
    for law in history_item.get("legal_rules") or []:
        title = repair_text(law.get("title"))
        if title and title not in keywords:
            keywords.append(title)
        if len(keywords) >= 6:
            break
    prediction = history_item.get("prediction") or {}

    # 确保 confidence 不为0，从多个来源尝试获取
    confidence = float(prediction.get("confidence") or 0)
    if confidence == 0:
        # 尝试从 history_item 的其他字段获取
        confidence = float(history_item.get("prediction_confidence") or 0)
    if confidence == 0 and history_item.get("supporting_case_rows"):
        # 如果有支撑案例，给出一个基础置信度
        case_count = len(history_item.get("supporting_case_rows") or [])
        law_count = len(history_item.get("legal_rules") or [])
        confidence = min(0.3 + (case_count * 0.1) + (law_count * 0.05), 0.8)
    history_module_packet = _history_module_packet_from_display(history_item, module_code)
    prediction_evidence = {
        "source": "history_analysis_result",
        "law_count": len(history_item.get("legal_rules") or []),
        "case_count": len(history_item.get("supporting_case_rows") or []),
        "laws": history_item.get("legal_rules") or [],
        "cases": [
            {
                **dict(case),
                "law_titles": [rule.get("title") for rule in (case.get("rules") or []) if rule.get("title")],
            }
            for case in (history_item.get("supporting_case_rows") or [])
        ],
        "law_case_groups": history_item.get("supporting_case_groups") or [],
    }

    return {
        "analysis": {
            "jurisdiction": history_item.get("court_level") or history_item.get("country") or "",
            "summary": history_item.get("analysis_summary") or history_item.get("case_summary") or "",
        },
        "intake_outline": {
            "facts": history_item.get("case_summary") or history_item.get("query_text") or "",
            "disputed_issues": history_item.get("dispute_focus") or [],
            "requested_relief": "",
            "keywords": keywords,
        },
        "bilingual_context": {
            "facts": {"zh": history_item.get("case_summary") or history_item.get("query_text") or "", "en": ""},
            "disputed_issues": {"zh": history_item.get("dispute_focus") or [], "en": []},
            "requested_relief": {"zh": "", "en": ""},
            "keywords": {"zh": keywords, "en": []},
        },
        "prediction": {
            "label": repair_text(prediction.get("label")) or "综合判断",
            "predicted_outcome": repair_text(prediction.get("conclusion")) or repair_text(prediction.get("label")) or "当前没有稳定预测结论。",
            "likely_prevailing_party": repair_text(prediction.get("label")) or "综合判断",
            "confidence": confidence,
            "confidence_percent": int(round(confidence * 100)),
            "reasoning": repair_text(prediction.get("explanation")) or history_item.get("analysis_summary") or history_item.get("case_summary") or "",
            "reason_points": history_item.get("key_facts") or history_item.get("legal_relations") or [],
            "risk_points": [repair_text(item.get("description") or item.get("name")) for item in (history_item.get("risk_points") or []) if repair_text(item.get("description") or item.get("name"))],
            "risk_level": repair_text(prediction.get("risk_level")) or history_item.get("risk_level") or "未知风险",
            "suggested_actions": prediction.get("suggested_actions") or [],
            "prediction_process": _history_prediction_process_from_display(history_item),
            "jurisdiction": history_item.get("court_level") or history_item.get("country") or "",
            "requested_relief": "",
            "support_case_count": len(history_item.get("supporting_case_rows") or []),
            "execution_summary": [
                "当前页面直接复用已保存的预测结果。",
                "未重新发起新的模型推理调用。",
                "支撑法规与案例继续按本地记录展示。",
            ],
            "model_status": "history_reuse",
            "model_name": "历史结果复用",
            "model_error": "",
            "supporting_case_groups": history_item.get("supporting_case_groups") or [],
            "linked_laws": history_item.get("legal_rules") or [],
            "prediction_evidence": prediction_evidence,
            "bilingual": {
                "predicted_outcome_zh": repair_text(prediction.get("conclusion")) or repair_text(prediction.get("label")) or "当前没有稳定预测结论。",
                "predicted_outcome_en": "",
                "reasoning_zh": repair_text(prediction.get("explanation")) or history_item.get("analysis_summary") or "",
                "reasoning_en": "",
                "likely_prevailing_party_zh": repair_text(prediction.get("label")) or "综合判断",
                "likely_prevailing_party_en": "",
            },
        },
        "module_packet": history_module_packet,
        "history_reused": True,
        "remote_fetch": {"status": "history_reuse", "message": "当前页面直接复用已保存的预测结果。"},
        "coverage_note": "当前结果直接来自已保存历史，不会重新调用模型。",
        "retrieval_summary": {
            "keywords": keywords[:6],
            "law_count": len(history_item.get("legal_rules") or []),
            "case_count": len(history_item.get("supporting_case_rows") or []),
        },
    }


def _build_analysis_payload_from_history(history_item: dict, module_code: str) -> dict:
    """从历史记录构建分析负载，确保与预测页面数据一致"""
    keywords = []
    if history_item.get("case_type"):
        keywords.append(history_item["case_type"])
    for law in history_item.get("legal_rules") or []:
        title = repair_text(law.get("title"))
        if title and title not in keywords:
            keywords.append(title)
        if len(keywords) >= 6:
            break

    snapshot = history_item.get("result_snapshot") or {}
    analysis_snapshot = snapshot.get("analysis", {})

    # 构建 module_packet
    module_packet = _history_module_packet_from_display(history_item, module_code)

    # 从历史记录获取 supporting_case_groups 和 linked_laws
    supporting_case_groups = history_item.get("supporting_case_groups") or []
    linked_laws = history_item.get("legal_rules") or []

    # 如果 supporting_case_groups 为空，从 module_packet 中构建
    if not supporting_case_groups and module_packet.get("case_law_rows"):
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

    # 构建分析结果
    analysis_result = {
        "input_text": history_item.get("query_text") or "",
        "analysis": {
            "jurisdiction": history_item.get("court_level") or history_item.get("country") or "",
            "summary": history_item.get("analysis_summary") or history_item.get("case_summary") or "",
            "facts": history_item.get("case_summary") or history_item.get("query_text") or "",
            "disputed_issues": history_item.get("dispute_focus") or [],
            "requested_relief": analysis_snapshot.get("requested_relief") or "",
            "keywords": keywords,
            "legal_topics": analysis_snapshot.get("legal_topics") or [],
            "claims": analysis_snapshot.get("claims") or [],
            "risk_flags": [item.get("description") or item.get("name") for item in (history_item.get("risk_points") or []) if item.get("description") or item.get("name")],
        },
        "intake_outline": {
            "facts": history_item.get("case_summary") or history_item.get("query_text") or "",
            "disputed_issues": history_item.get("dispute_focus") or [],
            "requested_relief": analysis_snapshot.get("requested_relief") or "",
            "keywords": keywords,
        },
        "bilingual_context": {
            "facts": {"zh": history_item.get("case_summary") or history_item.get("query_text") or "", "en": ""},
            "disputed_issues": {"zh": history_item.get("dispute_focus") or [], "en": []},
            "requested_relief": {"zh": analysis_snapshot.get("requested_relief") or "", "en": ""},
            "keywords": {"zh": keywords, "en": []},
        },
        "module_packet": module_packet,
        "supporting_case_groups": supporting_case_groups,
        "linked_laws": linked_laws,
        "retrieval_summary": {
            "keywords": keywords[:6],
            "law_count": len(module_packet.get("relevant_laws", [])),
            "case_count": len(module_packet.get("case_law_rows", [])),
        },
        "data_readiness": {
            "status": "ready",
            "evidence": {
                "matched_laws": len(module_packet.get("relevant_laws", [])),
                "matched_cases": len(module_packet.get("case_law_rows", [])),
            },
            "corpus": {
                "canlii_total": 0,
            },
        },
        "analysis_mode": "history_reuse",
        "coverage_note": "当前结果基于已有历史记录，不会重复消耗分析时间。",
        "history_reused": True,
        "remote_fetch": {"status": "history_reuse", "message": "当前页面直接复用已保存的历史记录。"},
    }

    return analysis_result


def _normalize_filename_text(value: str, max_length: int = 56) -> str:
    raw = re.sub(r"\s+", " ", str(value or "").strip())
    raw = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9\\s-]", " ", raw)
    raw = re.sub(r"\s+", "-", raw).strip(" .-_")
    if len(raw) > max_length:
        raw = raw[:max_length].rstrip(" .-_")
    return raw or "case"


def _memo_download_filenames(context: dict) -> tuple[str, str]:
    prediction_result = context.get("prediction_result") or {}
    analysis = prediction_result.get("analysis", {}) if isinstance(prediction_result, dict) else {}
    summary = (
        analysis.get("summary")
        or prediction_result.get("input_text")
        or context.get("text_input")
        or ""
    )
    summary_part = _normalize_filename_text(summary)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    ascii_name = f"legal-memo-{timestamp}.pdf"
    pretty_name = f"legal-memo-{summary_part}-{timestamp}.pdf"
    return ascii_name, pretty_name


def _memo_context_payload(text: str, limit: int, offset: int, source: str, sort: str, module: str) -> dict:
    module_code = normalize_module(module)
    effective_source = resolve_source_for_module(module_code, source)
    payload = {
        "text_input": text,
        "limit": limit,
        "offset": offset,
        "source": effective_source,
        "sort": sort,
        "module_code": module_code,
        "module_profile": _presentation_module_profile(module_code),
        "source_options": _presentation_source_options(module_code),
        "prediction_result": None,
        "analysis_url": _analysis_url(text, limit, offset, effective_source, sort, module_code) if text.strip() else "/analysis",
        "predict_url": _predict_url(text, limit, offset, effective_source, sort, module_code) if text.strip() else "/predict",
        "memo_download_url": _memo_download_url(text, limit, offset, effective_source, sort, module_code) if text.strip() else "",
        "memo_date": datetime.now().strftime("%Y-%m-%d"),
    }
    if text.strip() and settings.direct_prediction_auto_enabled:
        payload["prediction_result"] = _sanitize_prediction_payload(
            predict_legal_outcome(
            text=text,
            limit=limit,
            offset=offset,
            source=effective_source,
            sort=sort,
            module=module_code,
            ),
            module_code,
        )
    elif text.strip():
        payload["page_notice"] = "直接预测已关闭。请通过庭审模拟完成陈述、举证、质证和评议后查看最终报告。"
    return payload


@router.get("/", response_class=HTMLResponse)
def root_page(request: Request):
    context = _base_context(request, "login")
    context.update({"next_url": "/", "auth_error": "", "login_value": "", "page_notice": ""})
    return _cache_safe_template("login.html", context)


@router.get("/home", response_class=HTMLResponse)
def index(request: Request):
    page_user = _require_page_user(request, "/home")
    if isinstance(page_user, RedirectResponse):
        return page_user
    context = _base_context(request, "home")
    context.update(
        {
            "module_code": "canada",
            "module_profile": _presentation_module_profile("canada"),
            "source_options": _presentation_source_options("canada"),
        }
    )
    return _cache_safe_template("index.html", context)


@router.get("/results", response_class=HTMLResponse)
def results_page(
    request: Request,
    keywords: str = Query(""),
    sync_first: str | None = Query(None),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    page_user = _require_page_user(
        request,
        f"/results?{_build_query_string(keywords=keywords, limit=limit, offset=offset, source=source, sort=sort, module=module, refresh='1' if refresh else None)}",
    )
    if isinstance(page_user, RedirectResponse):
        return page_user
    module_code = normalize_module(module)
    effective_source = resolve_source_for_module(module_code, source)
    result = search_and_optionally_sync(
        keywords_input=keywords,
        sync_first=_to_bool(sync_first),
        limit=limit,
        offset=offset,
        source=effective_source,
        sort=sort,
        module=module_code,
        refresh=refresh,
        origin_page="search",
    )

    context = _base_context(request, "search")
    context.update(result)
    context.update({
        "module_code": module_code,
        "module_profile": _presentation_module_profile(module_code),
        "source_options": _presentation_source_options(module_code),
        "source": effective_source,
    })
    context.update(
        _task_context(
            result.get("remote_fetch"),
            refresh_url=f"/results?{_build_query_string(keywords=keywords, limit=limit, offset=offset, source=effective_source, sort=sort, module=module_code, refresh='1')}",
        )
    )
    if keywords.strip():
        _remember_query(
            request,
            query_text=keywords,
            restore_url=f"/results?{_build_query_string(keywords=keywords, limit=limit, offset=offset, source=effective_source, sort=sort, module=module_code)}",
        )
    return _cache_safe_template("results.html", context)


def _render_prediction_page(
    *,
    request: Request,
    page_id: str,
    text: str,
    draft: str,
    limit: int,
    offset: int,
    source: str,
    sort: str,
    module: str,
    refresh: bool,
):
    active_text = text.strip()
    restore_text = active_text or draft
    page_path = "/analysis" if page_id == "analysis" else "/predict"
    next_query = _build_query_string(
        text=active_text if active_text else None,
        draft=draft if draft.strip() and not active_text else None,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    page_user = _require_page_user(
        request,
        f"{page_path}?{next_query}" if next_query else page_path,
    )
    if isinstance(page_user, RedirectResponse):
        return page_user
    module_code = normalize_module(module)
    effective_source = resolve_source_for_module(module_code, source)
    context = _base_context(request, page_id)
    context.update(
        {
            "text_input": restore_text,
            "submitted_text": active_text,
            "draft_text": draft,
            "limit": limit,
            "offset": offset,
            "source": effective_source,
            "sort": sort,
            "module_code": module_code,
            "module_profile": _presentation_module_profile(module_code),
            "source_options": _presentation_source_options(module_code),
            "refresh": refresh,
            "analysis_result": None,
            "analysis_view": None,
            "prediction_result": None,
            "analysis_url": _analysis_url(active_text, limit, offset, effective_source, sort, module_code) if active_text else "/analysis",
            "predict_url": _predict_url(active_text, limit, offset, effective_source, sort, module_code) if active_text else "/predict",
            "memo_download_url": _memo_download_url(active_text, limit, offset, effective_source, sort, module_code) if active_text else "",
            "history_id": 0,
        }
    )
    history_analysis_seed = None
    if active_text:
        if not refresh:
            existing_history_id, existing_history_item = _exact_history_display_for_query(
                user_id=int(page_user["id"]),
                query_text=active_text,
                module_code=module_code,
            )
            if existing_history_id and existing_history_item:
                if page_id == "analysis":
                    # 分析页面：使用历史记录数据构建分析视图，不再重定向到历史详情
                    analysis_payload = _build_analysis_payload_from_history(
                        existing_history_item, module_code
                    )
                    enrich_with_deep_analysis(
                        analysis_payload,
                        tenant_id=repair_text(page_user.get("tenant_id")),
                        user_id=int(page_user["id"]),
                    )
                    context["history_id"] = existing_history_id
                    context["analysis_result"] = analysis_payload
                    context["analysis_view"] = existing_history_item
                    _remember_query(
                        request,
                        query_text=active_text,
                        restore_url=f"{page_path}?{_build_query_string(text=active_text, limit=limit, offset=offset, source=effective_source, sort=sort, module=module_code)}",
                    )
                    return _cache_safe_template("analyze.html", context)
                # 预测页面：使用历史记录数据构建预测视图
                if _history_prediction_is_reusable(existing_history_item):
                    prediction_payload = _sanitize_prediction_payload(
                        _prediction_payload_from_history_display(existing_history_item, module_code),
                        module_code,
                    )
                    context["history_id"] = existing_history_id
                    context["prediction_result"] = prediction_payload
                    context["analysis_result"] = prediction_payload
                    context["analysis_view"] = existing_history_item
                    _remember_query(
                        request,
                        query_text=active_text,
                        restore_url=f"{page_path}?{_build_query_string(text=active_text, limit=limit, offset=offset, source=effective_source, sort=sort, module=module_code)}",
                    )
                    return _cache_safe_template("predict.html", context)
                history_analysis_seed = _build_analysis_payload_from_history(existing_history_item, module_code)
                if not (history_analysis_seed.get("module_packet") or {}).get("case_law_rows"):
                    history_analysis_seed = None
        existing_history_id = 0
        if page_id == "analysis":
            analysis_payload = analyze_sentence_search(
                text=active_text,
                limit=limit,
                offset=offset,
                source=effective_source,
                sort=sort,
                module=module_code,
                refresh=refresh,
                origin_page="analysis",
                local_only=False,
                tenant_id=repair_text(page_user.get("tenant_id")),
                user_id=int(page_user["id"]),
            )
            context["analysis_result"] = analysis_payload
            context.update(
                _task_context(
                    analysis_payload.get("remote_fetch"),
                    refresh_url=_analysis_url(active_text, limit, offset, effective_source, sort, module_code, refresh=True),
                )
            )
            # 保存分析到历史记录
            existing_history_id = _save_case_history_if_possible(
                request,
                query_text=active_text,
                module_code=module_code,
                result_payload=analysis_payload,
            )
            context["history_id"] = existing_history_id
            context["analysis_view"] = _sanitize_history_display(_build_analysis_view_payload(
                query_text=active_text,
                module_code=module_code,
                result_payload=analysis_payload,
                history_id=existing_history_id,
                user_id=int(page_user["id"]),
            ))
        else:
            prediction_payload = _sanitize_prediction_payload(
                predict_legal_outcome(
                text=active_text,
                limit=limit,
                offset=offset,
                source=effective_source,
                sort=sort,
                module=module_code,
                refresh=refresh,
                analysis_result=history_analysis_seed,
                ),
                module_code,
            )
            context["prediction_result"] = prediction_payload
            context["analysis_result"] = prediction_payload
            context.update(
                _task_context(
                    prediction_payload.get("remote_fetch"),
                    refresh_url=_predict_url(active_text, limit, offset, effective_source, sort, module_code, refresh=True),
                )
            )
            context["history_id"] = _save_case_history_if_possible(
                request,
                query_text=active_text,
                module_code=module_code,
                result_payload=prediction_payload,
            )
            context["analysis_view"] = _sanitize_history_display(_build_analysis_view_payload(
                query_text=active_text,
                module_code=module_code,
                result_payload=prediction_payload,
                history_id=context["history_id"],
                user_id=int(page_user["id"]),
            ))
            context["prediction_result"] = prediction_payload
        _remember_query(
            request,
            query_text=active_text,
            restore_url=f"{page_path}?{_build_query_string(text=active_text, limit=limit, offset=offset, source=effective_source, sort=sort, module=module_code)}",
        )
    template_name = "predict.html" if page_id == "predict" else "analyze.html"
    return _cache_safe_template(template_name, context)


@router.get("/analysis", response_class=HTMLResponse)
def analysis_page(
    request: Request,
    text: str = Query(""),
    draft: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    return _render_prediction_page(
        request=request,
        page_id="analysis",
        text=text,
        draft=draft,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh=refresh,
    )


@router.get("/analyze", response_class=HTMLResponse)
def analyze_page_redirect(
    text: str = Query(""),
    draft: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    redirect_query = _build_query_string(
        text=text if text.strip() else None,
        draft=draft if draft.strip() and not text.strip() else None,
        limit=limit,
        offset=offset,
        source=source,
        sort=sort,
        module=module,
        refresh="1" if refresh else None,
    )
    return RedirectResponse(
        url=f"/analysis?{redirect_query}" if redirect_query else "/analysis",
        status_code=307,
    )


@router.get("/hearing", response_class=HTMLResponse)
def hearing_page(request: Request):
    page_user = _require_page_user(request, "/hearing")
    if isinstance(page_user, RedirectResponse):
        return page_user
    return _cache_safe_template("hearing.html", _base_context(request, "hearing"))


@router.get("/predict", response_class=HTMLResponse)
def predict_page_redirect(
    request: Request,
    text: str = Query(""),
    draft: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    if settings.direct_prediction_ui_enabled:
        return _render_prediction_page(
            request=request, page_id="predict", text=text, draft=draft, limit=limit,
            offset=offset, source=source, sort=sort, module=module, refresh=refresh,
        )
    query = request.url.query
    return RedirectResponse(url=f"/hearing?{query}" if query else "/hearing", status_code=307)


@router.get("/law/canada/{law_slug}", response_class=HTMLResponse)
def canada_law_detail_page(
    request: Request,
    law_slug: str,
    refresh: bool = Query(False),
):
    page_user = _require_page_user(request, f"/law/canada/{quote(law_slug)}")
    if isinstance(page_user, RedirectResponse):
        return page_user
    detail = get_canada_law_detail_packet(law_slug, refresh=refresh)
    if not detail:
        raise HTTPException(status_code=404, detail="law detail not found")

    context = _base_context(request, "law_detail")
    context.update(
        {
            "module_code": "canada",
            "module_profile": _presentation_module_profile("canada"),
            "source_options": _presentation_source_options("canada"),
            "law_detail": detail,
            "refresh": refresh,
        }
    )
    return _cache_safe_template("law_detail.html", context)


@router.get("/memo", response_class=HTMLResponse)
def memo_page(
    request: Request,
    text: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
):
    page_user = _require_page_user(request, _memo_url(text, limit, offset, source, sort, module))
    if isinstance(page_user, RedirectResponse):
        return page_user
    return RedirectResponse(
        url=_analysis_url(text, limit, offset, resolve_source_for_module(normalize_module(module), source), sort, normalize_module(module)),
        status_code=307,
    )


@router.get("/memo/download")
def memo_download(
    request: Request,
    text: str = Query(""),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
):
    page_user = _require_page_user(request, _memo_download_url(text, limit, offset, source, sort, module))
    if isinstance(page_user, RedirectResponse):
        return page_user
    if not text.strip():
        raise HTTPException(status_code=400, detail="text is required for memo PDF export.")

    context = _base_context(request, "memo")
    context.update(_memo_context_payload(text, limit, offset, source, sort, normalize_module(module)))
    try:
        pdf_bytes = render_legal_memo_pdf(context)
    except PDFRenderError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    ascii_name, pretty_name = _memo_download_filenames(context)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(pretty_name)}"
            )
        },
    )


@router.post("/search")
async def search_redirect(request: Request):
    page_user = _require_page_user(request, "/home")
    if isinstance(page_user, RedirectResponse):
        return page_user
    form = await request.form()
    query = urlencode(
        {
            "keywords": form.get("keywords", ""),
            "sync_first": "on" if form.get("sync_first") else "",
            "limit": form.get("limit", settings.default_search_limit),
            "offset": form.get("offset", 0),
            "source": form.get("source", "all"),
            "sort": form.get("sort", "relevance"),
            "module": form.get("module", "canada"),
        }
    )
    return RedirectResponse(url=f"/results?{query}", status_code=303)


@router.get("/api/search")
def api_search(
    request: Request,
    keywords: str = Query(...),
    sync_first: bool = Query(False),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    require_user(request)
    return search_and_optionally_sync(
        keywords_input=keywords,
        sync_first=sync_first,
        limit=limit,
        offset=offset,
        source=resolve_source_for_module(normalize_module(module), source),
        sort=sort,
        module=normalize_module(module),
        refresh=refresh,
        origin_page="search",
    )


@router.get("/api/analyze-search")
def api_analyze_search(
    request: Request,
    text: str = Query(...),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    user = require_user(request)
    return analyze_sentence_search(
        text=text,
        limit=limit,
        offset=offset,
        source=resolve_source_for_module(normalize_module(module), source),
        sort=sort,
        module=normalize_module(module),
        refresh=refresh,
        origin_page="analyze",
        local_only=True,
        tenant_id=repair_text(user.get("tenant_id")),
        user_id=int(user["id"]),
    )


@router.get("/api/predict")
def api_predict(
    request: Request,
    text: str = Query(...),
    limit: int = Query(settings.default_search_limit),
    offset: int = Query(0),
    source: str = Query("all"),
    sort: str = Query("relevance"),
    module: str = Query("canada"),
    refresh: bool = Query(False),
):
    require_user(request)
    if not settings.direct_prediction_ui_enabled:
        raise HTTPException(status_code=410, detail="直接预测已关闭。请使用 /hearing 的受控庭审模拟流程。")
    return _sanitize_prediction_payload(
        predict_legal_outcome(
            text=text,
            limit=limit,
            offset=offset,
            source=resolve_source_for_module(normalize_module(module), source),
            sort=sort,
            module=normalize_module(module),
            refresh=refresh,
        ),
        normalize_module(module),
    )


@router.post("/api/agent-chat")
def api_agent_chat(request: Request, payload: ChatRequest):
    require_user(request)
    module_code = normalize_module(payload.module)
    effective_source = resolve_source_for_module(module_code, payload.source)
    analysis_result = analyze_sentence_search(
        text=payload.text,
        limit=payload.limit,
        offset=payload.offset,
        source=effective_source,
        sort=payload.sort,
        module=module_code,
        refresh=payload.refresh,
        origin_page="analyze",
        local_only=True,
    )
    answer = answer_module_question(
        module=module_code,
        question=payload.question,
        analysis_result=analysis_result,
        refresh=payload.refresh,
    )
    return {
        "module_code": module_code,
        "module_profile": _presentation_module_profile(module_code),
        "question": payload.question,
        "answer": answer,
        "analysis_result": {
            "input_text": analysis_result.get("input_text", ""),
            "analysis_mode": analysis_result.get("analysis_mode", ""),
            "intake_outline": analysis_result.get("intake_outline", {}),
            "module_packet": _sanitize_module_packet(analysis_result.get("module_packet", {}), module_code),
        },
    }


@router.get("/api/ingestion-tasks/{task_id}")
def api_ingestion_task(request: Request, task_id: int):
    require_user(request)
    task = get_ingestion_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="ingestion task not found")
    return task


@router.get("/api/archive/status")
def api_archive_status(request: Request):
    require_admin(request)
    return get_archive_status()


@router.get("/api/data-readiness")
def api_data_readiness(request: Request):
    require_user(request)
    return get_source_quality_snapshot()


@router.get("/api/rag/status")
def api_rag_status(request: Request):
    require_user(request)
    return get_rag_status()


@router.get("/api/rag/search")
def api_rag_search(
    request: Request,
    query: str = Query(...),
    module: str = Query("canada"),
    source: str = Query("all"),
    limit: int = Query(8),
    jurisdiction: str | None = Query(None),
    document_type: str | None = Query(None),
    court_level: str | None = Query(None),
    language: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    require_user(request)
    filters = _rag_structured_filters(jurisdiction, document_type, court_level, language, date_from, date_to)
    return rag_search(query, module=normalize_module(module), source_filter=source, limit=limit, filters=filters)


@router.get("/api/rag/vector-search")
def api_rag_vector_search(
    request: Request,
    query: str = Query(...),
    module: str = Query("canada"),
    source: str = Query("all"),
    limit: int = Query(8),
    jurisdiction: str | None = Query(None),
    document_type: str | None = Query(None),
    court_level: str | None = Query(None),
    language: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    require_user(request)
    filters = _rag_structured_filters(jurisdiction, document_type, court_level, language, date_from, date_to)
    return vector_search(query, module=normalize_module(module), source_filter=source, limit=limit, filters=filters)


@router.get("/api/rag/hybrid-search")
def api_rag_hybrid_search(
    request: Request,
    query: str = Query(...),
    module: str = Query("canada"),
    source: str = Query("all"),
    limit: int = Query(8),
    jurisdiction: str | None = Query(None),
    document_type: str | None = Query(None),
    court_level: str | None = Query(None),
    language: str | None = Query(None),
    date_from: str | None = Query(None),
    date_to: str | None = Query(None),
):
    require_user(request)
    filters = _rag_structured_filters(jurisdiction, document_type, court_level, language, date_from, date_to)
    return hybrid_search(query, module=normalize_module(module), source_filter=source, limit=limit, filters=filters)


@router.get("/api/skills")
def api_list_legal_skills(request: Request):
    require_user(request)
    return {"skills": list_legal_skills()}


@router.post("/api/skills/run")
def api_run_legal_skill(request: Request, payload: SkillRunPayload):
    require_user(request)
    try:
        return run_legal_skill(
            payload.skill_name,
            payload.query,
            filters=payload.filters,
            limit=payload.limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/multi-agent/run")
def api_run_multi_agent(request: Request, payload: MultiAgentRequest):
    user = require_user(request)
    tenant_id = repair_text(user.get("organization")) or f"user:{user['id']}"
    try:
        return run_multi_agent_analysis(payload, user_id=int(user["id"]), tenant_id=tenant_id)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/agent/collaborate")
def api_run_skill_collaboration(request: Request, payload: SkillCollaborationRequest):
    user = require_user(request)
    tenant_id = repair_text(user.get("organization")) or f"user:{user['id']}"
    try:
        return run_skill_collaboration(payload, user_id=int(user["id"]), tenant_id=tenant_id)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/agent/mcp-tools")
def api_list_mcp_tools(request: Request):
    require_user(request)
    return list_mcp_tools()


@router.post("/api/agent/mcp-tools/call")
def api_call_mcp_tool(request: Request, payload: MCPToolCall):
    user = require_user(request)
    tenant_id = repair_text(user.get("organization")) or f"user:{user['id']}"
    try:
        return call_readonly_mcp_tool(payload, user_id=int(user["id"]), tenant_id=tenant_id)
    except (PermissionError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _hearing_tenant_id(user: dict) -> str:
    return repair_text(user.get("organization")) or f"user:{user['id']}"


@router.post("/api/hearing/runs")
def api_create_hearing_run(request: Request, payload: HearingRunCreate):
    user = require_user(request)
    return create_hearing_run(payload, user_id=int(user["id"]), tenant_id=_hearing_tenant_id(user))


@router.get("/api/hearing/runs/{run_id}")
def api_get_hearing_run(request: Request, run_id: str):
    user = require_user(request)
    try:
        return get_hearing_run(run_id, tenant_id=_hearing_tenant_id(user))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/hearing/runs/{run_id}/turns")
def api_submit_hearing_turn(request: Request, run_id: str, payload: HearingTurnInput):
    user = require_user(request)
    try:
        return submit_hearing_turn(run_id, payload, user_id=int(user["id"]), tenant_id=_hearing_tenant_id(user))
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/hearing/runs/{run_id}/rollback/{stage_index}")
def api_rollback_hearing_run(request: Request, run_id: str, stage_index: int):
    user = require_user(request)
    try:
        return rollback_hearing_run(run_id, stage_index=stage_index, user_id=int(user["id"]), tenant_id=_hearing_tenant_id(user))
    except (LookupError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/rag/rebuild")
def api_rag_rebuild(
    request: Request,
    source: str = Query("all"),
    limit: int | None = Query(None),
):
    require_admin(request)
    return rebuild_rag_index(source_filter=source, limit=limit)


@router.get("/api/rag/vector-status")
def api_rag_vector_status(request: Request):
    require_user(request)
    return get_vector_status()


@router.post("/api/rag/rebuild-vectors")
def api_rag_rebuild_vectors(
    request: Request,
    source: str = Query("all"),
    module: str = Query("canada"),
    limit: int | None = Query(None),
):
    require_admin(request)
    return rebuild_chunk_embeddings(source_filter=source, limit=limit, module=normalize_module(module))


@router.post("/api/rag/rebuild-hybrid")
def api_rag_rebuild_hybrid(
    request: Request,
    source: str = Query("all"),
    module: str = Query("canada"),
    limit: int | None = Query(None),
):
    require_admin(request)
    return rebuild_hybrid_index(source_filter=source, limit=limit, module=normalize_module(module))


@router.post("/api/rag/export")
def api_rag_export(
    request: Request,
    source: str = Query("all"),
    limit: int | None = Query(None),
):
    require_admin(request)
    return export_rag_chunks(source_filter=source, limit=limit)


@router.get("/api/risk-training/samples")
def api_risk_training_samples(
    request: Request,
    limit: int = Query(100),
):
    require_admin(request)
    return {"items": list_risk_training_samples(limit=limit), "limit": limit}


@router.post("/api/risk-training/feedback")
def api_risk_training_feedback(request: Request, payload: RiskFeedbackPayload):
    require_admin(request)
    label_id = record_risk_feedback_label(
        sample_id=payload.sample_id,
        human_risk_level=payload.human_risk_level,
        human_outcome=payload.human_outcome,
        human_notes=payload.human_notes,
        label_json=payload.label_json,
    )
    return {"status": "ok", "label_id": label_id, "sample_id": payload.sample_id}


@router.post("/api/archive/export")
def api_archive_export(request: Request, source: str = Query("all")):
    require_admin(request)
    return export_source_items_snapshot(source_filter=source)


@router.post("/api/archive/rebuild")
def api_archive_rebuild(request: Request, source: str = Query("all")):
    require_admin(request)
    return rebuild_local_archive_from_db(source_filter=source)


@router.post("/api/sync/ofac")
def api_sync_ofac(request: Request):
    require_admin(request)
    return sync_ofac_demo()


@router.post("/api/sync/canlii")
def api_sync_canlii(request: Request):
    require_admin(request)
    return sync_canlii_demo()


@router.post("/api/sync/all")
def api_sync_all(request: Request):
    require_admin(request)
    return sync_all_sources()


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = Query("/"), message: str = Query("")):
    context = _base_context(request, "login")
    context.update(
        {
            "next_url": _normalize_next_url(next),
            "auth_error": str(message or "").strip(),
            "login_value": "",
            "page_notice": "",
        }
    )
    return _cache_safe_template("login.html", context)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, next: str = Query("/"), message: str = Query("")):
    context = _base_context(request, "register")
    context.update(
        {
            "next_url": _normalize_next_url(next),
            "auth_error": str(message or "").strip(),
            "form_values": {},
            "page_notice": "",
        }
    )
    return _cache_safe_template("register.html", context)


@router.post("/auth/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    login: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    try:
        user = authenticate_user(login, password)
        set_session_user(request, user)
        return RedirectResponse(url=_normalize_next_url(next, user), status_code=303)
    except HTTPException as exc:
        context = _base_context(request, "login")
        context.update(
            {
                "next_url": _normalize_next_url(next),
                "auth_error": str(exc.detail),
                "login_value": login,
                "page_notice": "",
            }
        )
        return _cache_safe_template("login.html", context, status_code=exc.status_code)


@router.post("/auth/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    username: str = Form(""),
    email: str = Form(""),
    password: str = Form(""),
    confirm_password: str = Form(""),
    next: str = Form("/"),
):
    if password != confirm_password:
        context = _base_context(request, "register")
        context.update(
            {
                "next_url": _normalize_next_url(next),
                "auth_error": "两次输入的密码不一致。",
                "form_values": {"username": username, "email": email},
                "page_notice": "",
            }
        )
        return _cache_safe_template("register.html", context, status_code=400)
    try:
        user = register_user(username=username, password=password, email=email)
        set_session_user(request, user)
        return RedirectResponse(url=_normalize_next_url(next, user), status_code=303)
    except HTTPException as exc:
        context = _base_context(request, "register")
        context.update(
            {
                "next_url": _normalize_next_url(next),
                "auth_error": str(exc.detail),
                "form_values": {"username": username, "email": email},
                "page_notice": "",
            }
        )
        return _cache_safe_template("register.html", context, status_code=exc.status_code)


@router.post("/auth/logout")
def logout_submit(request: Request):
    clear_session_user(request)
    return RedirectResponse(url="/login?message=" + quote("已退出登录，请重新登录"), status_code=303)


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request):
    user = _require_page_user(request, "/account")
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/home?message=" + quote("请点击右上角头像查看和修改个人信息"), status_code=303)


@router.post("/account/profile")
def account_profile_submit(
    request: Request,
    real_name: str = Form(""),
    phone: str = Form(""),
    organization: str = Form(""),
    country_preference: str = Form(""),
    legal_type_preference: str = Form(""),
    note: str = Form(""),
):
    user = _require_page_user(request, "/account")
    if isinstance(user, RedirectResponse):
        return user
    update_user_profile(
        int(user["id"]),
        real_name=real_name,
        phone=phone,
        organization=organization,
        country_preference=country_preference,
        legal_type_preference=legal_type_preference,
        note=note,
    )
    return RedirectResponse(url="/home?message=" + quote("个人信息已更新"), status_code=303)


@router.get("/histories", response_class=HTMLResponse)
def histories_page(
    request: Request,
    query_type: str = Query(""),
    case_type: str = Query(""),
    country: str = Query(""),
    court_level: str = Query(""),
    legal_type: str = Query(""),
    page: int = Query(1),
):
    user = _require_page_user(
        request,
        f"/histories?{_build_query_string(query_type=query_type, case_type=case_type, country=country, court_level=court_level, legal_type=legal_type)}",
    )
    if isinstance(user, RedirectResponse):
        return user

    page_size = 20
    current_page = max(1, page)
    offset = (current_page - 1) * page_size

    total_count = count_user_histories(
        user_id=int(user["id"]),
        query_type=query_type,
        case_type=case_type,
        country=country,
        court_level=court_level,
        legal_type=legal_type,
    )
    total_pages = max(1, (total_count + page_size - 1) // page_size)
    if current_page > total_pages:
        current_page = total_pages
        offset = (current_page - 1) * page_size

    histories = list_user_histories(
        user_id=int(user["id"]),
        query_type=query_type,
        case_type=case_type,
        country=country,
        court_level=court_level,
        legal_type=legal_type,
        limit=page_size,
        offset=offset,
    )

    context = _base_context(request, "histories")
    context.update(
        {
            "histories": [_sanitize_history_display(build_history_display_payload(item)) for item in histories],
            "filters": {
                "query_type": query_type,
                "case_type": case_type,
                "country": country,
                "court_level": court_level,
                "legal_type": legal_type,
            },
            "pagination": {
                "current_page": current_page,
                "total_pages": total_pages,
                "total_count": total_count,
                "page_size": page_size,
                "has_prev": current_page > 1,
                "has_next": current_page < total_pages,
            },
        }
    )
    return _cache_safe_template("histories.html", context)


@router.get("/histories/graph", response_class=HTMLResponse)
def histories_graph_page(
    request: Request,
    case_type: str = Query(""),
    country: str = Query(""),
    court_level: str = Query(""),
    legal_rule: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    limit: int = Query(200),
):
    user = _require_page_user(
        request,
        f"/histories/graph?{_build_query_string(case_type=case_type, country=country, court_level=court_level, legal_rule=legal_rule, start_date=start_date, end_date=end_date, limit=limit)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "histories_graph")
    context.update(
        {
            "graph_filters": {
                "case_type": case_type,
                "country": country,
                "court_level": court_level,
                "legal_rule": legal_rule,
                "start_date": start_date,
                "end_date": end_date,
                "limit": limit,
            },
            "history_graph_data": build_user_history_graph(
                user_id=int(user["id"]),
                case_type=case_type,
                country=country,
                court_level=court_level,
                legal_rule=legal_rule,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            ),
        }
    )
    context["history_graph_data"] = _sanitize_history_graph(context["history_graph_data"])
    return _cache_safe_template("histories_graph.html", context)


@router.get("/histories/{history_id}", response_class=HTMLResponse)
def history_detail_page(request: Request, history_id: int):
    user = _require_page_user(request, f"/histories/{history_id}")
    if isinstance(user, RedirectResponse):
        return user
    history = get_history(history_id, user_id=int(user["id"]), admin=bool(user.get("is_admin")), touch=False)
    if not history:
        raise HTTPException(status_code=404, detail="history not found")
    context = _base_context(request, "history_detail")
    history_item = _sanitize_history_display(build_history_display_payload(history))
    module_code = history_item.get("module_code") or ("us_sanctions" if history_item.get("country") == "United States" else "canada")
    predict_url = f"/predict?draft={quote(history_item.get('query_text') or '')}&module={quote(module_code)}"
    context.update(
        {
            "history_item": history_item,
            "can_predict": not bool(history_item.get("has_prediction")),
            "predict_url": predict_url,
        }
    )
    return _cache_safe_template("history_detail.html", context)


@router.get("/histories/{history_id}/restore")
def restore_history_page(request: Request, history_id: int):
    user = _require_page_user(request, f"/histories/{history_id}/restore")
    if isinstance(user, RedirectResponse):
        return user
    history = get_history(history_id, user_id=int(user["id"]), admin=bool(user.get("is_admin")), touch=False)
    if not history:
        raise HTTPException(status_code=404, detail="history not found")
    return RedirectResponse(url=f"/histories/{history_id}", status_code=303)


@router.get("/admin", response_class=HTMLResponse)
def admin_home(request: Request):
    user = _require_page_admin(request, "/admin")
    if isinstance(user, RedirectResponse):
        return user
    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/admin/users", response_class=HTMLResponse)
def admin_users_page(request: Request, selected_user_id: int | None = Query(None)):
    user = _require_page_admin(
        request,
        f"/admin/users?{_build_query_string(selected_user_id=selected_user_id)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "admin_users")
    context.update(
        {
            "admin_users": list_all_users(limit=300),
            "selected_user_id": selected_user_id,
            "selected_histories": list_all_histories(user_id=selected_user_id, limit=50) if selected_user_id else [],
        }
    )
    return _cache_safe_template("admin_users.html", context)


@router.get("/admin/histories", response_class=HTMLResponse)
def admin_histories_page(
    request: Request,
    user_id: int | None = Query(None),
    query_type: str = Query(""),
    country: str = Query(""),
    court_level: str = Query(""),
    legal_type: str = Query(""),
    rule_keyword: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
):
    user = _require_page_admin(
        request,
        f"/admin/histories?{_build_query_string(user_id=user_id, query_type=query_type, country=country, court_level=court_level, legal_type=legal_type, rule_keyword=rule_keyword, start_date=start_date, end_date=end_date)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "admin_histories")
    context.update(
        {
            "histories": list_all_histories(
                user_id=user_id,
                query_type=query_type,
                country=country,
                court_level=court_level,
                legal_type=legal_type,
                rule_keyword=rule_keyword,
                start_date=start_date,
                end_date=end_date,
                limit=300,
            ),
            "filters": {
                "user_id": user_id,
                "query_type": query_type,
                "country": country,
                "court_level": court_level,
                "legal_type": legal_type,
                "rule_keyword": rule_keyword,
                "start_date": start_date,
                "end_date": end_date,
            },
            "admin_users": list_all_users(limit=300),
        }
    )
    return _cache_safe_template("admin_histories.html", context)


@router.get("/admin/cases", response_class=HTMLResponse)
def admin_cases_page(
    request: Request,
    country: str = Query(""),
    case_type: str = Query(""),
    court_level: str = Query(""),
    rule_keyword: str = Query(""),
    keyword: str = Query(""),
    source_site: str = Query(""),
):
    user = _require_page_admin(
        request,
        f"/admin/cases?{_build_query_string(country=country, case_type=case_type, court_level=court_level, rule_keyword=rule_keyword, keyword=keyword, source_site=source_site)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "admin_cases")
    context.update(
        {
            "cases": list_cases(
                country=country,
                case_type=case_type,
                court_level=court_level,
                rule_keyword=rule_keyword,
                keyword=keyword,
                source_site=source_site,
                limit=300,
            ),
            "filters": {
                "country": country,
                "case_type": case_type,
                "court_level": court_level,
                "rule_keyword": rule_keyword,
                "keyword": keyword,
                "source_site": source_site,
            },
        }
    )
    return _cache_safe_template("admin_cases.html", context)


@router.get("/admin/rules", response_class=HTMLResponse)
def admin_rules_page(
    request: Request,
    country: str = Query(""),
    legal_type: str = Query(""),
    article_no: str = Query(""),
    keyword: str = Query(""),
):
    user = _require_page_admin(
        request,
        f"/admin/rules?{_build_query_string(country=country, legal_type=legal_type, article_no=article_no, keyword=keyword)}",
    )
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "admin_rules")
    context.update(
        {
            "rules": list_rules(
                country=country,
                legal_type=legal_type,
                article_no=article_no,
                keyword=keyword,
                limit=300,
            ),
            "filters": {
                "country": country,
                "legal_type": legal_type,
                "article_no": article_no,
                "keyword": keyword,
            },
        }
    )
    return _cache_safe_template("admin_rules.html", context)


@router.get("/admin/imports", response_class=HTMLResponse)
def admin_imports_page(request: Request):
    user = _require_page_admin(request, "/admin/imports")
    if isinstance(user, RedirectResponse):
        return user
    context = _base_context(request, "admin_imports")
    context.update({"import_tasks": list_import_tasks(limit=200)})
    return _cache_safe_template("admin_imports.html", context)


@router.post("/admin/import/manual")
def admin_import_manual_submit(
    request: Request,
    data_type: str = Form("case"),
    country: str = Form("Canada"),
    title: str = Form(""),
    legal_type: str = Form(""),
    case_type: str = Form(""),
    court_name: str = Form(""),
    court_level: str = Form(""),
    summary: str = Form(""),
    facts: str = Form(""),
    judgment_result: str = Form(""),
    article_no: str = Form(""),
    article_text: str = Form(""),
    article_summary: str = Form(""),
    source_url: str = Form(""),
    auto_link: bool = Form(False),
):
    user = _require_page_admin(request, "/admin/imports")
    if isinstance(user, RedirectResponse):
        return user
    import_manual_entry(
        created_by=int(user["id"]),
        data_type=data_type,
        country=country,
        title=title,
        legal_type=legal_type,
        case_type=case_type,
        court_name=court_name,
        court_level=court_level,
        summary=summary,
        facts=facts,
        judgment_result=judgment_result,
        article_no=article_no,
        article_text=article_text,
        article_summary=article_summary,
        source_url=source_url,
        auto_link=bool(auto_link),
    )
    return RedirectResponse(url="/admin/imports", status_code=303)


@router.post("/admin/import/url")
def admin_import_url_submit(
    request: Request,
    source_url: str = Form(""),
    country: str = Form("Canada"),
    data_type: str = Form("case"),
    legal_type: str = Form(""),
    case_type: str = Form(""),
    court_level: str = Form(""),
    auto_link: bool = Form(False),
):
    user = _require_page_admin(request, "/admin/imports")
    if isinstance(user, RedirectResponse):
        return user
    import_from_url(
        created_by=int(user["id"]),
        target_url=source_url,
        country=country,
        data_type=data_type,
        legal_type=legal_type,
        case_type=case_type,
        court_level=court_level,
        auto_link=bool(auto_link),
    )
    return RedirectResponse(url="/admin/imports", status_code=303)


@router.post("/admin/import/crawler")
def admin_import_crawler_submit(request: Request):
    user = _require_page_admin(request, "/admin/imports")
    if isinstance(user, RedirectResponse):
        return user
    run_canada_crawler_import(created_by=int(user["id"]))
    return RedirectResponse(url="/admin/imports", status_code=303)


@router.post("/api/auth/register")
def api_auth_register(request: Request, payload: AuthPayload):
    if payload.password != payload.confirmPassword:
        raise HTTPException(status_code=400, detail="两次输入的密码不一致。")
    user = register_user(
        username=payload.username,
        password=payload.password,
        email=payload.email,
    )
    set_session_user(request, user)
    return {
        "message": "注册成功",
        "user": user,
        "landing_url": _landing_url_for_user(user),
        "session_max_age": settings.session_max_age_seconds,
    }


@router.post("/api/auth/login")
def api_auth_login(request: Request, payload: LoginPayload):
    login_value = payload.login or payload.username or payload.email
    user = authenticate_user(login_value, payload.password)
    set_session_user(request, user)
    return {
        "message": "登录成功",
        "user": user,
        "landing_url": _landing_url_for_user(user),
        "session_max_age": settings.session_max_age_seconds,
    }


@router.post("/api/auth/logout")
def api_auth_logout(request: Request):
    clear_session_user(request)
    return {"status": "ok", "message": "已退出登录"}


@router.get("/api/auth/me")
def api_auth_me(request: Request):
    user = require_user(request)
    return {"user": user}


@router.put("/api/users/me")
def api_update_me(request: Request, payload: ProfilePayload):
    user = require_user(request)
    updated = update_user_profile(
        int(user["id"]),
        email=payload.email,
        phone=payload.phone,
        organization=payload.organization,
        real_name=payload.real_name,
        country_preference=payload.country_preference,
        legal_type_preference=payload.legal_type_preference,
        note=payload.note,
    )
    return {"message": "个人信息已更新", "user": updated}


@router.put("/api/users/me/password")
def api_update_my_password(request: Request, payload: PasswordPayload):
    user = require_user(request)
    if payload.new_password != payload.confirm_password:
        raise HTTPException(status_code=400, detail="两次输入的新密码不一致。")
    update_user_password(int(user["id"]), payload.current_password, payload.new_password)
    return {"message": "密码已更新"}


@router.post("/api/analyze")
def api_analyze(request: Request, payload: AnalyzePayload):
    user = require_user(request)
    module_code = normalize_module(payload.module)
    if not payload.refresh:
        existing_history_id, existing_history_item = _exact_history_display_for_query(
            user_id=int(user["id"]),
            query_text=payload.text,
            module_code=module_code,
        )
        if existing_history_id and existing_history_item:
            existing_history_item["history_id"] = existing_history_id
            history_payload = _build_analysis_payload_from_history(existing_history_item, module_code)
            enrich_with_deep_analysis(
                history_payload,
                tenant_id=repair_text(user.get("tenant_id")),
                user_id=int(user["id"]),
            )
            for field in ("deep_query_plan", "deep_analysis", "quality_gate", "deep_analysis_status"):
                if field in history_payload:
                    existing_history_item[field] = history_payload[field]
            return existing_history_item
    result = analyze_sentence_search(
        text=payload.text,
        limit=payload.limit,
        offset=payload.offset,
        source=resolve_source_for_module(module_code, payload.source),
        sort=payload.sort,
        module=module_code,
        refresh=payload.refresh,
        origin_page="analyze",
        local_only=False,
        tenant_id=repair_text(user.get("tenant_id")),
        user_id=int(user["id"]),
    )
    history_id = _save_case_history_if_possible(
        request,
        query_text=payload.text,
        module_code=module_code,
        result_payload=result,
    )
    if history_id:
        result["history_id"] = history_id
    response = _sanitize_history_display(_build_analysis_view_payload(
        query_text=payload.text,
        module_code=module_code,
        result_payload=result,
        history_id=history_id,
        user_id=int(user["id"]),
    ))
    for field in ("deep_query_plan", "deep_analysis", "quality_gate", "deep_analysis_status"):
        if field in result:
            response[field] = result[field]
    return response


@router.get("/api/histories")
def api_histories(request: Request):
    user = require_user(request)
    query_type = request.query_params.get("query_type", "")
    case_type = request.query_params.get("case_type", "")
    country = request.query_params.get("country", "")
    court_level = request.query_params.get("court_level", "")
    legal_type = request.query_params.get("legal_type", "")
    if user.get("is_admin") and request.query_params.get("all") == "1":
        return [_sanitize_history_display(build_history_display_payload(item)) for item in list_all_histories(
            query_type=query_type,
            country=country,
            court_level=court_level,
            legal_type=legal_type,
            limit=200,
        )]
    return [_sanitize_history_display(build_history_display_payload(item)) for item in list_user_histories(
        user_id=int(user["id"]),
        query_type=query_type,
        case_type=case_type,
        country=country,
        court_level=court_level,
        legal_type=legal_type,
        limit=200,
    )]


@router.get("/api/histories/graph")
def api_histories_graph(
    request: Request,
    case_type: str = Query(""),
    country: str = Query(""),
    court_level: str = Query(""),
    legal_rule: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
    limit: int = Query(200),
):
    user = require_user(request)
    return _sanitize_history_graph(build_user_history_graph(
        user_id=int(user["id"]),
        case_type=case_type,
        country=country,
        court_level=court_level,
        legal_rule=legal_rule,
        start_date=start_date,
        end_date=end_date,
        limit=limit,
    ))


@router.get("/api/histories/{history_id}")
def api_history_detail(request: Request, history_id: int):
    user = require_user(request)
    row = get_history(history_id, user_id=int(user["id"]), admin=bool(user.get("is_admin")), touch=False)
    if not row:
        raise HTTPException(status_code=404, detail="history not found")
    return _sanitize_history_display(build_history_display_payload(row))


@router.delete("/api/histories/{history_id}")
def api_history_delete(request: Request, history_id: int):
    user = require_user(request)
    deleted = delete_history(history_id, user_id=int(user["id"]), admin=bool(user.get("is_admin")))
    if not deleted:
        raise HTTPException(status_code=404, detail="history not found")
    return {"status": "deleted"}


@router.get("/api/histories/{history_id}/graph")
def api_history_graph(request: Request, history_id: int):
    user = require_user(request)
    history = get_history(history_id, user_id=int(user["id"]), admin=bool(user.get("is_admin")), touch=False)
    if not history:
        raise HTTPException(status_code=404, detail="history not found")
    return _sanitize_history_graph(build_user_history_graph(user_id=int(history.get("user_id") or user["id"]), limit=200))


@router.get("/api/cases")
def api_cases(
    request: Request,
    country: str = Query(""),
    case_type: str = Query(""),
    court_level: str = Query(""),
    rule_keyword: str = Query(""),
    keyword: str = Query(""),
    source_site: str = Query(""),
):
    require_user(request)
    return list_cases(
        country=country,
        case_type=case_type,
        court_level=court_level,
        rule_keyword=rule_keyword,
        keyword=keyword,
        source_site=source_site,
        limit=300,
    )


@router.get("/api/cases/{case_id}")
def api_case_detail(request: Request, case_id: int):
    require_user(request)
    row = get_case(case_id)
    if not row:
        raise HTTPException(status_code=404, detail="case not found")
    return row


@router.get("/api/rules")
def api_rules(
    request: Request,
    country: str = Query(""),
    legal_type: str = Query(""),
    article_no: str = Query(""),
    keyword: str = Query(""),
):
    require_user(request)
    return list_rules(
        country=country,
        legal_type=legal_type,
        article_no=article_no,
        keyword=keyword,
        limit=300,
    )


@router.get("/api/rules/{rule_id}")
def api_rule_detail(request: Request, rule_id: int):
    require_user(request)
    row = get_rule(rule_id)
    if not row:
        raise HTTPException(status_code=404, detail="rule not found")
    return row


@router.get("/api/case-rule-relations")
def api_case_rule_relations(
    request: Request,
    case_id: int | None = Query(None),
    rule_id: int | None = Query(None),
):
    require_user(request)
    return list_case_rule_relations(case_id=case_id, rule_id=rule_id, limit=500)


class VotePayload(BaseModel):
    vote_type: str
    reason: str = ""


@router.post("/api/cases/{case_id}/vote")
def api_case_vote(request: Request, case_id: int, payload: VotePayload):
    user = require_user(request)
    result = upsert_case_vote(
        case_id=case_id,
        user_id=int(user["id"]),
        vote_type=payload.vote_type,
        reason=payload.reason,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "vote failed"))
    votes = get_case_votes(case_id)
    return {"status": "ok", "votes": votes, "my_vote": payload.vote_type}


@router.delete("/api/cases/{case_id}/vote")
def api_case_delete_vote(request: Request, case_id: int):
    user = require_user(request)
    delete_case_vote(case_id=case_id, user_id=int(user["id"]))
    votes = get_case_votes(case_id)
    return {"status": "deleted", "votes": votes}


@router.get("/api/cases/{case_id}/votes")
def api_case_get_votes(request: Request, case_id: int):
    require_user(request)
    votes = get_case_votes(case_id)
    details = get_case_vote_details(case_id)
    return {"votes": votes, "details": details}


@router.get("/api/admin/users")
def api_admin_users(request: Request):
    require_admin(request)
    return list_all_users(limit=300)


@router.put("/api/admin/users/{user_id}/status")
def api_admin_user_status(request: Request, user_id: int, payload: UserStatusPayload):
    require_admin(request)
    return update_user_status(user_id, payload.status)


@router.get("/api/admin/histories")
def api_admin_histories(
    request: Request,
    user_id: int | None = Query(None),
    query_type: str = Query(""),
    country: str = Query(""),
    court_level: str = Query(""),
    legal_type: str = Query(""),
    rule_keyword: str = Query(""),
    start_date: str = Query(""),
    end_date: str = Query(""),
):
    require_admin(request)
    return list_all_histories(
        user_id=user_id,
        query_type=query_type,
        country=country,
        court_level=court_level,
        legal_type=legal_type,
        rule_keyword=rule_keyword,
        start_date=start_date,
        end_date=end_date,
        limit=500,
    )


@router.get("/api/admin/cases")
def api_admin_cases(
    request: Request,
    country: str = Query(""),
    case_type: str = Query(""),
    court_level: str = Query(""),
    rule_keyword: str = Query(""),
    keyword: str = Query(""),
    source_site: str = Query(""),
):
    require_admin(request)
    return list_cases(
        country=country,
        case_type=case_type,
        court_level=court_level,
        rule_keyword=rule_keyword,
        keyword=keyword,
        source_site=source_site,
        limit=500,
    )


@router.get("/api/admin/rules")
def api_admin_rules(
    request: Request,
    country: str = Query(""),
    legal_type: str = Query(""),
    article_no: str = Query(""),
    keyword: str = Query(""),
):
    require_admin(request)
    return list_rules(
        country=country,
        legal_type=legal_type,
        article_no=article_no,
        keyword=keyword,
        limit=500,
    )


@router.post("/api/admin/import/url")
def api_admin_import_url(request: Request, payload: ImportUrlPayload):
    user = require_admin(request)
    return import_from_url(
        created_by=int(user["id"]),
        target_url=payload.source_url,
        country=payload.country,
        data_type=payload.data_type,
        legal_type=payload.legal_type,
        case_type=payload.case_type,
        court_level=payload.court_level,
        auto_link=payload.auto_link,
    )


@router.post("/api/admin/import/manual")
def api_admin_import_manual(request: Request, payload: ImportManualPayload):
    user = require_admin(request)
    return import_manual_entry(
        created_by=int(user["id"]),
        data_type=payload.data_type,
        country=payload.country,
        title=payload.title,
        legal_type=payload.legal_type,
        case_type=payload.case_type,
        court_name=payload.court_name,
        court_level=payload.court_level,
        summary=payload.summary,
        facts=payload.facts,
        judgment_result=payload.judgment_result,
        article_no=payload.article_no,
        article_text=payload.article_text,
        article_summary=payload.article_summary,
        source_url=payload.source_url,
        auto_link=payload.auto_link,
    )


@router.post("/api/admin/crawler/run")
def api_admin_crawler_run(request: Request):
    user = require_admin(request)
    return run_canada_crawler_import(created_by=int(user["id"]))


@router.get("/api/admin/import/tasks")
def api_admin_import_tasks(request: Request):
    require_admin(request)
    return list_import_tasks(limit=300)


# 数据源管理API
@router.get("/api/data-sources")
def api_data_sources(request: Request):
    """获取所有可用的数据源"""
    require_user(request)
    from app.service.data_source_service import get_source_status
    return get_source_status()


@router.post("/api/data-sources/legislation")
def api_update_legislation_source(request: Request, source_type: str = Query(...)):
    """更新法规数据源"""
    require_admin(request)
    from app.service.data_source_service import update_legislation_source
    success = update_legislation_source(source_type)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid source type or missing configuration")
    return {"status": "updated", "source_type": source_type}


@router.post("/api/data-sources/case")
def api_update_case_source(request: Request, source_type: str = Query(...)):
    """更新案例数据源"""
    require_admin(request)
    from app.service.data_source_service import update_case_source
    success = update_case_source(source_type)
    if not success:
        raise HTTPException(status_code=400, detail="Invalid source type or missing configuration")
    return {"status": "updated", "source_type": source_type}


# 加拿大数据同步API
@router.post("/api/sync/canada-data")
def api_sync_canada_data(request: Request):
    """同步加拿大数据"""
    require_admin(request)
    from app.service.canada_data_service import sync_canada_data
    return sync_canada_data()


@router.get("/api/canada-data/stats")
def api_canada_data_stats(request: Request):
    """获取加拿大数据统计"""
    require_user(request)
    from app.service.canada_data_service import get_canada_data_stats
    return get_canada_data_stats()


# 关系分析API
@router.post("/api/relations/analyze")
def api_analyze_relations(request: Request):
    """分析所有案例与法规的关系"""
    require_admin(request)
    from app.service.relation_analysis_service import analyze_all_cases
    return analyze_all_cases()


@router.get("/api/relations/stats")
def api_relation_stats(request: Request):
    """获取关系统计"""
    require_user(request)
    from app.service.relation_analysis_service import get_relation_stats
    return get_relation_stats()
