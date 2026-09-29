from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.service.civil_trial_workflow_service as trial
from app.api import routes
from app.service.civil_trial_workflow_service import (
    CivilTrialActionInput,
    CivilTrialRunCreate,
    TrialExhibitInput,
    TrialIssueInput,
    TrialWitnessInput,
)


RETRIEVED_AUTHORITIES = [
    {
        "authority_id": "AUTH-RAG-001",
        "authority_type": "case",
        "title": "Sattva Capital Corp. v. Creston Moly Corp.",
        "citation": "2014 SCC 53",
        "source_url": "https://decisions.scc-csc.ca/scc-csc/scc-csc/en/item/14302/index.do",
        "excerpt": "Contract interpretation principles.",
        "source_status": "retrieved_candidate",
    }
]


def concrete_trial_payload() -> CivilTrialRunCreate:
    return CivilTrialRunCreate(
        case_title="Northlake Condo Corp. v. Apex Restoration Ltd.",
        case_summary=(
            "Northlake hired Apex to repair a parking-garage waterproofing system. Water entered through repaired joints "
            "after completion. Northlake withheld the final payment and retained another contractor to perform repairs."
        ),
        claim=(
            "Apex breached the written waterproofing specification by omitting waterstop material at expansion joints. "
            "That omission caused recurring leakage and reasonable repair costs."
        ),
        defence=(
            "Apex says pre-existing structural cracks and Northlake's refusal to approve an injection-grouting change "
            "caused the leakage, and says the replacement scope included unrelated upgrades."
        ),
        requested_relief="Damages of CAD 185,000, prejudgment interest and costs.",
        issues=[
            TrialIssueInput(description="Apex是否违反合同防水施工规范"),
            TrialIssueInput(description="违约是否造成渗水及合理修复费用"),
            TrialIssueInput(description="Northlake是否因拒绝变更而应分担损失", burden_side="defendant"),
        ],
        witnesses=[
            TrialWitnessInput(
                name="Olivia Grant", side="plaintiff", role_description="Northlake物业经理",
                testimony_points=[
                    "现场会议记录显示Apex同意按图纸安装伸缩缝止水带。",
                    "完工后第一次强降雨，P2层同一接缝出现积水，我当天发出缺陷通知。",
                    "Northlake支付了第三方修复发票。",
                ],
                issue_indexes=[1, 2],
                credibility_risks=["部分会议纪要由助理整理", "证人不具备工程因果鉴定资格"],
            ),
            TrialWitnessInput(
                name="Dr. Samuel Lee", side="plaintiff", kind="expert", role_description="法证工程师",
                testimony_points=[
                    "开槽检查显示三处接缝底部没有合同图纸要求的止水带。",
                    "染色试验把主要进水路径定位到Apex施工的三处接缝。",
                    "合理修复费用区间为172,000至190,000加元。",
                ],
                issue_indexes=[1, 2], credibility_risks=["首次检查在完工九个月后"],
                expert_field="建筑围护与地下结构防水", report_title="2024年8月地下车库渗水调查报告",
            ),
            TrialWitnessInput(
                name="Marcus Bell", side="defendant", role_description="Apex项目经理",
                testimony_points=[
                    "我发现原混凝土存在贯穿裂缝并向Northlake发出增加注浆的建议。",
                    "Northlake没有签署增加注浆工程的变更单。",
                    "物业代表签署了可见工程的完工检查表。",
                ],
                issue_indexes=[1, 2, 3], credibility_risks=["变更建议没有工程量", "完工表没有记录隐藏节点"],
            ),
            TrialWitnessInput(
                name="Priya Nair", side="defendant", kind="expert", role_description="修复造价顾问",
                testimony_points=[
                    "第三方报价包含原合同以外的排水升级。",
                    "剔除升级和夜间施工溢价后合理费用约为118,000加元。",
                ],
                issue_indexes=[2, 3], credibility_risks=["没有进行渗水原因检测"],
                expert_field="建筑修复造价", report_title="2024年10月修复费用审查报告",
            ),
        ],
        exhibits=[
            TrialExhibitInput(
                title="施工合同及防水图纸", description="要求伸缩缝安装止水带和密封层", proponent="plaintiff",
                foundation_witness="Olivia Grant", issue_indexes=[1], source_url="https://example.test/contract",
            ),
            TrialExhibitInput(
                title="缺陷通知邮件", description="Olivia向Apex报告P2层渗水", proponent="plaintiff",
                foundation_witness="Olivia Grant", issue_indexes=[1, 2], hearsay_risk=True,
                source_url="https://example.test/notice",
            ),
            TrialExhibitInput(
                title="第三方修复发票", description="修复工程发票及付款记录", proponent="plaintiff",
                foundation_witness="Olivia Grant", issue_indexes=[2], hearsay_risk=True, business_record=True,
                source_url="https://example.test/invoice",
            ),
            TrialExhibitInput(
                title="Lee工程报告", description="开槽、染色试验和修复范围", proponent="plaintiff",
                foundation_witness="Dr. Samuel Lee", issue_indexes=[1, 2], source_url="https://example.test/lee",
            ),
            TrialExhibitInput(
                title="无元数据手机截图", description="据称显示施工前的结构裂缝", proponent="defendant",
                foundation_witness="Marcus Bell", issue_indexes=[3], authenticity="disputed",
            ),
            TrialExhibitInput(
                title="完工检查表", description="物业签署的可见工程检查表", proponent="defendant",
                foundation_witness="Marcus Bell", issue_indexes=[1, 3],
            ),
            TrialExhibitInput(
                title="Nair费用审查报告", description="区分缺陷修复和升级范围", proponent="defendant",
                foundation_witness="Priya Nair", issue_indexes=[2, 3],
            ),
        ],
        practice_side="plaintiff",
    )


class CivilTrialWorkflowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.original_engine = trial.engine
        cls.original_is_sqlite = trial.is_sqlite
        cls.original_authorities = trial._authority_candidates
        cls.db_dir = tempfile.TemporaryDirectory(prefix="civil-trial-")
        cls.engine = create_engine(f"sqlite+pysqlite:///{Path(cls.db_dir.name) / 'trial.sqlite3'}", future=True)
        trial.engine = cls.engine
        trial.is_sqlite = lambda: True
        trial._authority_candidates = lambda _query: [dict(item) for item in RETRIEVED_AUTHORITIES]
        trial.ensure_civil_trial_tables()

    @classmethod
    def tearDownClass(cls) -> None:
        trial.engine = cls.original_engine
        trial.is_sqlite = cls.original_is_sqlite
        trial._authority_candidates = cls.original_authorities
        cls.engine.dispose()
        cls.db_dir.cleanup()

    def test_complete_multi_session_trial_with_testimony_objections_and_judgment(self) -> None:
        run = trial.create_civil_trial_run(concrete_trial_payload(), user_id=501, tenant_id="northlake")
        self.assertEqual(run["status"], "active")
        self.assertGreater(len(run["agenda"]), 30)
        self.assertEqual(len(run["witnesses"]), 4)
        self.assertEqual(len(run["exhibits"]), 7)

        safety_count = 0
        while run["status"] != "completed":
            safety_count += 1
            self.assertLess(safety_count, 80)
            if run["status"] == "adjourned":
                with self.assertRaisesRegex(ValueError, "必须先续庭"):
                    trial.perform_civil_trial_action(
                        run["id"], CivilTrialActionInput(action="advance"), user_id=501, tenant_id="northlake"
                    )
                run = trial.perform_civil_trial_action(
                    run["id"], CivilTrialActionInput(action="resume"), user_id=501, tenant_id="northlake"
                )
                continue
            content = "请证人明确说明合同图纸、现场观察与损失之间的联系。" if run["active_task"]["task"] == "direct_round" else ""
            run = trial.perform_civil_trial_action(
                run["id"], CivilTrialActionInput(action="advance", content=content), user_id=501, tenant_id="northlake"
            )

        self.assertGreaterEqual(len(run["sessions"]), 4)
        self.assertTrue(all(item["status"] == "completed" for item in run["witnesses"]))
        self.assertGreaterEqual(run["final_report"]["record_metrics"]["testimony_events"], 16)
        self.assertEqual({item["status"] for item in run["exhibits"]}, {"admitted", "limited", "excluded"})
        self.assertGreaterEqual(len(run["objections"]), 2)
        self.assertEqual(len(run["judgment"]["issue_findings"]), 3)
        self.assertEqual(set(run["completed_phases"]), set(trial.PHASE_LABELS))
        self.assertTrue(all("支持强度" not in item["reasons"] for item in run["judgment"]["issue_findings"]))
        self.assertIn(run["judgment"]["outcome"], {"原告诉请成立", "原告诉请驳回", "原告部分胜诉"})
        self.assertIsNone(run["final_report"]["probability"])
        self.assertFalse(run["final_report"]["display_probability"])
        self.assertEqual(run["final_report"]["record_metrics"]["role_boundary_violations"], 0)
        self.assertLessEqual(run["final_report"]["advocacy_scores"]["plaintiff"]["total"], 100)
        self.assertLessEqual(run["final_report"]["advocacy_scores"]["defendant"]["total"], 100)

        trace_pairs = {(item["role"], item["tool"]) for item in run["agent_trace"]}
        expected_pairs = {
            ("legal_research_agent", "hybrid_search"), ("court_clerk", "session_control"),
            ("plaintiff_counsel", "question_witness"), ("defendant_counsel", "question_witness"),
            ("plaintiff_witness", "answer_question"), ("defendant_witness", "answer_question"),
            ("expert_witness", "expert_report_read"), ("evidence_officer", "foundation_check"),
            ("trial_judge", "rule_objection"), ("trial_judge", "build_judgment"),
            ("evaluation_agent", "score_advocacy"),
        }
        self.assertTrue(expected_pairs.issubset(trace_pairs), expected_pairs - trace_pairs)
        with self.assertRaisesRegex(ValueError, "已经完成"):
            trial.perform_civil_trial_action(
                run["id"], CivilTrialActionInput(action="advance"), user_id=501, tenant_id="northlake"
            )

        output = ROOT_DIR / "tmp" / "civil_trial_northlake_result.json"
        output.parent.mkdir(exist_ok=True)
        output.write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_manual_adjournment_persists_and_requires_resume(self) -> None:
        run = trial.create_civil_trial_run(concrete_trial_payload(), user_id=502, tenant_id="adjourn-test")
        run = trial.perform_civil_trial_action(
            run["id"], CivilTrialActionInput(action="adjourn", reason="证人次日才能到庭"),
            user_id=502, tenant_id="adjourn-test",
        )
        self.assertEqual(run["status"], "adjourned")
        self.assertEqual(run["sessions"][-1]["reason"], "证人次日才能到庭")
        loaded = trial.get_civil_trial_run(run["id"], tenant_id="adjourn-test")
        self.assertEqual(loaded["status"], "adjourned")
        run = trial.perform_civil_trial_action(
            run["id"], CivilTrialActionInput(action="resume"), user_id=502, tenant_id="adjourn-test"
        )
        self.assertEqual(run["status"], "active")
        self.assertEqual(len(run["sessions"]), 2)

    def test_tenant_isolation_and_agent_tool_boundary(self) -> None:
        run = trial.create_civil_trial_run(concrete_trial_payload(), user_id=503, tenant_id="tenant-a")
        with self.assertRaisesRegex(LookupError, "无权访问"):
            trial.get_civil_trial_run(run["id"], tenant_id="tenant-b")
        with self.assertRaisesRegex(PermissionError, "not permitted"):
            trial._assert_tool("plaintiff_witness", "hybrid_search")
        with self.assertRaisesRegex(PermissionError, "not permitted"):
            trial._assert_tool("evaluation_agent", "build_judgment")

    def test_invalid_record_rejects_missing_side_and_unknown_foundation_witness(self) -> None:
        payload = concrete_trial_payload().model_dump()
        payload["witnesses"] = [item for item in payload["witnesses"] if item["side"] == "plaintiff"]
        with self.assertRaisesRegex(ValidationError, "均至少需要一名证人"):
            CivilTrialRunCreate.model_validate(payload)

        payload = concrete_trial_payload().model_dump()
        payload["exhibits"][0]["foundation_witness"] = "Unknown Witness"
        with self.assertRaisesRegex(ValidationError, "基础证人不存在"):
            CivilTrialRunCreate.model_validate(payload)

    def test_http_route_requires_authentication_before_creating_run(self) -> None:
        with patch.object(
            routes,
            "require_user",
            side_effect=HTTPException(status_code=401, detail="authentication required"),
        ), patch.object(routes, "create_civil_trial_run") as create:
            with self.assertRaises(HTTPException) as raised:
                routes.api_create_civil_trial_run(object(), concrete_trial_payload())
        self.assertEqual(raised.exception.status_code, 401)
        create.assert_not_called()

    def test_template_exposes_record_and_authorities_without_win_probability(self) -> None:
        source = (ROOT_DIR / "app" / "templates" / "civil_trial.html").read_text(encoding="utf-8")
        self.assertIn("/api/civil-trial/runs", source)
        self.assertIn("trial-authorities", source)
        self.assertIn("主询问", source)
        self.assertIn("交叉询问", source)
        self.assertNotIn("胜诉概率", source)


if __name__ == "__main__":
    unittest.main(verbosity=2)
