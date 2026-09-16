from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from app.core.database import engine
from app.service.hybrid_retrieval_service import hybrid_search


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    return round(ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower), 3)


def _sample_relations(sample_size: int, min_match_score: float, seed: int) -> list[dict]:
    sql = """
    SELECT lc.source_item_id AS expected_case_source_id,
           lr.source_item_id AS expected_rule_source_id,
           lr.title AS rule_title,
           lc.title AS case_title,
           crr.match_score
    FROM case_rule_relations crr
    JOIN legal_cases lc ON lc.id = crr.case_id
    JOIN legal_rules lr ON lr.id = crr.rule_id
    WHERE lc.source_code = 'a2aj_case'
      AND lc.source_item_id IS NOT NULL
      AND crr.match_score >= :min_match_score
      AND COALESCE(lr.title, '') <> ''
    ORDER BY crr.id
    """
    with engine.connect() as conn:
        rows = [dict(row) for row in conn.execute(text(sql), {"min_match_score": min_match_score}).mappings().all()]
    random.Random(seed).shuffle(rows)
    return rows[:sample_size]


def _rank_of_source(items: list[dict], source_id: int | None) -> int | None:
    if source_id is None:
        return None
    for index, item in enumerate(items, start=1):
        if item.get("source_table") == "source_items" and int(item.get("source_id") or 0) == int(source_id):
            return index
    return None


def run(args: argparse.Namespace) -> dict:
    relations = _sample_relations(args.sample_size, args.min_match_score, args.seed)
    if not relations:
        raise RuntimeError("没有满足条件的案例-法规关联，无法进行独立检索评测。")

    # 预热模型和数据库连接；不纳入延迟指标。
    hybrid_search(relations[0]["rule_title"], module="canada", source_filter="case", limit=args.limit)

    latencies: list[float] = []
    reciprocal_ranks: list[float] = []
    hits = 0
    details = []
    for relation in relations:
        started = time.perf_counter()
        result = hybrid_search(
            relation["rule_title"], module="canada", source_filter="case", limit=args.limit
        )
        latency_ms = (time.perf_counter() - started) * 1000
        items = result.get("items") or []
        rank = _rank_of_source(items, relation["expected_case_source_id"])
        if rank is not None:
            hits += 1
            reciprocal_ranks.append(1 / rank)
        else:
            reciprocal_ranks.append(0.0)
        latencies.append(latency_ms)
        details.append(
            {
                "query": relation["rule_title"],
                "expected_case_source_id": relation["expected_case_source_id"],
                "expected_case_title": relation["case_title"],
                "relation_match_score": float(relation["match_score"]),
                "hit_rank": rank,
                "latency_ms": round(latency_ms, 3),
            }
        )

    return {
        "evaluation_type": "法规标题到已关联案例的检索评测",
        "sample_size": len(relations),
        "sample_method": "固定随机种子的高置信度案例-法规关联随机抽样",
        "ground_truth_constraint": "关联由法规别名/引文在案例原文中的直接命中生成；该指标反映当前关联集上的可检索性，不等价于人工标注的法律正确率。",
        "query_limit": args.limit,
        "min_relation_match_score": args.min_match_score,
        "metrics": {
            f"case_recall_at_{args.limit}": round(hits / len(relations), 4),
            f"case_mrr_at_{args.limit}": round(statistics.mean(reciprocal_ranks), 4),
            "latency_ms_p50": _percentile(latencies, 0.5),
            "latency_ms_p95": _percentile(latencies, 0.95),
            "latency_ms_mean": round(statistics.mean(latencies), 3),
            "latency_ms_max": round(max(latencies), 3),
        },
        "failures": [item for item in details if item["hit_rank"] is None][:50],
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="基于已关联案例-法规对的独立抽样检索评测。")
    parser.add_argument("--sample-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--min-match-score", type=float, default=0.92)
    parser.add_argument("--seed", type=int, default=20260910)
    parser.add_argument("--output", default="logs/retrieval_evaluation.json")
    args = parser.parse_args(argv)
    try:
        payload = run(args)
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(payload, ensure_ascii=False))
        print("[RESULT]: SUCCESS")
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        print("[RESULT]: FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
