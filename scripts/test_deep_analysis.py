from __future__ import annotations

from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine, text

from app.api import routes
import app.service.analysis_service as analysis_service
import app.service.deep_analysis_service as deep_analysis_service
from app.service.deep_analysis_service import (
    build_deep_analysis_payload,
)
from app.service.legal_query_planner_service import StructuredFact, build_deep_query_plan, build_legal_query_plan
import app.service.legal_query_planner_service as query_planner
from app.service.quality_gate_service import evaluate_quality_gate
import app.service.quality_gate_service as quality_gate_service


CASE_TEXT = (
    "2024年1月，房东与租客在安大略省签订一年期租赁合同。"
    "租客支付押金2000加元并按月支付租金。房屋随后持续漏水并出现霉菌，"
    "租客通过邮件和照片三次要求维修，房东没有处理。2024年5月，房东发出终止通知并要求腾退，"
    "租客认为通知是对维修投诉的报复，主张减租、返还维修支出并赔偿受损物品。"
)

EVIDENCE_PACKET = {
    "relevant_laws": [
        {
            "rule_id": 101,
            "title": "Residential Tenancies Act, 2006",
            "article_no": "SO 2006, c 17",
            "article_summary": "Landlord maintenance and termination duties.",
            "source_url": "https://example.test/law/101",
            "linked_case_count": 1,
        },
        {
            "rule_id": 102,
            "title": "Rules of Procedure",
            "article_no": "Rule 3",
            "article_summary": "Notice and filing requirements.",
            "source_url": "https://example.test/law/102",
            "linked_case_count": 1,
        },
    ],
    "case_law_rows": [
        {
            "case_id": 201,
            "title": "Tenant v Landlord",
            "court_level": "ONLTB",
            "judgment_date": "2023-03-01",
            "summary": "Maintenance, rent abatement, and termination notice dispute.",
            "source_url": "https://example.test/case/201",
        }
    ],
}


class QueryPlanTests(unittest.TestCase):
    def test_case_produces_traceable_facts_and_inferred_issues(self):
        plan = build_deep_query_plan(CASE_TEXT, use_llm=False, load_priors=False)
        self.assertGreaterEqual(len(plan.structured_facts), 5)
        self.assertGreaterEqual(len(plan.dispute_issues), 2)
        self.assertTrue(all(fact.source_span in plan.raw_case_text for fact in plan.structured_facts))
        self.assertTrue(all(issue.supporting_facts for issue in plan.dispute_issues))
        self.assertTrue(all(issue.issue not in CASE_TEXT for issue in plan.dispute_issues))
        self.assertNotEqual(plan.rule_query, " ".join(plan.keywords))

    def test_schema_rejects_out_of_range_confidence(self):
        with self.assertRaises(ValidationError):
            StructuredFact(fact_type="party", content="当事人", source_span="房东", confidence=1.1)

    def test_empty_long_and_non_language_inputs_degrade_without_error(self):
        empty = build_deep_query_plan("", use_llm=False, load_priors=False)
        self.assertEqual(empty.structured_facts, [])
        self.assertTrue(empty.planning_warnings)

        long_plan = build_deep_query_plan("合同违约。" * 5000, use_llm=False, load_priors=False)
        self.assertLessEqual(len(long_plan.raw_case_text), 20000)
        self.assertTrue(any("20000" in warning for warning in long_plan.planning_warnings))

        symbols = build_deep_query_plan("<script>alert(1)</script>'; DROP TABLE legal_cases; --", use_llm=False, load_priors=False)
        self.assertGreaterEqual(len(symbols.dispute_issues), 2)

        english = build_deep_query_plan("The employer dismissed the employee without notice or severance.", use_llm=False, load_priors=False)
        self.assertGreaterEqual(len(english.dispute_issues), 2)

    def test_llm_facts_are_schema_validated_and_traceable(self):
        llm_response = {
            "data": {
                "facts": [
                    {
                        "fact_type": "document",
                        "content": "租赁合同是已明确提及的书面材料",
                        "source_span": "租赁合同",
                        "confidence": 0.91,
                    }
                ]
            }
        }
        with patch.object(query_planner, "is_llm_configured", return_value=True), patch.object(
            query_planner, "create_structured_response", return_value=llm_response
        ):
            plan = build_deep_query_plan(CASE_TEXT, use_llm=True, load_priors=False)
        self.assertTrue(any(fact.content == "租赁合同是已明确提及的书面材料" for fact in plan.structured_facts))

    def test_invalid_llm_payload_falls_back_to_rules(self):
        invalid_response = {
            "data": {
                "facts": [
                    {
                        "fact_type": "document",
                        "content": "编造材料",
                        "source_span": "原文不存在的片段",
                        "confidence": 1.5,
                    }
                ]
            }
        }
        with patch.object(query_planner, "is_llm_configured", return_value=True), patch.object(
            query_planner, "create_structured_response", return_value=invalid_response
        ):
            plan = build_deep_query_plan(CASE_TEXT, use_llm=True, load_priors=False)
        self.assertGreaterEqual(len(plan.structured_facts), 5)
        self.assertTrue(any("规则化事实抽取" in warning for warning in plan.planning_warnings))

    def test_legacy_plan_fields_remain_available(self):
        plan = build_legal_query_plan("Ontario tenant repair", keywords=["repair"])
        for field in ("original_query", "law_query", "case_query", "domains", "issues", "preferred_sources"):
            self.assertIn(field, plan)
        self.assertIn("structured_facts", plan)


