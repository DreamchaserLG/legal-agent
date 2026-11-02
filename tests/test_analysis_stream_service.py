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
            while run.status not in {"completed", "failed"} and time.monotonic() < deadline:
                service.wait_for_analysis_events(run, 0, timeout=0.05)

        self.assertEqual(run.status, "completed")
        self.assertEqual(run.result["module_packet"]["case_law_rows"], [1, 2])
        self.assertIn("research_round_completed", [event["event_type"] for event in run.events])
        self.assertEqual(run.events[-1]["event_type"], "run_completed")

    def test_run_is_scoped_to_owner(self):
        with patch.object(service, "analyze_sentence_search", return_value={}):
            created = service.create_analysis_stream_run(
                {"text": "Canadian legal question", "module": "canada"},
                user_id=7,
                tenant_id="user:7",
            )
        with self.assertRaises(ValueError):
            service.get_analysis_stream_run(created["run_id"], user_id=8)


if __name__ == "__main__":
    unittest.main()
