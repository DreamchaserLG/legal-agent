from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.service.hearing_workflow_service import (
    HearingRunCreate,
    HearingTurnInput,
    create_hearing_run,
    submit_hearing_turn,
)
import app.service.hearing_workflow_service as hearing


def main() -> None:
    hearing.is_llm_configured = lambda: False
    hearing._evidence_from_search = lambda *_args, **_kwargs: [
        {
            "doc_id": "cases_metadata:101:1", "title": "示例加拿大案例", "citation": "2020 ONCA 1",
            "source_kind": "case", "source_url": "https://example.invalid/case", "court": "ONCA",
            "decision_date": "2020-01-01", "score": 0.92, "excerpt": "示例案例文本。",
        },
        {
            "doc_id": "laws_metadata:201:1", "title": "示例加拿大法规", "citation": "RSC 1985, c C-46",
            "source_kind": "law", "source_url": "https://example.invalid/law", "court": "",
            "decision_date": "", "score": 0.91, "excerpt": "示例法规文本。",
        },
    ]
    hearing.settings.hearing_simulation_enabled = True
    hearing.settings.hearing_final_report_enabled = True
    hearing.settings.hearing_simulation_probability_enabled = True
    run = create_hearing_run(
        HearingRunCreate(
            case_summary=(
                "Ontario employment dismissal dispute. The employee alleges wrongful dismissal "
                "and relies on the employment contract and termination notice."
            ),
            jurisdiction="Ontario",
        ),
        user_id=1,
        tenant_id="hearing-test",
    )
    for index in range(7):
        stage = run["active_stage"]
        record = run["stages"][stage]
        run = submit_hearing_turn(
            run["id"],
            HearingTurnInput(
                content=(
                    "User submission with no contract, screenshot evidence, loan and investment wording, "
                    "limitation date, and asset uncertainty."
                    if index == 0 else f"User submission for {stage} with facts, evidence source, legal response and date."
                ),
                action="complete_stage",
                evidence_ids=record["evidence_ids"][:2],
                legal_basis_ids=record["legal_basis_ids"][:2],
            ),
            user_id=1,
            tenant_id="hearing-test",
        )
        print({"iteration": index + 1, "stage": stage, "status": run["status"], "next": run["active_stage"]})

    report = run["final_report"]
    assert report and report["display_probability"] is True
    assert 0 <= report["probability"] <= 100
    assert len(report["confidence_interval"]) == 2
    assert report["calibration_status"].startswith("未校准")
    assert run["status"] == "completed"
    assert run["latest_turn"]["stage"] == "胜诉概率报告"
    assert {"user", "opposing_counsel", "judge"}.issubset({message["role"] for message in run["turn_results"][0]["messages"]})
    assert {"实体风险", "证据风险", "程序风险", "执行风险"}.issubset({risk["category"] for risk in report["risks"]})
    print({"result": "success", "risk_count": len(report["risks"]), "probability": report["probability"]})


if __name__ == "__main__":
    main()