class DeepAnalysisTests(unittest.TestCase):
    def test_analysis_is_specific_and_not_an_input_copy(self):
        plan, analysis = build_deep_analysis_payload(
            CASE_TEXT,
            module_packet=EVIDENCE_PACKET,
            use_llm=False,
            load_priors=False,
        )
        self.assertNotEqual(analysis.case_summary, CASE_TEXT)
        self.assertIn("当事人与关系", analysis.case_summary)
        self.assertIn("时间线", analysis.case_summary)
        self.assertIn("核心行为", analysis.case_summary)
        self.assertIn("诉求与地域", analysis.case_summary)
        self.assertGreaterEqual(len(analysis.dispute_details), 2)
        self.assertEqual({risk.risk_type for risk in analysis.risk_predictions}, {"substantive", "procedural", "evidence", "enforcement"})
        self.assertTrue(all(0 <= value <= 1 for value in analysis.case_strength.model_dump().values() if isinstance(value, float)))
        self.assertTrue(analysis.disclaimer)
        self.assertTrue(plan.structured_facts)

    def test_missing_evidence_never_creates_authority_ids(self):
        _, analysis = build_deep_analysis_payload(
            "双方发生纠纷，但没有提供日期、文件、金额或具体经过。",
            module_packet={},
            use_llm=False,
            load_priors=False,
        )
        self.assertTrue(all(not item.supporting_rule_ids and not item.supporting_case_ids for item in analysis.risk_predictions))
        self.assertTrue(all(item.manual_review_required and item.confidence < 0.3 for item in analysis.risk_predictions))
        self.assertTrue(any("未检索到直接依据" in item.unresolved_gaps for item in analysis.dispute_details))


class QualityGateTests(unittest.TestCase):
    def test_low_quality_fails_first_traceability_check(self):
        _, analysis = build_deep_analysis_payload(
            "双方发生纠纷，没有提供其他资料。",
            module_packet={},
            use_llm=False,
            load_priors=False,
        )
        gate = evaluate_quality_gate(analysis, audit=False)
        self.assertFalse(gate.passed)
        self.assertEqual(gate.downgrade_reason, "引用可追溯率低于阈值")

    def test_evidence_bound_analysis_passes(self):
        _, analysis = build_deep_analysis_payload(
            CASE_TEXT,
            module_packet=EVIDENCE_PACKET,
            use_llm=False,
            load_priors=False,
        )
        gate = evaluate_quality_gate(analysis, audit=False)
        self.assertTrue(gate.passed)
        self.assertIsNone(gate.downgrade_reason)

    def test_audit_record_is_queryable(self):
        sqlite_engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
        _, analysis = build_deep_analysis_payload(
            CASE_TEXT,
            module_packet=EVIDENCE_PACKET,
            use_llm=False,
            load_priors=False,
        )
        with patch.object(quality_gate_service, "engine", sqlite_engine), patch.object(quality_gate_service, "is_sqlite", return_value=True):
            gate = evaluate_quality_gate(analysis, input_summary="audit-case", tenant_id="tenant-a", user_id=7, audit=True)
            with sqlite_engine.connect() as conn:
                row = conn.execute(text("SELECT tenant_id, user_id, passed FROM quality_gate_runs")).mappings().one()
        self.assertEqual(gate.gate_details["audit_status"], "recorded")
        self.assertEqual(row["tenant_id"], "tenant-a")
        self.assertEqual(row["user_id"], 7)
        self.assertEqual(bool(row["passed"]), gate.passed)


