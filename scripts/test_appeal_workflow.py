from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader
from pydantic import ValidationError
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.pool import StaticPool
from starlette.middleware.sessions import SessionMiddleware

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.api.routes as routes
import app.service.appeal_workflow_service as appeal
from app.service.appeal_workflow_service import (
    APPEAL_STAGES,
    AppealAdvanceInput,
    AppealAgentTurn,
    AppealClaim,
    AppealRunCreate,
    advance_appeal_run,
    create_appeal_run,
    get_appeal_run,
    rollback_appeal_run,
)


AUTHORITY_FIXTURE = [
    {
        "authority_id": "A-001",
        "source_key": "legal_cases:10:100",
        "authority_type": "case",
        "title": "Housen v. Nikolaisen",
        "citation": "2002 SCC 33",
        "court": "SCC",
        "decision_date": "2002-03-28",
        "source_url": "https://example.invalid/housen",
        "excerpt": "Questions of law and fact attract different standards of review.",
        "score": 0.95,
    },
    {
        "authority_id": "A-002",
        "source_key": "legal_rules:20:200",
        "authority_type": "law",
        "title": "Courts of Justice Act",
        "citation": "RSO 1990, c C.43",
        "court": "",
        "decision_date": "",
        "source_url": "https://example.invalid/statute",
        "excerpt": "Ontario appeal jurisdiction fixture.",
        "score": 0.9,
    },
]


def payload(*, practice_role: str = "observer", record_materials: str = "Exhibit 1 contract\nTrial transcript page 42") -> AppealRunCreate:
    return AppealRunCreate(
        case_title="Doe v. Example Ltd.",
        case_summary="The appellant challenges an Ontario civil judgment concerning a terminated services agreement.",
        lower_court_decision="The trial judge dismissed the contract claim after finding that no enforceable promise was proven.",
        grounds_of_appeal="The trial judge applied the wrong legal test for contract formation.\nThe trial judge made a palpable and overriding factual error about the signed agreement.",
        requested_order="Set aside the judgment and order a new trial.",
        record_materials=record_materials,
        practice_role=practice_role,
    )


class AppealWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.engine = create_engine(
            "sqlite+pysqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
            future=True,
        )
        cls.original_engine = appeal.engine
        cls.original_is_sqlite = appeal.is_sqlite
        cls.original_authorities = appeal._authority_candidates
        cls.original_llm_configured = appeal.is_llm_configured
        cls.original_structured = appeal.create_structured_response
        appeal.engine = cls.engine
        appeal.is_sqlite = lambda: True
        appeal._authority_candidates = lambda _query: [dict(item) for item in AUTHORITY_FIXTURE]
        appeal.is_llm_configured = lambda: False
        appeal.ensure_appeal_workflow_tables()

    @classmethod
    def tearDownClass(cls) -> None:
        appeal.engine = cls.original_engine
        appeal.is_sqlite = cls.original_is_sqlite
        appeal._authority_candidates = cls.original_authorities
        appeal.is_llm_configured = cls.original_llm_configured
        appeal.create_structured_response = cls.original_structured
        cls.engine.dispose()

    def tenant(self, suffix: str) -> str:
        return f"appeal-test-{self._testMethodName}-{suffix}"

    def create(self, *, role: str = "observer", suffix: str = "a", record_materials: str = "Exhibit 1 contract") -> dict:
        return create_appeal_run(
            payload(practice_role=role, record_materials=record_materials),
            user_id=7,
            tenant_id=self.tenant(suffix),
        )

    def advance(self, run: dict, *, suffix: str = "a", body: AppealAdvanceInput | None = None) -> dict:
        return advance_appeal_run(
            run["id"],
            body or AppealAdvanceInput(),
            user_id=7,
            tenant_id=self.tenant(suffix),
        )

    def test_create_builds_ontario_record_issues_and_authorities(self):
        run = self.create()
        self.assertEqual(run["jurisdiction"], "Ontario")
        self.assertEqual(run["appeal_type"], "civil")
        self.assertEqual(run["active_stage"], "record_review")
        self.assertEqual(len(run["stage_nodes"]), len(APPEAL_STAGES))
        self.assertEqual({item["expected_standard"] for item in run["issues"]}, {"correctness", "palpable_and_overriding_error"})
        self.assertTrue(all(item["record_id"].startswith("R-") for item in run["record"]))
        self.assertEqual([item["authority_id"] for item in run["authorities"]], ["A-001", "A-002"])

    def test_chinese_wrong_legal_test_uses_correctness_standard(self):
        chinese_payload = AppealRunCreate(
            case_title="张某诉装修公司合同纠纷上诉",
            case_summary="张某主张原审错误处理住宅装修合同项下的损害赔偿请求，现就该民事判决提起上诉。",
            lower_court_decision="原审仅支持部分修复费用，并驳回张某提出的其余损害赔偿请求。",
            grounds_of_appeal="原审采用了错误的损害赔偿法律测试，应按正确性标准审查。\n原审对验收记录的认定存在明显且具有决定性的错误。",
            requested_order="撤销原判并发回重审。",
            practice_role="observer",
        )
        run = create_appeal_run(
            chinese_payload,
            user_id=7,
            tenant_id=self.tenant("chinese-standard"),
        )

        self.assertEqual(run["issues"][0]["question_type"], "question_of_law")
        self.assertEqual(run["issues"][0]["expected_standard"], "correctness")
        self.assertEqual(run["issues"][1]["expected_standard"], "palpable_and_overriding_error")

    def test_full_observer_workflow_completes_in_fixed_order(self):
        run = self.create()
        visited = []
        for _ in APPEAL_STAGES:
            visited.append(run["active_stage"])
            run = self.advance(run)
        self.assertEqual(visited, APPEAL_STAGES)
        self.assertEqual(run["status"], "completed")
        self.assertEqual([item["speaker_role"] for item in run["turns"]], [
            "appellant_counsel", "judge_panel", "appellant_counsel",
            "respondent_counsel", "judge_panel", "respondent_counsel", "appellant_counsel",
        ])
        self.assertEqual(len(run["scores"]), 5)
        self.assertFalse(run["final_report"]["display_probability"])
        self.assertIsNone(run["final_report"]["probability"])
        self.assertEqual(run["final_report"]["invalid_reference_count"], 0)
        self.assertEqual(run["final_report"]["role_compliance"], 1.0)

    def test_full_appellant_practice_workflow_completes(self):
        run = self.create(role="appellant")
        while run["status"] == "active":
            body = AppealAdvanceInput()
            if run["requires_user_input"]:
                body = AppealAdvanceInput(
                    content="The appellant addresses the identified issue, applies correctness or palpable and overriding error as required, cites the record, and requests a new trial.",
                    issue_ids=["I-001"], record_ids=["R-001", "R-002"], authority_ids=["A-001"],
                )
            run = self.advance(run, body=body)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(sum(item["source"] == "user" for item in run["turns"]), 3)

    def test_full_respondent_practice_workflow_completes(self):
        run = self.create(role="respondent")
        while run["status"] == "active":
            body = AppealAdvanceInput()
            if run["requires_user_input"]:
                body = AppealAdvanceInput(
                    content="The respondent submits that the appellant has not shown a palpable and overriding error and that the record supports dismissal of the appeal.",
                    issue_ids=["I-001"], record_ids=["R-001", "R-002"], authority_ids=["A-001"],
                )
            run = self.advance(run, body=body)
        self.assertEqual(run["status"], "completed")
        self.assertEqual(sum(item["source"] == "user" for item in run["turns"]), 2)

    def test_practice_role_requires_submission_only_on_its_stages(self):
        run = self.create(role="appellant")
        run = self.advance(run)
        self.assertTrue(run["requires_user_input"])
        with self.assertRaisesRegex(ValueError, "请提交陈述内容"):
            self.advance(run)
        stored = get_appeal_run(run["id"], tenant_id=self.tenant("a"))
        self.assertEqual(stored["active_stage"], "appellant_main")

    def test_valid_user_submission_is_scored_and_traceable(self):
        run = self.create(role="appellant")
        run = self.advance(run)
        run = self.advance(
            run,
            body=AppealAdvanceInput(
                content="The trial judge applied the wrong legal test. Correctness applies, and the signed agreement in the record supports a new trial.",
                issue_ids=["I-001"],
                record_ids=["R-001", "R-005"],
                authority_ids=["A-001"],
            ),
        )
        turn = run["turns"][-1]
        self.assertEqual(turn["source"], "user")
        self.assertEqual(turn["verification"]["claim_checks"][0]["status"], "record_and_authority")
        self.assertGreater(turn["score"]["total"], 50)

    def test_unknown_record_id_is_rejected_without_advancing(self):
        run = self.create(role="appellant")
        run = self.advance(run)
        with self.assertRaisesRegex(ValueError, "案卷外记录"):
            self.advance(run, body=AppealAdvanceInput(content="Submission", issue_ids=["I-001"], record_ids=["R-999"], authority_ids=["A-001"]))
        stored = get_appeal_run(run["id"], tenant_id=self.tenant("a"))
        self.assertEqual(stored["active_stage"], "appellant_main")
        self.assertEqual(stored["turns"], [])

    def test_unknown_authority_and_issue_ids_are_rejected(self):
        run = self.create(role="appellant")
        run = self.advance(run)
        with self.assertRaisesRegex(ValueError, "未知争点"):
            self.advance(run, body=AppealAdvanceInput(content="Submission", issue_ids=["I-999"], record_ids=["R-001"], authority_ids=["A-999"]))

    def test_judge_cannot_submit_claims_or_requested_order(self):
        run = self.create()
        run = self.advance(run)
        run = self.advance(run)
        state = appeal._load(run["id"], self.tenant("a"))
        bad = AppealAgentTurn(
            speaker_role="judge_panel",
            content="The appeal should be allowed.",
            claims=[AppealClaim(statement="Allow appeal", record_ids=["R-001"], authority_ids=["A-001"])],
            requested_order="Allow appeal",
        )
        with self.assertRaisesRegex(ValueError, "合议庭只能提问"):
            appeal._validate_turn(state, "judge_question_appellant", bad)

    def test_answer_must_reference_existing_judge_question(self):
        run = self.create()
        run = self.advance(run)
        run = self.advance(run)
        state = appeal._load(run["id"], self.tenant("a"))
        bad = AppealAgentTurn(
            speaker_role="appellant_counsel",
            content="Answer without question reference.",
            issue_ids=["I-001"],
            claims=[AppealClaim(statement="Answer", record_ids=["R-001"], authority_ids=["A-001"])],
            record_ids=["R-001"], authority_ids=["A-001"],
        )
        with self.assertRaisesRegex(ValueError, "必须明确对应"):
            appeal._validate_turn(state, "appellant_answer", bad)

    def test_appellant_reply_cannot_add_issue_not_raised_by_respondent(self):
        run = self.create()
        for _ in range(7):
            run = self.advance(run)
        state = appeal._load(run["id"], self.tenant("a"))
        state["turns"] = [item for item in state["turns"] if item["speaker_role"] != "respondent_counsel"]
        bad = AppealAgentTurn(
            speaker_role="appellant_counsel",
            content="New reply issue.", issue_ids=["I-002"],
            claims=[AppealClaim(statement="New issue", record_ids=["R-001"], authority_ids=["A-001"])],
            record_ids=["R-001"], authority_ids=["A-001"], requested_order="New trial",
        )
        with self.assertRaisesRegex(ValueError, "答复越界"):
            appeal._validate_turn(state, "appellant_reply", bad)

    def test_invalid_llm_output_falls_back_to_bounded_turn(self):
        run = self.create()
        run = self.advance(run)
        original_flag = appeal.is_llm_configured
        original_call = appeal.create_structured_response
        appeal.is_llm_configured = lambda: True
        appeal.create_structured_response = lambda *_args, **_kwargs: {"data": {
            "speaker_role": "appellant_counsel", "content": "Invented source", "issue_ids": ["I-999"],
            "claims": [{"statement": "Invented", "record_ids": ["R-999"], "authority_ids": ["A-999"]}],
            "record_ids": ["R-999"], "authority_ids": ["A-999"], "responds_to": [], "question_ids": [],
            "requested_order": "Allow", "stop_reason": "turn_complete",
        }}
        try:
            run = self.advance(run)
        finally:
            appeal.is_llm_configured = original_flag
            appeal.create_structured_response = original_call
        turn = run["turns"][-1]
        self.assertNotIn("R-999", turn["record_ids"])
        self.assertNotIn("A-999", turn["authority_ids"])
        self.assertTrue(turn["verification"]["valid"])

    def test_no_authority_path_completes_with_explicit_limit(self):
        original = appeal._authority_candidates
        appeal._authority_candidates = lambda _query: []
        try:
            run = self.create(suffix="noauth", record_materials="")
            for _ in APPEAL_STAGES:
                run = self.advance(run, suffix="noauth")
        finally:
            appeal._authority_candidates = original
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["final_report"]["authority_traceability"], 0)
        self.assertTrue(any("没有检索到候选法律依据" in item for item in run["final_report"]["improvement_actions"]))

    def test_retrieval_failure_degrades_to_empty_authorities(self):
        original_search = appeal.hybrid_search
        fixture_candidates = appeal._authority_candidates
        appeal.hybrid_search = lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("retrieval unavailable"))
        appeal._authority_candidates = type(self).original_authorities
        try:
            run = self.create(suffix="retrieval-failure")
        finally:
            appeal.hybrid_search = original_search
            appeal._authority_candidates = fixture_candidates
        self.assertEqual(run["authorities"], [])
        self.assertEqual(run["status"], "active")

    def test_unsupported_submission_scores_low_and_produces_correction(self):
        run = self.create(role="appellant")
        run = self.advance(run)
        run = self.advance(run, body=AppealAdvanceInput(content="The appeal should be allowed."))
        score = run["scores"][-1]
        self.assertLess(score["total"], 50)
        self.assertTrue(score["notes"])

    def test_completed_run_rejects_additional_advance(self):
        run = self.create()
        for _ in APPEAL_STAGES:
            run = self.advance(run)
        with self.assertRaisesRegex(ValueError, "已经完成"):
            self.advance(run)

    def test_tenant_isolation_blocks_cross_tenant_read(self):
        run = self.create()
        with self.assertRaises(LookupError):
            get_appeal_run(run["id"], tenant_id="other-tenant")

    def test_rollback_restores_stage_checkpoint_and_discards_later_turns(self):
        run = self.create()
        run = self.advance(run)
        run = self.advance(run)
        run = self.advance(run)
        self.assertEqual(run["active_stage"], "appellant_answer")
        rolled = rollback_appeal_run(run["id"], stage_index=1, user_id=7, tenant_id=self.tenant("a"))
        self.assertEqual(rolled["active_stage"], "appellant_main")
        self.assertEqual(rolled["turns"], [])
        self.assertIsNone(rolled["final_report"])

    def test_invalid_or_unvisited_rollback_is_rejected(self):
        run = self.create()
        with self.assertRaisesRegex(ValueError, "无效"):
            rollback_appeal_run(run["id"], stage_index=99, user_id=7, tenant_id=self.tenant("a"))
        with self.assertRaisesRegex(ValueError, "没有可用检查点"):
            rollback_appeal_run(run["id"], stage_index=4, user_id=7, tenant_id=self.tenant("a"))

    def test_sql_and_html_like_content_remains_data(self):
        run = self.create(role="appellant")
        run = self.advance(run)
        hostile = "<script>alert(1)</script>'; DROP TABLE appeal_runs; -- correctness"
        run = self.advance(run, body=AppealAdvanceInput(content=hostile, issue_ids=["I-001"], record_ids=["R-001"], authority_ids=["A-001"]))
        self.assertEqual(run["turns"][-1]["content"], hostile)
        self.assertIn("appeal_runs", inspect(self.engine).get_table_names())
        with self.engine.connect() as conn:
            self.assertGreater(conn.execute(text("SELECT COUNT(*) FROM appeal_runs")).scalar_one(), 0)

    def test_tool_allowlist_rejects_role_escalation(self):
        with self.assertRaises(PermissionError):
            appeal._assert_tool("appellant_counsel", "build_report")

    def test_input_schema_rejects_empty_and_oversized_values(self):
        with self.assertRaises(ValidationError):
            AppealRunCreate(case_title="x", case_summary="short", lower_court_decision="short", grounds_of_appeal="short", requested_order="")
        with self.assertRaises(ValidationError):
            AppealAdvanceInput(content="x" * 12001)

    def test_page_template_loads_and_contains_no_probability_output(self):
        environment = Environment(loader=FileSystemLoader(str(Path(__file__).resolve().parents[1] / "app" / "templates")))
        source = environment.loader.get_source(environment, "appeal.html")[0]
        environment.get_template("appeal.html")
        self.assertIn("安大略民事上诉模拟", source)
        self.assertIn("/api/appeal/runs", source)
        self.assertNotIn("胜诉概率", source)

    def test_api_requires_authentication_before_service_call(self):
        request = object()
        with patch.object(routes, "require_user", side_effect=HTTPException(status_code=401, detail="authentication required")), patch.object(routes, "create_appeal_run") as create:
            with self.assertRaises(HTTPException) as raised:
                routes.api_create_appeal_run(request, payload())
        self.assertEqual(raised.exception.status_code, 401)
        create.assert_not_called()

    def test_http_page_and_api_enforce_login(self):
        app = FastAPI()
        app.add_middleware(SessionMiddleware, secret_key="appeal-test")
        app.include_router(routes.router)
        client = TestClient(app)
        page = client.get("/appeal-simulation", follow_redirects=False)
        self.assertEqual(page.status_code, 303)
        self.assertTrue(page.headers["location"].startswith("/login?next=%2Fappeal-simulation"))
        response = client.post("/api/appeal/runs", json=payload().model_dump())
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)
