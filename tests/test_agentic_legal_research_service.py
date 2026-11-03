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

    def test_query_events_are_observable_and_refinement_is_consumed_once(self):
        issue = SimpleNamespace(issue="negligence causation", legal_domain="tort")
        plan = SimpleNamespace(
            dispute_issues=[issue], rule_query="negligence statute", case_query="negligence case",
            model_dump=lambda **_: {"issues": ["negligence causation"]}, jurisdictions=[],
        )
        calls = []
        events = []
        control_calls = 0

        def search(query, **kwargs):
            calls.append(query)
            return {"status": "ok", "items": []}

        def control():
            nonlocal control_calls
            control_calls += 1
            if control_calls == 1:
                return {"revision": 1, "additional_queries": ["manual causation refinement"]}
            # An unrelated later control revision must not re-inject the query.
            return {"revision": control_calls, "additional_queries": []}

        def capture(event_type, message, payload):
            events.append((event_type, payload))

        with patch.object(service, "build_deep_query_plan", return_value=plan), patch.object(
            service, "hybrid_search", side_effect=search
        ), patch.object(service.settings, "legal_research_min_new_evidence", 0):
            service.run_agentic_legal_research(
                "injury", max_rounds=2, max_queries_per_round=4,
                event_callback=capture, control_callback=control,
            )

        self.assertEqual(calls.count("manual causation refinement"), 1)
        self.assertEqual(
            len([event for event, _ in events if event == "research_query_started"]),
            len(calls),
        )
        self.assertEqual(
            len([event for event, _ in events if event == "research_query_completed"]),
            len(calls),
        )
        for _, payload in events:
            self.assertIn("progress", payload)
            self.assertIn("current_phase", payload)
            self.assertIn("next_action", payload)

    def test_query_failure_and_cancelled_terminal_status_are_reported(self):
        issue = SimpleNamespace(issue="duty of care", legal_domain="tort")
        plan = SimpleNamespace(
            dispute_issues=[issue], rule_query="duty statute", case_query="duty case",
            model_dump=lambda **_: {"issues": ["duty of care"]}, jurisdictions=[],
        )
        events = []
        control_calls = 0

        def control():
            nonlocal control_calls
            control_calls += 1
            return {"cancelled": control_calls >= 2, "additional_queries": []}

        with patch.object(service, "build_deep_query_plan", return_value=plan), patch.object(
            service, "hybrid_search", side_effect=RuntimeError("local index unavailable")
        ):
            result = service.run_agentic_legal_research(
                "injury", max_rounds=2, max_queries_per_round=2,
                event_callback=lambda event, message, payload: events.append(event),
                control_callback=control,
            )

        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(result["stop_reason"], "cancelled_by_user")
        self.assertIn("research_query_failed", events)
        self.assertEqual(events[-1], "research_cancelled")


if __name__ == "__main__":
    unittest.main()
