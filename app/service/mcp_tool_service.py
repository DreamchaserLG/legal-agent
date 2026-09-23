from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from app.core.config import settings
from app.service.common_service import repair_text
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.module_service import normalize_module
from app.service.skill_collaboration_service import SkillCollaborationRequest, run_skill_collaboration
from app.service.skill_runtime_service import list_legal_skills, run_legal_skill


MCP_TOOL_VERSION = "local-readonly-1.0"


class MCPToolCall(BaseModel):
    tool_name: Literal[
        "legal_hybrid_search",
        "legal_authority_links",
        "legal_citation_verify",
        "legal_skill_collaboration",
    ]
    query: str = Field(min_length=1, max_length=20000)
    module: str = "canada"
    filters: dict[str, str] = Field(default_factory=dict)
    case_acl: list[str] = Field(default_factory=list, max_length=500)
    limit: int = Field(default=8, ge=1, le=12)


def _safe_filters(filters: dict[str, str]) -> dict[str, str]:
    allowed = {"jurisdiction", "document_type", "court_level", "language", "date_from", "date_to"}
    return {key: repair_text(value) for key, value in filters.items() if key in allowed and repair_text(value)}


def _allowed(item: dict[str, Any], case_acl: list[str]) -> bool:
    if not case_acl:
        return True
    allowed = {repair_text(value) for value in case_acl if repair_text(value)}
    candidates = {
        repair_text(item.get("doc_id")),
        repair_text(item.get("source_uid")),
        f"{repair_text(item.get('source_table'))}:{item.get('source_id')}",
        f"{repair_text(item.get('source_table'))}:{item.get('source_id')}:{item.get('chunk_id')}",
    }
    return bool(allowed.intersection(candidates))


def _apply_acl_to_skill_result(result: dict[str, Any], case_acl: list[str]) -> dict[str, Any]:
    if not case_acl:
        return result
    safe = dict(result)
    payload = dict(safe.get("result") or {})
    evidence = [item for item in payload.get("evidence") or [] if _allowed(item, case_acl)]
    payload["evidence"] = evidence
    if "formal_relations" in payload:
        # 现有正式关系结果不带可用于 ACL 反向校验的完整分块标识，白名单模式下宁可不返回。
        payload["formal_relations"] = []
        payload["formal_relations_status"] = "白名单模式下未返回无法逐项 ACL 校验的正式关联。"
    if "verdict" in payload:
        payload["verdict"] = "evidence_available" if evidence else "insufficient_evidence"
    safe["result"] = payload
    return safe


def list_mcp_tools() -> dict[str, Any]:
    return {
        "protocol_status": "mcp_ready_internal_adapter",
        "tool_version": MCP_TOOL_VERSION,
        "read_only": True,
        "tools": [
            {
                "name": "legal_hybrid_search",
                "description": "按结构化过滤检索本地法规和案例候选。",
                "required_context": ["tenant_id", "case_acl"],
            },
            {
                "name": "legal_authority_links",
                "description": "查询案例与法规的已有正式关联、匹配原因和分数。",
                "required_context": ["tenant_id", "case_acl"],
            },
            {
                "name": "legal_citation_verify",
                "description": "检查本地候选资料是否包含可追溯引文或来源。",
                "required_context": ["tenant_id", "case_acl"],
            },
            {
                "name": "legal_skill_collaboration",
                "description": "运行受预算、ACL 和 Skill 白名单约束的本地法律研究协作链。",
                "required_context": ["tenant_id", "case_acl"],
            },
        ],
        "available_skills": list_legal_skills(),
        "limitations": [
            "当前是 MCP 就绪的内部工具适配层，不是标准 MCP 传输服务。",
            "所有工具只读，不提供任意 SQL、文件、网络、导入、索引或写入能力。",
            "每次调用必须由应用层会话认证和租户/案件 ACL 上下文保护。",
        ],
    }


def call_readonly_mcp_tool(call: MCPToolCall, *, user_id: int | None, tenant_id: str) -> dict[str, Any]:
    if not settings.mcp_readonly_tools_enabled:
        return {"status": "disabled", "reason": "只读 MCP 工具适配层已由配置关闭。", "review_required": True}
    module = normalize_module(call.module)
    filters = _safe_filters(call.filters)
    context = {
        "tenant_id": repair_text(tenant_id) or "public",
        "case_acl_enforced": bool(call.case_acl),
        "read_only": True,
        "tool_version": MCP_TOOL_VERSION,
    }
    if call.tool_name == "legal_hybrid_search":
        result = hybrid_search(call.query, module=module, source_filter="all", limit=call.limit, filters=filters)
        result["items"] = [item for item in result.get("items") or [] if _allowed(item, call.case_acl)]
        result["total"] = len(result["items"])
    elif call.tool_name == "legal_authority_links":
        result = _apply_acl_to_skill_result(
            run_legal_skill("authority_linking", call.query, filters=filters, limit=call.limit),
            call.case_acl,
        )
    elif call.tool_name == "legal_citation_verify":
        result = _apply_acl_to_skill_result(
            run_legal_skill("evidence_verification", call.query, filters=filters, limit=call.limit),
            call.case_acl,
        )
    else:
        result = run_skill_collaboration(
            SkillCollaborationRequest(
                query=call.query,
                intent="research",
                module=module,
                filters=filters,
                case_acl=call.case_acl,
                max_skills=6,
            ),
            user_id=user_id,
            tenant_id=context["tenant_id"],
        )
    return {
        "status": result.get("status", "ok"),
        "tool_name": call.tool_name,
        "context": context,
        "result": result,
        "review_required": True,
        "disclaimer": "工具结果仅用于法律研究辅助，必须由具备相应法域资格的律师复核，不构成法律意见。",
    }
