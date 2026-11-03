from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from app.service import analysis_stream_service as service


class AnalysisStreamServiceTests(unittest.TestCase):
    def test_background_run_publishes_progress_and_result(self):
        def fake_analysis(**kwargs):
            callback = kwargs["event_callback"]
            callback("case_structured", "facts ready", {"keywords": ["tenant"]})
            callback("research_round_completed", "round ready", {"new_evidence_count": 3})
            return {"module_packet": {"relevant_laws": [1], "case_law_rows": [1, 2]}}

        with patch.object(service, "analyze_sentence_search", side_effect=fake_analysis):
            created = service.create_analysis_stream_run(
                {"text": "Ontario tenant repair dispute", "module": "canada"},
                user_id=42,
                tenant_id="user:42",
            )
            run = service.get_analysis_stream_run(created["run_id"], user_id=42)
            deadline = time.monotonic() + 2
            while run.status not in {"completed", "failed", "cancelled"} and time.monotonic() < deadline:
                service.wait_for_analysis_events(run, 0, timeout=0.05)

        self.assertEqual(run.status, "completed")
        self.assertEqual(run.result["module_packet"]["case_law_rows"], [1, 2])
        self.assertIn("research_round_completed", [event["event_type"] for event in run.events])
        self.assertEqual(run.events[-1]["event_type"], "run_completed")
        for event in run.events:
            self.assertIn("progress", event)
            self.assertIn("current_phase", event)
            self.assertIn("next_action", event)
            self.assertEqual(event["progress"], event["payload"]["progress"])
        serialized = service.serialize_analysis_stream_run(run)
        self.assertEqual(serialized["progress"], 100.0)
        self.assertEqual(serialized["current_phase"], "completed")

    def test_run_is_scoped_to_owner(self):
        with patch.object(service, "analyze_sentence_search", return_value={}):
            created = service.create_analysis_stream_run(
                {"text": "Canadian legal question", "module": "canada"},
                user_id=7,
                tenant_id="user:7",
            )
        with self.assertRaises(ValueError):
            service.get_analysis_stream_run(created["run_id"], user_id=8)

    def test_owner_can_pause_refine_and_cancel(self):
        gate = service.threading.Event()

        def fake_analysis(**kwargs):
            gate.wait(1)
            return {}

        with patch.object(service, "analyze_sentence_search", side_effect=fake_analysis):
            created = service.create_analysis_stream_run(
                {"text": "Ontario N12 dispute", "module": "canada"}, user_id=9, tenant_id="user:9",
            )
            paused = service.apply_analysis_stream_control(created["run_id"], user_id=9, action="pause")
            refined = service.apply_analysis_stream_control(
                created["run_id"], user_id=9, action="refine", query="Ontario RTA section 57 bad faith",
            )
            cancelled = service.apply_analysis_stream_control(created["run_id"], user_id=9, action="cancel")
            gate.set()
            deadline = time.monotonic() + 2
            run = service.get_analysis_stream_run(created["run_id"], user_id=9)
            while run.status not in {"completed", "failed", "cancelled"} and time.monotonic() < deadline:
                service.wait_for_analysis_events(run, len(run.events), timeout=0.05)
        self.assertTrue(paused["control"]["paused"])
        self.assertIn("Ontario RTA section 57 bad faith", refined["control"]["additional_queries"])
        self.assertTrue(cancelled["control"]["cancelled"])
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.events[-1]["event_type"], "run_cancelled")

    def test_refinement_snapshot_is_consumed_once_but_history_remains_visible(self):
        run = service.AnalysisStreamRun(
            run_id="consume-once", user_id=1, tenant_id="user:1", request={},
        )
        run.additional_queries.append("Ontario limitation period")
        run.pending_additional_queries.append("Ontario limitation period")

        first = service.research_control_snapshot(run)
        second = service.research_control_snapshot(run)

        self.assertEqual(first["additional_queries"], ["Ontario limitation period"])
        self.assertEqual(second["additional_queries"], [])
        self.assertEqual(
            service.serialize_analysis_stream_run(run)["control"]["additional_queries"],
            ["Ontario limitation period"],
        )


if __name__ == "__main__":
    unittest.main()
