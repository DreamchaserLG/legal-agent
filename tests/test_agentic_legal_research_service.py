import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.service import agentic_legal_research_service as service


class AgenticLegalResearchTests(unittest.TestCase):
    def test_non_canadian_module_is_not_reinterpreted(self):
        result = service.run_agentic_legal_research("foreign dispute", module="other")
        self.assertEqual(result["status"], "unsupported_jurisdiction")
        self.assertEqual(result["evidence"], [])

    def test_gap_drives_second_round_and_stops_on_coverage(self):
        issue = SimpleNamespace(issue="negligence causation", legal_domain="tort")
        plan = SimpleNamespace(
            dispute_issues=[issue], rule_query="negligence statute", case_query="negligence case",
            model_dump=lambda **_: {"issues": ["negligence causation"]},
        )
        calls = []

        def search(query, **kwargs):
            calls.append(query)
            if "statute" in query or "elements" in query:
                return {"status": "ok", "items": [{"source_table": "laws", "source_id": 1, "chunk_id": 1, "source_kind": "law", "title": "Negligence causation statute", "excerpt": "negligence causation tort", "score": .9}]}
            return {"status": "ok", "items": [{"source_table": "cases", "source_id": 2, "chunk_id": 1, "source_kind": "case", "title": "Negligence causation case", "excerpt": "negligence causation tort", "score": .8}]}

        with patch.object(service, "build_deep_query_plan", return_value=plan), patch.object(service, "hybrid_search", side_effect=search):
            result = service.run_agentic_legal_research("injury", max_rounds=3, max_queries_per_round=3)
        self.assertEqual(result["stop_reason"], "all_issues_covered")
        self.assertEqual(result["coverage"][0]["status"], "covered")
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(result["external_text_policy"], "untrusted_data_only")


if __name__ == "__main__":
    unittest.main()