class PermissionRegressionTests(unittest.TestCase):
    def test_analyze_api_rejects_unauthenticated_request_before_analysis(self):
        with patch.object(routes, "require_user", side_effect=HTTPException(status_code=401, detail="login required")), patch.object(routes, "analyze_sentence_search") as analyze:
            with self.assertRaises(HTTPException) as raised:
                routes.api_analyze_search(
                    request=object(), text=CASE_TEXT, limit=5, offset=0,
                    source="all", sort="relevance", module="canada", refresh=False,
                )
        self.assertEqual(raised.exception.status_code, 401)
        analyze.assert_not_called()


class FeatureFlagIntegrationTests(unittest.TestCase):
    def test_disabled_flag_preserves_legacy_payload(self):
        payload = {"input_text": CASE_TEXT, "module_packet": EVIDENCE_PACKET}
        with patch.object(analysis_service.settings, "deep_analysis_enabled", False):
            result = analysis_service.enrich_with_deep_analysis(payload, audit=False)
        self.assertIs(result, payload)
        self.assertNotIn("deep_analysis", result)

    def test_enabled_flag_attaches_plan_analysis_and_gate(self):
        payload = {
            "input_text": CASE_TEXT,
            "retrieval_keywords": ["tenant", "repair"],
            "module_packet": EVIDENCE_PACKET,
            "rag_context": {"items": []},
        }

        def deterministic_builder(case_text, **kwargs):
            return deep_analysis_service.build_deep_analysis_payload(
                case_text,
                keywords=kwargs.get("keywords"),
                module_packet=kwargs.get("module_packet"),
                retrieval_items=kwargs.get("retrieval_items"),
                use_llm=False,
                load_priors=False,
            )

        with patch.object(analysis_service.settings, "deep_analysis_enabled", True), patch.object(
            analysis_service, "build_deep_analysis_payload", side_effect=deterministic_builder
        ):
            result = analysis_service.enrich_with_deep_analysis(payload, audit=False)
        self.assertEqual(result["deep_analysis_status"], "ready")
        self.assertIn("deep_query_plan", result)
        self.assertIn("deep_analysis", result)
        self.assertTrue(result["quality_gate"]["passed"])


class TemplateTests(unittest.TestCase):
    def test_passed_gate_renders_deep_sections_without_raw_input(self):
        _, analysis = build_deep_analysis_payload(
            CASE_TEXT,
            module_packet=EVIDENCE_PACKET,
            use_llm=False,
            load_priors=False,
        )
        gate = evaluate_quality_gate(analysis, audit=False)
        html = routes.templates.env.get_template("partials/deep_analysis_sections.html").render(
            analysis_result={"deep_analysis": analysis.model_dump(), "quality_gate": gate.model_dump()},
            history_id=0,
        )
        self.assertIn("争议焦点详情", html)
        self.assertIn("风险预测矩阵", html)
        self.assertIn("Residential Tenancies Act", html)
        self.assertNotIn(CASE_TEXT, html)

    def test_failed_gate_hides_full_analysis_sections(self):
        _, analysis = build_deep_analysis_payload(
            "双方发生纠纷，没有提供其他资料。",
            module_packet={},
            use_llm=False,
            load_priors=False,
        )
        gate = evaluate_quality_gate(analysis, audit=False)
        html = routes.templates.env.get_template("partials/deep_analysis_sections.html").render(
            analysis_result={"deep_analysis": analysis.model_dump(), "quality_gate": gate.model_dump()},
            history_id=0,
        )
        self.assertIn("深度解析未通过质量门禁", html)
        self.assertNotIn("争议焦点详情", html)
        self.assertNotIn("风险预测矩阵", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
