from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.service.risk_assessment_service import (
    ensure_risk_assessment_tables,
    list_risk_training_samples,
    record_risk_assessment_sample,
    record_risk_feedback_label,
)


SAMPLES = [
    {
        "input_text": "Ontario tenant is behind on rent but alleges serious repair issues and asks whether eviction risk is high.",
        "jurisdiction": "Ontario",
        "requested_relief": "Avoid eviction or reduce arrears.",
        "disputed_issues": ["rent arrears", "maintenance defects", "eviction remedy"],
        "risk_level": "medium",
        "confidence": 0.58,
        "evidence_status": "partial",
        "evidence_quality_status": "thin",
        "outcome": "Eviction risk exists, but repair evidence may affect timing, arrears, or remedies.",
    },
    {
        "input_text": "Federal appeal question where the only evidence is a short factual allegation without dates, pleadings, or cited authority.",
        "jurisdiction": "Canada",
        "requested_relief": "Predict appeal success.",
        "disputed_issues": ["appeal merits", "record sufficiency"],
        "risk_level": "high",
        "confidence": 0.25,
        "evidence_status": "insufficient",
        "evidence_quality_status": "insufficient",
        "outcome": "Prediction should be guarded because the record is insufficient.",
    },
    {
        "input_text": "Commercial contract dispute with written agreement, clear termination clause, and evidence of bad-faith non-renewal.",
        "jurisdiction": "Canada",
        "requested_relief": "Damages for breach of duty of good faith.",
        "disputed_issues": ["contract interpretation", "good faith", "damages"],
        "risk_level": "low",
        "confidence": 0.68,
        "evidence_status": "supported",
        "evidence_quality_status": "usable",
        "outcome": "The claim has a plausible path if local evidence supports the good-faith theory.",
    },
]


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def seed_samples(with_feedback: bool = False) -> dict:
    ensure_risk_assessment_tables()
    inserted = []
    for sample in SAMPLES:
        prediction = {
            "predicted_outcome": sample["outcome"],
            "likely_prevailing_party": "uncertain",
            "confidence": sample["confidence"],
            "reasoning": sample["outcome"],
            "key_factors": sample["disputed_issues"],
            "supporting_case_titles": [],
            "caveats": ["Demo seed sample; verify against retrieved authorities before production use."],
            "risk_points": sample["disputed_issues"] if sample["risk_level"] != "low" else [],
            "evidence_status": sample["evidence_status"],
            "evidence_quality": {"status": sample["evidence_quality_status"], "warnings": []},
            "jurisdiction": sample["jurisdiction"],
            "requested_relief": sample["requested_relief"],
            "disputed_issues": sample["disputed_issues"],
        }
        sample_id = record_risk_assessment_sample(
            analysis_payload={
                "module_code": "canada",
                "input_text": sample["input_text"],
                "analysis": {
                    "jurisdiction": sample["jurisdiction"],
                    "requested_relief": sample["requested_relief"],
                    "disputed_issues": sample["disputed_issues"],
                },
                "rag_context": {"items": []},
            },
            prediction=prediction,
        )
        if with_feedback and sample_id:
            record_risk_feedback_label(
                sample_id=sample_id,
                human_risk_level=sample["risk_level"],
                human_outcome=sample["outcome"],
                human_notes="Demo bootstrap label.",
                label_json={"source": "seed_risk_samples.py", "risk_level": sample["risk_level"]},
            )
        inserted.append(sample_id)
    return {
        "status": "completed",
        "inserted": [item for item in inserted if item],
        "count": len([item for item in inserted if item]),
        "latest": list_risk_training_samples(limit=10),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed demo risk-assessment training samples.")
    parser.add_argument("--with-feedback", action="store_true", help="Also insert matching human feedback labels.")
    args = parser.parse_args(argv)
    payload = seed_samples(with_feedback=args.with_feedback)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0 if payload.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
