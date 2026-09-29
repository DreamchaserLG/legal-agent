from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.service.appeal_workflow_service as appeal
from app.service.appeal_workflow_service import AppealAdvanceInput, AppealRunCreate


REAL_AUTHORITIES = [
    {
        "authority_id": "A-001",
        "source_key": "external:scc:2014scc7",
        "authority_type": "case",
        "title": "Hryniak v. Mauldin",
        "citation": "2014 SCC 7, [2014] 1 S.C.R. 87",
        "court": "Supreme Court of Canada",
        "decision_date": "2014-01-23",
        "source_url": "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/13427/index.do",
        "excerpt": "The Supreme Court dismissed Robert Hryniak's appeal and confirmed that summary judgment may be granted where there is no genuine issue requiring a trial.",
        "score": 1.0,
    },
    {
        "authority_id": "A-002",
        "source_key": "external:ontario:rule-20",
        "authority_type": "law",
        "title": "Rules of Civil Procedure, R.R.O. 1990, Reg. 194, Rule 20",
        "citation": "Rule 20.04(2), 20.04(2.1)",
        "court": "Ontario regulation",
        "decision_date": "",
        "source_url": "https://www.ontario.ca/laws/regulation/900194",
        "excerpt": "Rule 20.04 directs summary judgment where there is no genuine issue requiring a trial, and permits weighing evidence, assessing credibility, and drawing reasonable inferences unless trial is required in the interest of justice.",
        "score": 1.0,
    },
    {
        "authority_id": "A-003",
        "source_key": "external:onca:2011onca764",
        "authority_type": "case",
        "title": "Combined Air Mechanical Services Inc. v. Flesch",
        "citation": "2011 ONCA 764",
        "court": "Court of Appeal for Ontario",
        "decision_date": "2011-12-05",
        "source_url": "https://www.minicounsel.ca/oca/2011/764",
        "excerpt": "The Ontario Court of Appeal articulated the full appreciation approach to amended Rule 20 and dismissed Hryniak's appeal in the Mauldin action.",
        "score": 0.98,
    },
    {
        "authority_id": "A-004",
        "source_key": "external:scc:housen",
        "authority_type": "case",
        "title": "Housen v. Nikolaisen",
        "citation": "2002 SCC 33, [2002] 2 S.C.R. 235",
        "court": "Supreme Court of Canada",
        "decision_date": "2002-05-23",
        "source_url": "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/1972/index.do",
        "excerpt": "Questions of law are reviewed for correctness; findings of fact and mixed fact and law without an extricable legal error are reviewed for palpable and overriding error.",
        "score": 0.95,
    },
    {
        "authority_id": "A-005",
        "source_key": "external:onsc:2010onsc5490",
        "authority_type": "case",
        "title": "Bruno Appliance and Furniture, Inc. v. Hryniak",
        "citation": "2010 ONSC 5490",
        "court": "Ontario Superior Court of Justice",
        "decision_date": "2010-10-22",
        "source_url": "https://www.canlii.org/en/on/onsc/doc/2010/2010onsc5490/2010onsc5490.html",
        "excerpt": "The motion judge granted summary judgment against Hryniak in the Mauldin action after finding civil fraud on the Rule 20 record.",
        "score": 0.92,
    },
]


