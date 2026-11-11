import unittest
from unittest.mock import patch

from app.service.deep_analysis_service import build_deep_analysis_payload
from app.service.legal_query_planner_service import _llm_extract_facts, settings
from app.service.quality_gate_service import evaluate_quality_gate
from app.service.query_translation_service import translate_query


CIVIL_CASE = (
    "购买房产时提出要求之一是空气流通好，质量高。"
    "中介推荐的房产成交后，该房产地下室被发现曾种过大麻。卖方和中介是否构成欺诈？"
)
CRIMINAL_CASE = "个人在家中上网浏览儿童情色录像，并存储到个人电脑中，是否构成犯罪？"


class CaseAnalysisTests(unittest.TestCase):
    def analyze(self, text, **kwargs):
        return build_deep_analysis_payload(text, use_llm=False, load_priors=False, **kwargs)

    def test_real_estate_fraud_distinguishes_seller_and_broker(self):
        plan, analysis = self.analyze(CIVIL_CASE)
        self.assertEqual(len(analysis.dispute_details), 2)
        self.assertIn("房产卖方", analysis.dispute_details[0].issue)
        self.assertIn("房产中介", analysis.dispute_details[1].issue)
        self.assertEqual(plan.jurisdictions, [])
        self.assertTrue(any("管辖区" in warning for warning in plan.planning_warnings))

    def test_criminal_case_distinguishes_access_and_possession(self):
        plan, analysis = self.analyze(CRIMINAL_CASE)
        self.assertEqual(len(analysis.dispute_details), 2)
        self.assertIn("浏览或访问", analysis.dispute_details[0].issue)
        self.assertIn("保存或持有", analysis.dispute_details[1].issue)
        self.assertEqual(plan.jurisdictions, [])
        self.assertTrue(any("管辖区" in warning for warning in plan.planning_warnings))

    def test_fact_extraction_uses_case_model_and_rejects_unanchored_facts(self):
        response = {"data": {"facts": [
            {"fact_type": "action", "content": "浏览录像", "source_span": "浏览儿童情色录像"},
            {"fact_type": "action", "content": "对外传播", "source_span": "转发给别人"},
            {"fact_type": "document", "content": "电脑存储证明", "source_span": "个人电脑"},
            {"fact_type": "document", "content": "案情提到录像", "source_span": "儿童情色录像"},
        ]}}
        with patch.multiple(
            settings, deep_analysis_llm_model="qwen-flash", deep_analysis_llm_non_thinking=True,
            deep_analysis_timeout_seconds=8,
        ), patch("app.service.legal_query_planner_service.is_llm_configured", return_value=True), patch(
            "app.service.legal_query_planner_service.create_structured_response", return_value=response,
        ) as request:
            facts = _llm_extract_facts(CRIMINAL_CASE, timeout_seconds=30)
        self.assertEqual([fact.content for fact in facts], ["浏览录像", "案情提到录像"])
        self.assertEqual(request.call_args.kwargs["model"], "qwen-flash")
        self.assertIs(request.call_args.kwargs["enable_thinking"], False)
        self.assertEqual(request.call_args.kwargs["timeout_seconds"], 8)
        self.assertEqual(request.call_args.kwargs["temperature"], 0.0)

    def test_chinese_search_preserves_case_specific_terms(self):
        self.assertIn("cannabis grow operation", translate_query(CIVIL_CASE)["translated"])
        self.assertIn("child pornography", translate_query(CRIMINAL_CASE)["translated"])
        mixed = translate_query("卖方是否披露 real estate latent defect")["translated"]
        self.assertIn("real estate latent defect", mixed)

    def test_verified_relation_does_not_mark_rule_current(self):
        packet = {
            "relevant_laws": [{
                "rule_id": "rule-1",
                "title": "Synthetic real estate rule on seller disclosure",
                "linked_case_count": 1,
            }],
        }
        _, analysis = self.analyze(CIVIL_CASE, module_packet=packet)
        self.assertTrue(analysis.dispute_details[0].legal_basis)
        self.assertEqual(evaluate_quality_gate(analysis, audit=False).rule_currency, 0)

    def test_unrelated_research_is_not_used_to_support_an_issue(self):
        evidence = {
            "source_kind": "law",
            "evidence_id": "statute:1:1",
            "title": "Fisheries Act",
            "citation": "s. 1",
            "source_url": "https://example.invalid/fisheries",
        }
        _, analysis = self.analyze(CIVIL_CASE, retrieval_items=[evidence])
        self.assertTrue(all(not detail.legal_basis for detail in analysis.dispute_details))
        self.assertTrue(all(not risk.supporting_rule_ids for risk in analysis.risk_predictions))
        self.assertEqual(analysis.case_strength.legal_basis_sufficiency, 0)

    def test_criminal_case_links_official_section_for_review_when_corpus_lacks_it(self):
        _, analysis = self.analyze(CRIMINAL_CASE)
        self.assertEqual(len(analysis.references), 1)
        reference = analysis.references[0]
        self.assertIn("163.1", reference["citation"])
        self.assertTrue(reference["source_url"].startswith("https://laws-lois.justice.gc.ca/"))
        self.assertEqual(reference["verification_status"], "official_source_pending_legal_review")
        self.assertFalse(evaluate_quality_gate(analysis, audit=False).passed)

    def test_criminal_reference_excludes_reporting_law(self):
        evidence = [
            {
                "source_kind": "law",
                "evidence_id": "law:1",
                "title": "Internet Child Sexual Abuse Material Reporting Act",
                "citation": "s. 1",
                "excerpt": "This act mentions Criminal Code 163.1 in a reporting provision.",
            },
            {
                "source_kind": "law",
                "evidence_id": "law:2",
                "title": "Criminal Code",
                "citation": "s. 163.1",
            },
        ]
        _, analysis = self.analyze(CRIMINAL_CASE, retrieval_items=evidence)
        self.assertEqual([ref["reference_id"] for ref in analysis.references], ["law:2"])
        for detail in analysis.dispute_details:
            self.assertEqual([item["rule_id"] for item in detail.legal_basis], ["law:2"])

    def test_agentic_evidence_keeps_identity_and_citation(self):
        evidence = [
            {
                "source_kind": "law",
                "evidence_id": "statute:1:1",
                "title": "Synthetic real estate rule on seller disclosure",
                "citation": "s. 1",
                "source_url": "https://example.invalid/rule",
            },
            {
                "source_kind": "case",
                "evidence_id": "case:2:3",
                "title": "Synthetic case on real estate disclosure",
                "citation": "Synthetic neutral citation",
                "source_url": "https://example.invalid/case",
            },
        ]
        _, analysis = self.analyze(CIVIL_CASE, retrieval_items=evidence)
        detail = analysis.dispute_details[0]
        self.assertEqual(detail.legal_basis[0]["rule_id"], "statute:1:1")
        self.assertEqual(detail.case_support[0]["case_id"], "case:2:3")
        reference = next(item for item in analysis.references if item["reference_type"] == "case")
        self.assertEqual(reference["citation"], "Synthetic neutral citation")

    def test_employment_termination_does_not_add_ontario_n12_issue(self):
        plan, _ = self.analyze("A British Columbia employee seeks compensation after employment termination.")
        self.assertTrue(any("employment" in issue.legal_domain for issue in plan.dispute_issues))
        self.assertFalse(any("Ontario" in issue.legal_domain for issue in plan.dispute_issues))
        self.assertEqual(plan.jurisdictions, ["British Columbia"])


if __name__ == "__main__":
    unittest.main()
