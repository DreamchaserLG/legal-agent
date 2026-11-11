import unittest
from threading import Event
from unittest.mock import patch

from sqlalchemy import create_engine, text, event
from sqlalchemy.exc import OperationalError

from app.service import data_quality_service
from app.service import legal_agent_runtime_service as runtime
from app.service.civil_trial_workflow_service import _record_counsel_objection, _witness_credibility
from app.service.legal_agent_runtime_service import run_legal_agent_worker_forever
from app.service.legal_skill_service import guard_case_keywords
from app.service.module_service import retrieved_case_name
from app.service.pdf_service import _render_export_html, render_legal_memo_pdf
from app.service.user_service import build_case_history_payload, build_history_display_payload, _history_supporting_case_rows


class DefectRegressionTests(unittest.TestCase):
    def test_worker_continues_after_failed_database_poll(self):
        stop = Event()
        def process(_):
            if not stop.is_set() and poll.call_count == 1:
                raise OperationalError(None, None, OSError())
            stop.set()
            return True

        with patch.object(stop, "wait"), patch(
            "app.service.legal_agent_runtime_service.ensure_legal_agent_runtime_tables"
        ), patch(
            "app.service.legal_agent_runtime_service.process_next_legal_agent_run", side_effect=process,
        ) as poll:
            run_legal_agent_worker_forever(stop_event=stop)
        self.assertEqual(poll.call_count, 2)

    def test_run_claim_records_start_atomically_and_only_once(self):
        engine = create_engine("sqlite://")
        with patch.object(runtime, "engine", engine), patch.object(runtime, "is_sqlite", return_value=True), patch(
            "app.service.multi_agent_service.engine", engine,
        ), patch("app.service.multi_agent_service.is_sqlite", return_value=True), patch.object(
            runtime, "ensure_user_preference_tables",
        ):
            runtime.ensure_legal_agent_runtime_tables()
            with engine.begin() as conn:
                conn.execute(text(
                    "INSERT INTO multi_agent_runs (graph_version,tenant_id,intent,query_text,status,state_json,duration_ms) "
                    "VALUES (:version,'test','single_question','synthetic','queued','{}',0)"
                ), {"version": runtime.INTERACTIVE_GRAPH_VERSION})
            claimed = runtime._claim_next_run("worker-test")
            self.assertEqual(claimed["status"], "running")
            self.assertIsNotNone(claimed["started_at"])
            self.assertIsNone(runtime._claim_next_run("another-worker"))
            with engine.connect() as conn:
                self.assertEqual(conn.execute(text(
                    "SELECT COUNT(*) FROM multi_agent_run_events WHERE event_type='run_started'"
                )).scalar(), 1)

    def test_initial_submission_is_committed_with_run_and_events(self):
        engine = create_engine("sqlite://")
        with patch.object(runtime, "engine", engine), patch.object(runtime, "is_sqlite", return_value=True), patch(
            "app.service.multi_agent_service.engine", engine,
        ), patch("app.service.multi_agent_service.is_sqlite", return_value=True), patch.object(
            runtime, "ensure_user_preference_tables",
        ):
            runtime.ensure_legal_agent_runtime_tables()
            commits = []

            def inspect_commit(conn):
                submissions = conn.execute(text("SELECT COUNT(*) FROM multi_agent_human_submissions")).scalar()
                types = list(conn.execute(text("SELECT event_type FROM multi_agent_run_events ORDER BY id")).scalars())
                commits.append((submissions, types))

            event.listen(engine, "commit", inspect_commit)
            try:
                with patch.object(runtime, "ensure_legal_agent_runtime_tables"), patch.object(
                    runtime, "get_legal_agent_run", return_value={"id": 1},
                ), patch.object(runtime, "submit_human_turn", side_effect=AssertionError("Initial turn must be atomic")):
                    payload = runtime.InteractiveAgentRunCreate(
                        query="Synthetic tenancy dispute", initial_statement="Synthetic claimant statement",
                    )
                    runtime.enqueue_legal_agent_run(payload, user_id=1, tenant_id="test")
                self.assertEqual(commits, [(1, ["run_created", "human_submission_received"])])
            finally:
                event.remove(engine, "commit", inspect_commit)

    def test_enqueue_rolls_back_if_initial_events_cannot_be_recorded(self):
        engine = create_engine("sqlite://")
        with patch.object(runtime, "engine", engine), patch.object(runtime, "is_sqlite", return_value=True), patch(
            "app.service.multi_agent_service.engine", engine,
        ), patch("app.service.multi_agent_service.is_sqlite", return_value=True), patch.object(
            runtime, "ensure_user_preference_tables",
        ):
            runtime.ensure_legal_agent_runtime_tables()
            with patch.object(runtime, "ensure_legal_agent_runtime_tables"), patch.object(
                runtime, "_append_event", side_effect=RuntimeError("Synthetic failure"),
            ):
                with self.assertRaises(RuntimeError):
                    runtime.enqueue_legal_agent_run(runtime.InteractiveAgentRunCreate(
                        query="Synthetic dispute", initial_statement="Synthetic initial turn",
                    ), user_id=1, tenant_id="test")
            with engine.connect() as conn:
                self.assertEqual(conn.execute(text("SELECT COUNT(*) FROM multi_agent_runs")).scalar(), 0)

    def test_overview_uses_deep_issues_and_related_laws(self):
        issue = "Was the N12 issued in good faith?"
        query = "An Ontario tenant received an N12 after reporting mould to the landlord."
        row = build_case_history_payload(
            query_text=query, module_code="canada", module_packet={}, prediction_payload={},
            analysis_payload={
                "intake_outline": {"disputed_issues": ["vendor disclosure"]},
                "deep_analysis": {"dispute_details": [{"issue": issue, "legal_basis": [
                    {"title": "Ontario Forest Tenure Modernization Act, 2011"},
                    {"title": "Residential Tenancies Act, 2006", "article_no": "SO 2006, c 17"},
                ]}]},
            },
        )
        with patch("app.service.user_service._active_rule_lookup", return_value=(set(), set())):
            view = build_history_display_payload(row)
        self.assertEqual(view["dispute_focus"], [issue])
        self.assertEqual(view["case_type"], "住宅租赁")
        self.assertTrue(any("Residential Tenancies" in item for item in view["legal_relations"]))
        self.assertFalse(any("Forest Tenure" in item for item in view["legal_relations"]))

    def test_domain_guard_preserves_sale_and_rejects_contamination(self):
        candidates = ["vendor disclosure obligations", "residential tenancy", "substantial performance"]
        self.assertEqual(guard_case_keywords("房东收到租客的霉菌维修投诉后送达N12", candidates), candidates[1:])
        self.assertEqual(guard_case_keywords("购买房产后发现卖方隐瞒既往用途", candidates), candidates)
        self.assertEqual(guard_case_keywords("施工合同中的履约和抵销争议", candidates), candidates[1:])

    def test_source_breakdown_reconciles_without_counting_a2aj_as_canlii(self):
        engine = create_engine("sqlite://")
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE source_items (source_code TEXT, raw_json TEXT, raw_text TEXT)"))
            conn.execute(text("INSERT INTO source_items VALUES ('a2aj_case', NULL, NULL), ('laws_lois_xml', NULL, NULL), (NULL, NULL, NULL)"))
        with patch.object(data_quality_service, "engine", engine), patch.object(data_quality_service, "is_sqlite", return_value=True):
            counts = data_quality_service.get_source_quality_snapshot()
        self.assertEqual(counts["case_document_total"], 1)
        self.assertEqual(counts["legislation_total"], 1)
        self.assertEqual(counts["canlii_total"], 0)
        self.assertEqual(sum(counts["source_counts"].values()), counts["total_items"])

    def test_analysis_only_memo_renders_saved_content(self):
        result = _render_export_html({"memo_analysis": {
            "case_title": "<Test case>", "case_summary": "Test case facts", "dispute_focus": ["Notice validity"],
        }})
        self.assertIn("&lt;Test case&gt;", result)
        self.assertIn("Notice validity", result)
        self.assertNotIn("案件预测报告", result)

    def test_analysis_pdf_contains_chinese_and_requires_no_browser(self):
        with patch("app.service.pdf_service._pick_browser", side_effect=AssertionError("Browser should not be invoked")):
            document = render_legal_memo_pdf({"memo_analysis": {
                "case_title": "租赁案例", "case_summary": "房东收到维修投诉。<script>escaped text</script>",
                "dispute_focus": ["N12 自用通知是否善意"],
            }})
        self.assertTrue(document.startswith(b"%PDF"))
        self.assertGreater(len(document), 2000)

    def test_grouped_case_name_survives_history_projection(self):
        rows = _history_supporting_case_rows({}, [{
            "title": "Example statute",
            "cases": [{"title": "Elkins v. Van Wissen", "source_uid": "case:synthetic"}],
        }])
        self.assertEqual(rows[0]["case_name"], "Elkins v. Van Wissen")
        self.assertEqual(rows[0]["source_uid"], "case:synthetic")
        self.assertIsNone(rows[0]["case_id"])

    def test_case_name_metadata_and_bounded_excerpt(self):
        self.assertEqual(retrieved_case_name({"title": "2023 ONCA 789", "excerpt": "Elkins v. Van Wissen Collection Decisions Ontario"}), "Elkins v. Van Wissen")
        self.assertEqual(retrieved_case_name({"title": "2023 ONCA 789", "raw_json": {"case_name": "Metadata name"}}), "Metadata name")
        self.assertEqual(retrieved_case_name({"title": "2023 ONCA 789", "excerpt": "This court now turns to the facts."}), "")

    def test_evidence_objection_and_separate_cross_exam_scores(self):
        state = {
            "practice_side": "defendant", "exhibits": [], "sessions": [{"session_no": 1}],
            "objections": [], "transcript": [], "agent_trace": [],
        }
        task = {"phase": "plaintiff_evidence"}
        self.assertFalse(_record_counsel_objection(state, task, "The witness discussed hearsay exceptions."))
        self.assertTrue(_record_counsel_objection(state, task, "Objection, My Lord. Hearsay."))
        self.assertEqual(state["objections"][0]["raised_by"], "defendant_counsel")
        self.assertEqual(state["objections"][0]["grounds"], ["hearsay"])
        self.assertEqual([event["event_type"] for event in state["transcript"]], ["objection", "objection_ruling"])
        state["transcript"].extend([
            {"witness_id": "W1", "event_type": "cross_answer", "credibility_risk": "Did not have firsthand knowledge"},
            {"witness_id": "W2", "event_type": "cross_answer", "credibility_risk": "Could not recall timing"},
        ])
        a = _witness_credibility(state, {"witness_id": "W1", "name": "Witness One"})
        b = _witness_credibility(state, {"witness_id": "W2", "name": "Witness Two"})
        self.assertLess(a["score"], b["score"])
        self.assertNotEqual(a["reason"], b["reason"])
        self.assertIsNone(_witness_credibility(state, {"witness_id": "W3", "name": "Witness Three"})["score"])
