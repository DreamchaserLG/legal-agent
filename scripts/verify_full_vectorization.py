from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from app.core.database import engine
from app.service.embedding_service import embedding_metadata


CHECKPOINT_NAME = "a2aj_case_bge_m3_v1"


def _log(path: Path, event: str, **payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _checkpoint_phase() -> str:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT phase FROM vectorization_checkpoints WHERE job_name = :job_name"),
            {"job_name": CHECKPOINT_NAME},
        ).mappings().first()
    return str(row.get("phase") if row else "missing")


def _integrity_metrics() -> dict:
    meta = embedding_metadata()
    sql = text(
        """
        SELECT
          COUNT(*) AS case_chunks,
          COUNT(*) FILTER (WHERE rce.chunk_id IS NULL) AS missing_embedding,
          COUNT(*) FILTER (WHERE rce.content_hash <> rc.content_hash) AS hash_mismatch,
          COUNT(*) FILTER (WHERE rce.dimension <> :dimension) AS wrong_dimension,
          COUNT(*) FILTER (WHERE rce.embedding_vector IS NULL) AS missing_vector,
          COUNT(*) FILTER (WHERE abs(1 - sqrt(-(rce.embedding_vector <#> rce.embedding_vector))) > 0.002) AS non_unit_vector
        FROM rag_chunks rc
        LEFT JOIN rag_chunk_embeddings rce ON rce.chunk_id = rc.id
        WHERE rc.source_table = 'source_items' AND rc.source_code = 'a2aj_case'
          AND rce.embedding_provider = :provider AND rce.embedding_model = :model
        """
    )
    # The provider/model predicate intentionally makes rows with another embedding model fail the count check below.
    with engine.connect() as conn:
        row = dict(
            conn.execute(
                sql,
                {
                    "provider": meta["provider"],
                    "model": meta["model"],
                    "dimension": int(meta["dimension"]),
                },
            ).mappings().one()
        )
        total = conn.execute(
            text(
                "SELECT COUNT(*) FROM rag_chunks "
                "WHERE source_table = 'source_items' AND source_code = 'a2aj_case'"
            )
        ).scalar()
    row["total_case_chunks"] = int(total or 0)
    row["embedding_metadata"] = meta
    return {key: int(value) if isinstance(value, int) else value for key, value in row.items()}


def run(args: argparse.Namespace) -> dict:
    log_path = Path(args.log_path)
    while True:
        phase = _checkpoint_phase()
        _log(log_path, "poll", phase=phase)
        if phase == "completed":
            break
        if phase == "missing":
            raise RuntimeError("未找到全量向量化检查点。")
        time.sleep(args.poll_seconds)

    integrity = _integrity_metrics()
    invalid = [
        key
        for key in ("missing_embedding", "hash_mismatch", "wrong_dimension", "missing_vector", "non_unit_vector")
        if int(integrity.get(key) or 0) != 0
    ]
    if int(integrity["case_chunks"]) != int(integrity["total_case_chunks"]):
        invalid.append("embedding_model_coverage")
    if invalid:
        raise RuntimeError(f"全量向量一致性门禁失败：{', '.join(invalid)}")

    evaluation_path = Path(args.evaluation_output)
    command = [
        sys.executable,
        "scripts/evaluate_case_retrieval.py",
        "--sample-size",
        str(args.sample_size),
        "--limit",
        str(args.limit),
        "--output",
        str(evaluation_path),
    ]
    result = subprocess.run(command, cwd=ROOT_DIR, text=True, capture_output=True, timeout=args.evaluation_timeout)
    if result.returncode != 0:
        raise RuntimeError(f"检索评测失败：{result.stderr[-2000:] or result.stdout[-2000:]}")
    payload = {
        "status": "completed",
        "integrity": integrity,
        "evaluation_output": str(evaluation_path),
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    _log(log_path, "completed", **payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="等待全量向量化完成后执行一致性门禁与检索评测。")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--sample-size", type=int, default=200)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--evaluation-timeout", type=int, default=3600)
    parser.add_argument("--evaluation-output", default="logs/retrieval_evaluation_full.json")
    parser.add_argument("--log-path", default="logs/full_vectorization_verification.jsonl")
    args = parser.parse_args(argv)
    try:
        payload = run(args)
        print(json.dumps(payload, ensure_ascii=False, default=str))
        print("[RESULT]: SUCCESS")
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        print("[RESULT]: FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
