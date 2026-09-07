from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from app.service.common_service import repair_text
from app.service.hybrid_retrieval_service import hybrid_search
from app.service.module_service import normalize_module


DEFAULT_EVAL_PATH = "data/eval/canada_retrieval_eval.json"


def _load_eval_dataset(path: str | None = None) -> dict:
    dataset_path = Path(path or DEFAULT_EVAL_PATH)
    with dataset_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    data["_path"] = str(dataset_path)
    return data


def _contains_all(value: str, expected_values: list[str] | None) -> bool:
    if not expected_values:
        return True
    lowered = repair_text(value).lower()
    return all(repair_text(item).lower() in lowered for item in expected_values if repair_text(item))


def _item_blob(item: dict) -> str:
    metadata = item.get("metadata") or {}
    keywords = metadata.get("keywords") if isinstance(metadata, dict) else []
    if not isinstance(keywords, list):
        keywords = []
    return " ".join(
        [
            repair_text(item.get("title")),
            repair_text(item.get("source_url")),
            repair_text(item.get("excerpt")),
            " ".join([repair_text(value) for value in keywords]),
        ]
    )


def _matches_expectation(item: dict, expectation: dict) -> bool:
    source_kind = repair_text(expectation.get("source_kind")).lower()
    if source_kind and repair_text(item.get("source_kind")).lower() != source_kind:
        return False
    if not _contains_all(item.get("title", ""), expectation.get("title_contains")):
        return False
    if not _contains_all(item.get("source_url", ""), expectation.get("url_contains")):
        return False
    if not _contains_all(_item_blob(item), expectation.get("text_contains")):
        return False
    return True


def _first_match_rank(items: list[dict], expectations: list[dict]) -> tuple[int | None, dict | None]:
    for index, item in enumerate(items, start=1):
        for expectation in expectations:
            if _matches_expectation(item, expectation):
                return index, expectation
    return None, None


def _hit_at(rank: int | None, k: int) -> bool:
    return bool(rank and rank <= k)


def run_retrieval_evaluation(
    *,
    dataset_path: str | None = None,
    module: str | None = None,
    limit: int | None = None,
    output_path: str | None = None,
) -> dict:
    started_at = datetime.utcnow()
    dataset = _load_eval_dataset(dataset_path)
    module_name = normalize_module(module or dataset.get("module") or "canada")
    search_limit = max(1, int(limit or dataset.get("default_limit") or 10))
    cases = dataset.get("cases") or []
    rows = []
    metrics = {
        "cases": 0,
        "hit@1": 0,
        "hit@3": 0,
        "hit@5": 0,
        "hit@10": 0,
        "mrr": 0.0,
    }

    for case in cases:
        query = repair_text(case.get("query"))
        result = hybrid_search(
            query,
            keywords=case.get("keywords") or [],
            module=module_name,
            source_filter=case.get("source_filter") or "all",
            limit=search_limit,
        )
        items = result.get("items") or []
        rank, expectation = _first_match_rank(items, case.get("expected") or [])
        metrics["cases"] += 1
        for k in (1, 3, 5, 10):
            if _hit_at(rank, k):
                metrics[f"hit@{k}"] += 1
        if rank:
            metrics["mrr"] += 1.0 / rank
        rows.append(
            {
                "id": case.get("id"),
                "query": query,
                "status": result.get("status"),
                "strategy": result.get("strategy"),
                "match_rank": rank,
                "matched_expectation": expectation or {},
                "hit@1": _hit_at(rank, 1),
                "hit@3": _hit_at(rank, 3),
                "hit@5": _hit_at(rank, 5),
                "hit@10": _hit_at(rank, 10),
                "top_results": [
                    {
                        "rank": index,
                        "title": item.get("title"),
                        "source_kind": item.get("source_kind"),
                        "score": item.get("score"),
                        "source_url": item.get("source_url"),
                    }
                    for index, item in enumerate(items[:search_limit], start=1)
                ],
            }
        )

    total = max(1, int(metrics["cases"]))
    summary = {
        "cases": int(metrics["cases"]),
        "hit@1": round(metrics["hit@1"] / total, 4),
        "hit@3": round(metrics["hit@3"] / total, 4),
        "hit@5": round(metrics["hit@5"] / total, 4),
        "hit@10": round(metrics["hit@10"] / total, 4),
        "mrr": round(metrics["mrr"] / total, 4),
    }
    payload = {
        "status": "completed",
        "dataset": dataset.get("name", ""),
        "dataset_path": dataset.get("_path", ""),
        "module": module_name,
        "limit": search_limit,
        "summary": summary,
        "cases": rows,
        "duration_seconds": round((datetime.utcnow() - started_at).total_seconds(), 3),
    }
    if output_path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        payload["output_path"] = str(path)
    return payload
