import unittest
from unittest.mock import patch

from fastapi import HTTPException, Request

from app.api import routes
from app.service.analysis_stream_service import AnalysisStreamRun, _RUNS
from app.service.deep_analysis_service import build_deep_analysis_payload
from app.service.quality_gate_service import evaluate_quality_gate
from app.service.user_service import build_case_history_payload


CASE_TEXT = "An Ontario tenant received an N12 after notifying the landlord of mould."


def sample_result():
    plan, analysis = build_deep_analysis_payload(CASE_TEXT, use_llm=False, load_priors=False)
    return {
        "input_text": CASE_TEXT,
        "module_code": "canada",
        "analysis": {"summary": CASE_TEXT},
        "deep_query_plan": plan.model_dump(),
        "deep_analysis": analysis.model_dump(),
        "quality_gate": evaluate_quality_gate(analysis, audit=False).model_dump(),
        "deep_analysis_status": "downgraded",
        "module_packet": {},
    }


class AnalysisWorkspaceTests(unittest.TestCase):
    def request(self, path):
        return Request({"type": "http", "method": "GET", "path": path, "headers": [], "query_string": b""})

    def test_language_redirect_preserves_run_query(self):
        target = "/analysis?run=example&offset=20#analysis-result"
        response = routes.switch_language(self.request("/lang/en"), "en", target)
        self.assertEqual(response.status_code, 303)
        self.assertEqual(response.headers["location"], target)
        self.assertIn(f"{routes.LANG_COOKIE}=en", response.headers["Set-Cookie"])

    def test_deep_risks_survive_history_without_legacy_risk_flags(self):
        result = sample_result()
        row = build_case_history_payload(
            query_text=CASE_TEXT, analysis_payload=result, prediction_payload={},
            module_packet={}, module_code="canada",
        )
        self.assertTrue(row["risk_points_json"])
        restored = routes._build_analysis_payload_from_history(
            {"query_text": CASE_TEXT, "result_snapshot": row["result_snapshot"]}, "canada",
        )
        self.assertEqual(restored["deep_analysis"], result["deep_analysis"])
        self.assertEqual(restored["quality_gate"], result["quality_gate"])

    def test_result_fragment_is_owner_only_and_saves_once(self):
        run = AnalysisStreamRun(
            run_id="finished", user_id=7, tenant_id="user:7", status="completed",
            request={"text": CASE_TEXT, "module": "canada"}, result=sample_result(),
        )
        url = "/api/analysis-stream/runs/finished/view"
        with patch.dict(_RUNS, {run.run_id: run}), patch.object(routes, "require_user") as user, patch.object(
            routes, "_save_case_history_if_possible", return_value=31,
        ) as save, patch.object(routes, "_build_analysis_view_payload", return_value={
            "case_title": "Tenant notice", "query_text": CASE_TEXT,
        }):
            user.return_value = {"id": 8}
            with self.assertRaises(HTTPException) as caught:
                routes.api_view_analysis_stream_result(self.request(url), run.run_id)
            self.assertEqual(caught.exception.status_code, 404)
            user.return_value = {"id": 7}
            for _ in range(2):
                response = routes.api_view_analysis_stream_result(self.request(url), run.run_id)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["X-Analysis-History-Id"], "31")
                self.assertIn('data-risk-item="evidence"', response.body.decode())
                self.assertIn('id="verdict-conclusion"', response.body.decode())
                self.assertIn("no-store", response.headers["Cache-Control"])
            save.assert_called_once()
