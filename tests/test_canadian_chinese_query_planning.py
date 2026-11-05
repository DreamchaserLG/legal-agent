from __future__ import annotations

import unittest

from app.service.legal_query_planner_service import build_deep_query_plan
from app.service.query_translation_service import translate_query


class CanadianChineseQueryPlanningTests(unittest.TestCase):
    CASE = "安大略省租房，房东收到霉菌漏水维修通知后不维修，随后用N12自用收房，涉嫌报复性驱逐。"

    def test_chinese_canadian_terms_translate_to_corpus_vocabulary(self):
        result = translate_query(self.CASE)
        translated = result["translated"].lower()
        self.assertEqual(result["source_lang"], "zh")
        self.assertIn("ontario", translated)
        self.assertIn("residential tenancy", translated)
        self.assertIn("n12", translated)
        self.assertIn("retaliatory eviction", translated)

    def test_planner_extracts_tenancy_maintenance_and_n12_issues(self):
        plan = build_deep_query_plan(self.CASE, use_llm=False, load_priors=False)
        domains = {issue.legal_domain for issue in plan.dispute_issues}
        self.assertIn("Ontario residential tenancy", domains)
        self.assertIn("Ontario tenancy maintenance and remedies", domains)
        self.assertIn("Ontario N12 eviction and bad faith", domains)

    def test_filtered_law_queries_keep_doctrinal_terms(self):
        samples = {
            "显失公平交易与不当影响合同撤销": (
                "unconscionability", "undue influence", "rescission", "contract",
            ),
            "加拿大刑事量刑中醉酒、过失与既往记录的加重减轻": (
                "sentencing", "intoxication", "prior criminal record",
            ),
            "安大略房东以自住为由驱逐租客，善意要求与 s.57 救济": (
                "ontario", "landlord own use", "good faith", "eviction",
            ),
        }
        for query, expected_terms in samples.items():
            with self.subTest(query=query):
                translated = translate_query(query)["translated"].lower()
                for term in expected_terms:
                    self.assertIn(term, translated)


if __name__ == "__main__":
    unittest.main()
