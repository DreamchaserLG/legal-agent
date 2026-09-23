from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.service.mcp_tool_service as mcp_tools
import app.service.skill_collaboration_service as collaboration
from app.service.mcp_tool_service import MCPToolCall, call_readonly_mcp_tool, list_mcp_tools
from app.service.skill_collaboration_service import SkillCollaborationRequest, run_skill_collaboration


CASE = {
    "source_table": "legal_cases",
    "source_id": 101,
    "chunk_id": 1,
    "source_kind": "case",
    "title": "示例案例",
    "citation": "2020 ONCA 1",
    "source_url": "https://example.invalid/case",
}
LAW = {
    "source_table": "canada_laws",
    "source_id": 201,
    "chunk_id": 1,
    "source_kind": "law",
    "title": "示例法规",
    "citation": "RSC 1985, c C-46",
    "source_url": "https://example.invalid/law",
}


def fake_skill(skill_name, query, *, filters=None, limit=8):
    payloads = {
        "issue_fact_extraction": {"intake": {"missing_facts": ["关键事件日期"]}},
        "case_retrieval_ca": {"evidence": [CASE]},
        "statute_retrieval_ca": {"evidence": [LAW]},
        "authority_linking": {"evidence": [CASE, LAW], "formal_relations": [{"case_id": 101, "rule_id": 201, "match_score": 0.95}]},
        "evidence_verification": {"evidence": [CASE, LAW], "verdict": "evidence_available"},
        "risk_assessment_ca": {"confidence_band": "medium", "evidence": [CASE, LAW]},
    }
    return {"status": "ok", "run_id": len(skill_name), "result": payloads[skill_name]}


def main():
    collaboration.settings.agent_collaboration_enabled = True
    collaboration.run_legal_skill = fake_skill
    collaboration._persist = lambda *_args, **_kwargs: 7
    result = run_skill_collaboration(
        SkillCollaborationRequest(query="Ontario case risk research", intent="risk_analysis"),
        user_id=1,
        tenant_id="tenant-test",
    )
    assert result["status"] == "ok"
    assert [step["skill_name"] for step in result["steps"]] == [
        "issue_fact_extraction", "case_retrieval_ca", "statute_retrieval_ca",
        "authority_linking", "evidence_verification", "risk_assessment_ca",
    ]
    assert len(result["evidence"]) == 2
    assert result["authority_links"][0]["match_score"] == 0.95
    assert result["risks"]["confidence_band"] == "medium"

    mcp_tools.settings.mcp_readonly_tools_enabled = True
    mcp_tools.run_legal_skill = fake_skill
    catalog = list_mcp_tools()
    assert catalog["protocol_status"] == "mcp_ready_internal_adapter"
    tool_result = call_readonly_mcp_tool(
        MCPToolCall(tool_name="legal_authority_links", query="Ontario case", case_acl=["legal_cases:101:1"]),
        user_id=1,
        tenant_id="tenant-test",
    )
    assert tool_result["context"]["read_only"] is True
    assert tool_result["result"]["result"]["formal_relations"] == []
    assert tool_result["result"]["result"]["evidence"] == [CASE]
    print({"result": "success", "steps": len(result["steps"]), "tools": len(catalog["tools"])})


if __name__ == "__main__":
    main()
