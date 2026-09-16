from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.database import fetch_all
from app.service.legal_data_service import ensure_legal_data_tables, sync_canada_legal_data
from app.service.rag_service import rebuild_rag_index
from app.service.vector_store_service import rebuild_chunk_embeddings, vector_search
from scripts.ingest_open_legal_data import ingest_a2aj_case_parquets


def _log(path: Path, event: str, **payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **payload,
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _a2aj_case_count() -> int:
    rows = fetch_all("SELECT COUNT(*) AS total FROM source_items WHERE source_code = 'a2aj_case'")
    return int(rows[0]["total"] or 0) if rows else 0


def _a2aj_processed_row_count() -> int:
    rows = fetch_all(
        "SELECT COALESCE(SUM(last_processed_row), 0) AS total FROM a2aj_parquet_checkpoints WHERE completed = TRUE"
    )
    return int(rows[0]["total"] or 0) if rows else 0


def _vector_integrity() -> dict:
    rows = fetch_all(
        """
        SELECT
            COUNT(*) AS bge_chunks,
            COUNT(*) FILTER (WHERE rc.id IS NULL) AS missing_chunk,
            COUNT(*) FILTER (WHERE rce.dimension <> 1024 OR vector_dims(rce.embedding_vector) <> 1024) AS invalid_dimension,
            COUNT(*) FILTER (WHERE rce.content_hash <> rc.content_hash) AS stale_content_hash,
            COUNT(*) FILTER (WHERE ABS(1 + (rce.embedding_vector <#> rce.embedding_vector)) > 0.001) AS invalid_norm
        FROM rag_chunk_embeddings rce
        LEFT JOIN rag_chunks rc ON rc.id = rce.chunk_id
        WHERE rce.embedding_provider = 'sentence_transformers'
          AND rce.embedding_model = 'BAAI/bge-m3'
        """
    )
    return dict(rows[0]) if rows else {}


def _association_snapshot() -> dict:
    rows = fetch_all(
        """
        SELECT
            (SELECT COUNT(*) FROM legal_cases) AS legal_cases,
            (SELECT COUNT(*) FROM case_rule_relations) AS relations,
            (SELECT COUNT(DISTINCT case_id) FROM case_rule_relations) AS linked_cases
        """
    )
    return dict(rows[0]) if rows else {}


def _run_ingest_until_complete(args: argparse.Namespace, log_path: Path) -> dict:
    recovery_round = 0
    while True:
        start_offset = _a2aj_case_count()
        _log(log_path, "ingest_started", offset=start_offset, recovery_round=recovery_round)
        result = ingest_a2aj_case_parquets(
            cache_dir=args.cache_dir,
            max_download_mb=args.max_download_mb,
            max_text_chars=args.max_text_chars,
            batch_size=args.page_size,
        )
        _log(log_path, "ingest_finished", result=result, current_total=_a2aj_case_count())
        if not result.get("errors"):
            return result
        recovery_round += 1
        if recovery_round > args.max_recovery_rounds:
            raise RuntimeError(f"A2AJ 导入连续失败超过恢复上限，最后状态：{result}")
        time.sleep(args.recovery_pause_seconds)


def run_pipeline(args: argparse.Namespace) -> dict:
    log_path = Path(args.log_path)
    ensure_legal_data_tables()
    started = time.perf_counter()
    _log(log_path, "pipeline_started", expected_cases=args.expected_cases)

    ingest_result = _run_ingest_until_complete(args, log_path)
    imported_cases = _a2aj_case_count()
    processed_rows = _a2aj_processed_row_count()
    if imported_cases < args.expected_cases and processed_rows < args.expected_cases:
        raise RuntimeError(
            f"A2AJ 导入未达到期望：有效正文案例 {imported_cases}，已处理上游行 {processed_rows}，期望 {args.expected_cases}。"
        )

    _log(log_path, "sync_started", imported_cases=imported_cases, processed_rows=processed_rows)
    sync_result = sync_canada_legal_data(force=True)
    _log(log_path, "sync_finished", result=sync_result)

    _log(log_path, "canada_rag_started")
    case_rag_result = rebuild_rag_index(source_filter="canada")
    _log(log_path, "canada_rag_finished", result=case_rag_result)

    _log(log_path, "case_vector_started")
    case_vector_result = rebuild_chunk_embeddings(source_filter="case", module="canada")
    _log(log_path, "case_vector_finished", result=case_vector_result)

    _log(log_path, "law_vector_started")
    law_vector_result = rebuild_chunk_embeddings(source_filter="law", module="canada")
    _log(log_path, "law_vector_finished", result=law_vector_result)

    latency_samples = []
    for _ in range(5):
        began = time.perf_counter()
        search_result = vector_search("wrongful dismissal notice period employment contract Ontario", source_filter="case", limit=8)
        latency_samples.append((time.perf_counter() - began) * 1000)
    integrity = _vector_integrity()
    associations = _association_snapshot()
    verification = {
        "a2aj_cases": imported_cases,
        "a2aj_processed_rows": processed_rows,
        "association": associations,
        "vector_integrity": integrity,
        "vector_search_ms": [round(item, 2) for item in latency_samples],
        "vector_search_warm_mean_ms": round(sum(latency_samples[1:]) / max(len(latency_samples[1:]), 1), 2),
        "vector_result_total": int(search_result.get("total") or 0),
    }
    if any(int(integrity.get(key) or 0) != 0 for key in ("missing_chunk", "invalid_dimension", "stale_content_hash", "invalid_norm")):
        raise RuntimeError(f"向量一致性校验失败：{verification}")
    if int(associations.get("relations") or 0) <= 0:
        raise RuntimeError(f"案例-法规关联校验失败：{verification}")
    _log(log_path, "pipeline_succeeded", verification=verification)
    return {
        "status": "success",
        "duration_seconds": round(time.perf_counter() - started, 3),
        "ingest": ingest_result,
        "sync": sync_result,
        "case_rag": case_rag_result,
        "case_vector": case_vector_result,
        "law_vector": law_vector_result,
        "verification": verification,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="无人值守执行 A2AJ 全量案例导入、关联、向量化与验收。")
    parser.add_argument("--expected-cases", type=int, default=int(os.getenv("A2AJ_EXPECTED_CASES", "225807")))
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument(
        "--max-text-chars",
        type=int,
        default=0,
        help="单条正文最大字符数；0 表示保留全文。",
    )
    parser.add_argument("--cache-dir", default="data/raw/a2aj")
    parser.add_argument("--max-download-mb", type=int, default=2048)
    parser.add_argument("--max-recovery-rounds", type=int, default=12)
    parser.add_argument("--recovery-pause-seconds", type=int, default=300)
    parser.add_argument("--log-path", default="logs/a2aj_full_pipeline.jsonl")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_pipeline(args)
        print(json.dumps(result, ensure_ascii=False, default=str))
        print("[RESULT]: SUCCESS")
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        print("[RESULT]: FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