def real_case_payload() -> AppealRunCreate:
    return AppealRunCreate(
        case_title="Hryniak v. Mauldin, 2014 SCC 7 - Ontario civil appeal simulation",
        case_summary=(
            "Robert Hryniak appealed from an Ontario Court of Appeal decision that upheld summary judgment in favour of "
            "the Mauldin Group in a civil fraud action. The investors alleged they wired US$1.2 million after a June 2001 "
            "investment meeting involving Hryniak, Cranston and Peebles; the funds moved through Cassels Brock and Tropos "
            "and later disappeared after transfer offshore."
        ),
        lower_court_decision=(
            "The Ontario Superior Court granted summary judgment against Hryniak, finding civil fraud and ordering damages "
            "for the Mauldin Group, while leaving claims against Peebles and Cassels Brock for trial. The Court of Appeal "
            "dismissed Hryniak's appeal in the Mauldin action, despite recognizing that similar future cases would generally "
            "require trial under its full appreciation approach."
        ),
        grounds_of_appeal=(
            "The Court of Appeal applied the wrong legal test by suspending its full appreciation test for this appellant and should have ordered trial.\n"
            "The motion judge and Court of Appeal used Rule 20.04(2.1) powers on a voluminous paper record with major credibility disputes.\n"
            "The finding of civil fraud should not stand because the record required viva voce trial process before final liability."
        ),
        requested_order="Allow the appeal, set aside summary judgment against Hryniak, and remit the Mauldin action for trial.",
        record_materials=(
            "Mauldin Group invested US$1.2 million after the June 2001 investment meeting.\n"
            "Eighteen witnesses filed affidavits, cross-examinations took three weeks and the motion record contained 28 volumes of evidence.\n"
            "The motion judge found Hryniak not credible and granted summary judgment only against him.\n"
            "The Court of Appeal held the record firmly supported the fraud finding and dismissed the appeal.\n"
            "The Supreme Court of Canada dismissed the appeal with costs to the respondents on January 23, 2014."
        ),
        practice_role="appellant",
    )


class RealCaseHryniakAppealWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original_engine = appeal.engine
        cls.original_is_sqlite = appeal.is_sqlite
        cls.original_authorities = appeal._authority_candidates
        cls.original_llm_configured = appeal.is_llm_configured
        cls.db_dir = tempfile.TemporaryDirectory(prefix="appeal-real-case-")
        cls.engine = create_engine(f"sqlite+pysqlite:///{Path(cls.db_dir.name) / 'case.sqlite3'}", future=True)
        appeal.engine = cls.engine
        appeal.is_sqlite = lambda: True
        appeal._authority_candidates = lambda _query: [dict(item) for item in REAL_AUTHORITIES]
        appeal.is_llm_configured = lambda: False
        appeal.ensure_appeal_workflow_tables()

    @classmethod
    def tearDownClass(cls):
        appeal.engine = cls.original_engine
        appeal.is_sqlite = cls.original_is_sqlite
        appeal._authority_candidates = cls.original_authorities
        appeal.is_llm_configured = cls.original_llm_configured
        cls.engine.dispose()
        cls.db_dir.cleanup()

    def test_hryniak_real_case_runs_complete_flow_with_traceable_authorities(self):
        run = appeal.create_appeal_run(real_case_payload(), user_id=34641, tenant_id="real-case-hryniak")

        self.assertEqual([item["authority_id"] for item in run["authorities"]], ["A-001", "A-002", "A-003", "A-004", "A-005"])
        self.assertTrue(all(item["source_url"].startswith("http") for item in run["authorities"]))
        self.assertEqual([item["expected_standard"] for item in run["issues"]][0], "correctness")

        run = appeal.advance_appeal_run(run["id"], AppealAdvanceInput(), user_id=34641, tenant_id="real-case-hryniak")
        run = appeal.advance_appeal_run(
            run["id"],
            AppealAdvanceInput(
                content=(
                    "For Hryniak, the reversible error is legal and procedural. The Court of Appeal accepted that the "
                    "full appreciation test would require a trial for records like this, yet it upheld final civil fraud "
                    "liability on a paper record. Under Rule 20.04 and Hryniak v. Mauldin, the court must ask whether "
                    "summary judgment gives a fair and just determination. The affidavits from 18 witnesses, three weeks of "
                    "cross-examinations and 28-volume record show why the order should be set aside and the matter sent to trial."
                ),
                issue_ids=["I-001", "I-002"],
                record_ids=["R-002", "R-003", "R-006", "R-007"],
                authority_ids=["A-001", "A-002", "A-003", "A-004"],
            ),
            user_id=34641,
            tenant_id="real-case-hryniak",
        )
        run = appeal.advance_appeal_run(run["id"], AppealAdvanceInput(), user_id=34641, tenant_id="real-case-hryniak")
        run = appeal.advance_appeal_run(
            run["id"],
            AppealAdvanceInput(
                content=(
                    "The answer to the panel is that correctness applies to the legal test for summary judgment, while any "
                    "factual finding still needs deference only after the court confirms a trial is unnecessary. Here the "
                    "Court of Appeal itself identified a record normally needing trial, so the legal threshold was not met."
                ),
                issue_ids=["I-001", "I-002"],
                record_ids=["R-002", "R-006", "R-007"],
                authority_ids=["A-001", "A-002", "A-003", "A-004"],
            ),
            user_id=34641,
            tenant_id="real-case-hryniak",
        )
        run = appeal.advance_appeal_run(run["id"], AppealAdvanceInput(), user_id=34641, tenant_id="real-case-hryniak")
        run = appeal.advance_appeal_run(run["id"], AppealAdvanceInput(), user_id=34641, tenant_id="real-case-hryniak")
        run = appeal.advance_appeal_run(run["id"], AppealAdvanceInput(), user_id=34641, tenant_id="real-case-hryniak")
        run = appeal.advance_appeal_run(
            run["id"],
            AppealAdvanceInput(
                content=(
                    "Replying only to the respondent: the respondents can rely on the fraud finding, but that does not answer "
                    "the threshold problem. The issue is whether Rule 20 allowed final liability on this record. Because the "
                    "record was credibility-heavy and the appellate court accepted that similar cases need trial, the appeal "
                    "should be allowed and remitted."
                ),
                issue_ids=["I-001"],
                record_ids=["R-002", "R-006", "R-007", "R-008"],
                authority_ids=["A-001", "A-002", "A-003"],
            ),
            user_id=34641,
            tenant_id="real-case-hryniak",
        )
        run = appeal.advance_appeal_run(run["id"], AppealAdvanceInput(), user_id=34641, tenant_id="real-case-hryniak")

        self.assertEqual(run["status"], "completed")
        self.assertEqual(len(run["scores"]), 5)
        self.assertEqual(run["final_report"]["role_compliance"], 1.0)
        self.assertGreaterEqual(run["final_report"]["record_traceability"], 0.8)
        self.assertGreaterEqual(run["final_report"]["authority_traceability"], 0.8)
        self.assertIsNone(run["final_report"]["probability"])
        self.assertFalse(run["final_report"]["display_probability"])

        trace_pairs = {(item["role"], item["tool"]) for item in run["agent_trace"]}
        self.assertIn(("court_clerk", "record_read"), trace_pairs)
        self.assertIn(("court_clerk", "authority_read"), trace_pairs)
        self.assertIn(("appellant_counsel", "record_read"), trace_pairs)
        self.assertIn(("appellant_counsel", "authority_read"), trace_pairs)
        self.assertIn(("appellant_counsel", "transcript_read"), trace_pairs)
        self.assertIn(("respondent_counsel", "record_read"), trace_pairs)
        self.assertIn(("respondent_counsel", "authority_read"), trace_pairs)
        self.assertIn(("respondent_counsel", "transcript_read"), trace_pairs)
        self.assertIn(("judge_panel", "record_read"), trace_pairs)
        self.assertIn(("judge_panel", "authority_read"), trace_pairs)
        self.assertIn(("judge_panel", "question_issue"), trace_pairs)
        self.assertIn(("verification_service", "reference_validation"), trace_pairs)
        self.assertIn(("evaluation_service", "score_turn"), trace_pairs)
        self.assertIn(("evaluation_service", "build_report"), trace_pairs)

        result_path = Path("tmp/appeal_real_case_hryniak_result.json")
        result_path.parent.mkdir(exist_ok=True)
        result_path.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    unittest.main(verbosity=2)
