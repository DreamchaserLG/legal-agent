from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import text

from app.core.config import settings
from app.core.database import engine
from app.service.common_service import plain_text_preview, repair_text
from app.service.rag_service import ensure_rag_tables


_BOUNDARY_KEYS = ("section_path", "section_id", "section", "article_no", "heading_path")


def _metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _boundary(metadata: dict[str, Any]) -> tuple[str, str] | None:
    for key in _BOUNDARY_KEYS:
        value = repair_text(metadata.get(key))
        if value:
            return key, value.lower()
    return None


def _estimate_tokens(value: str) -> int:
    text_value = repair_text(value)
    if not text_value:
        return 0
    cjk = len(re.findall(r"[\u3400-\u9fff]", text_value))
    latin = len(re.findall(r"[A-Za-z0-9]+|[^\sA-Za-z0-9\u3400-\u9fff]", text_value))
    return max(1, cjk + latin)


def _merge_overlap(parts: list[str]) -> str:
    merged = ""
    for raw_part in parts:
        part = repair_text(raw_part)
        if not part:
            continue
        if not merged:
            merged = part
            continue
        max_overlap = min(len(merged), len(part), 1200)
        overlap = 0
        for size in range(max_overlap, 23, -1):
            if merged[-size:] == part[:size]:
                overlap = size
                break
        separator = "\n\n" if overlap == 0 else ""
        merged = f"{merged}{separator}{part[overlap:]}"
    return merged.strip()


def _fetch_window(seed: dict[str, Any], radius: int) -> list[dict[str, Any]]:
    source_table = repair_text(seed.get("source_table"))
    source_id = int(seed.get("source_id") or 0)
    chunk_index = int(seed.get("chunk_index") or 0)
    if not source_table or not source_id:
        return []
    sql = """
    SELECT id, source_kind, source_table, source_id, source_code, source_uid,
           title, source_url, jurisdiction, document_type, court_level, language,
           citation, published_at, chunk_index, text_content, metadata_json
    FROM rag_chunks
    WHERE source_table = :source_table
      AND source_id = :source_id
      AND chunk_index BETWEEN :start_index AND :end_index
    ORDER BY chunk_index ASC
    """
    with engine.connect() as conn:
        rows = conn.execute(
            text(sql),
            {
                "source_table": source_table,
                "source_id": source_id,
                "start_index": max(0, chunk_index - radius),
                "end_index": chunk_index + radius,
            },
        ).mappings().all()
    return [dict(row) for row in rows]


def _bounded_rows(seed: dict[str, Any], rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    seed_meta = _metadata(seed.get("metadata"))
    seed_boundary = _boundary(seed_meta)
    if not seed_boundary:
        for row in rows:
            if int(row.get("id") or 0) == int(seed.get("chunk_id") or 0):
                seed_boundary = _boundary(_metadata(row.get("metadata_json")))
                break
    if not seed_boundary:
        return rows, "same_document"

    bounded = []
    for row in rows:
        row_boundary = _boundary(_metadata(row.get("metadata_json")))
        if row_boundary is None or row_boundary == seed_boundary:
            bounded.append(row)
    return bounded, f"same_{seed_boundary[0]}"


def _select_rows_with_budget(
    seed: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    remaining_chunks: int,
    remaining_tokens: int,
) -> list[dict[str, Any]]:
    seed_index = int(seed.get("chunk_index") or 0)
    candidates = sorted(
        rows,
        key=lambda row: (
            0 if int(row.get("chunk_index") or 0) == seed_index else 1,
            abs(int(row.get("chunk_index") or 0) - seed_index),
            int(row.get("chunk_index") or 0),
        ),
    )
    selected: list[dict[str, Any]] = []
    used_tokens = 0
    for row in candidates:
        if len(selected) >= remaining_chunks:
            break
        row_tokens = _estimate_tokens(repair_text(row.get("text_content")))
        is_seed = int(row.get("chunk_index") or 0) == seed_index
        if not is_seed and used_tokens + row_tokens > remaining_tokens:
            continue
        if is_seed and row_tokens > remaining_tokens:
            return []
        selected.append(row)
        used_tokens += row_tokens
    return sorted(selected, key=lambda row: int(row.get("chunk_index") or 0))


def expand_context_windows(
    items: list[dict[str, Any]],
    *,
    enabled: bool | None = None,
    radius: int | None = None,
    seed_limit: int | None = None,
    max_chunks: int | None = None,
    token_budget: int | None = None,
) -> dict[str, Any]:
    """Expand top ranked seed chunks without changing their retrieval score."""
    enabled = bool(getattr(settings, "rag_context_window_enabled", True)) if enabled is None else bool(enabled)
    if not enabled or not items:
        return {
            "items": [dict(item) for item in items],
            "stats": {"enabled": enabled, "bundles": 0, "chunks": 0, "estimated_tokens": 0},
        }

    ensure_rag_tables()
    radius = max(0, min(int(radius if radius is not None else getattr(settings, "rag_context_window_radius", 1)), 2))
    seed_limit = max(1, min(int(seed_limit or getattr(settings, "rag_context_window_seed_limit", 4)), 12))
    max_chunks = max(1, min(int(max_chunks or getattr(settings, "rag_context_window_max_chunks", 12)), 36))
    token_budget = max(512, min(int(token_budget or getattr(settings, "rag_context_window_token_budget", 8000)), 32000))

    expanded = [dict(item) for item in items]
    seen_documents: set[tuple[str, int]] = set()
    used_chunks = 0
    used_tokens = 0
    bundles = 0
    for item in expanded:
        if bundles >= seed_limit or used_chunks >= max_chunks or used_tokens >= token_budget:
            break
        document_key = (repair_text(item.get("source_table")), int(item.get("source_id") or 0))
        if not document_key[0] or not document_key[1] or document_key in seen_documents:
            continue
        seen_documents.add(document_key)
        rows = _fetch_window(item, radius)
        rows, boundary_mode = _bounded_rows(item, rows)
        rows = _select_rows_with_budget(
            item,
            rows,
            remaining_chunks=max_chunks - used_chunks,
            remaining_tokens=token_budget - used_tokens,
        )
        if not rows:
            continue
        context_text = _merge_overlap([repair_text(row.get("text_content")) for row in rows])
        estimated_tokens = _estimate_tokens(context_text)
        if not context_text or used_tokens + estimated_tokens > token_budget:
            continue
        seed_index = int(item.get("chunk_index") or 0)
        context_chunks = [
            {
                "chunk_id": int(row.get("id") or 0),
                "chunk_index": int(row.get("chunk_index") or 0),
                "role": "seed" if int(row.get("chunk_index") or 0) == seed_index else "neighbor",
                "excerpt": plain_text_preview(row.get("text_content"))[:500],
            }
            for row in rows
        ]
        item["context_bundle"] = {
            "seed_chunk_id": int(item.get("chunk_id") or 0),
            "source_table": document_key[0],
            "source_id": document_key[1],
            "boundary_mode": boundary_mode,
            "chunk_ids": [row["chunk_id"] for row in context_chunks],
            "chunk_indexes": [row["chunk_index"] for row in context_chunks],
            "context_chunks": context_chunks,
            "text": context_text,
            "estimated_tokens": estimated_tokens,
        }
        item["context_expanded"] = len(context_chunks) > 1
        used_chunks += len(rows)
        used_tokens += estimated_tokens
        bundles += 1

    return {
        "items": expanded,
        "stats": {
            "enabled": True,
            "radius": radius,
            "seed_limit": seed_limit,
            "bundles": bundles,
            "chunks": used_chunks,
            "estimated_tokens": used_tokens,
            "token_budget": token_budget,
        },
    }


def restrict_context_bundle_to_acl(item: dict[str, Any], case_acl: list[str]) -> dict[str, Any]:
    """Keep exact-chunk ACLs from implicitly granting neighboring private chunks."""
    current = dict(item)
    bundle = dict(current.get("context_bundle") or {})
    if not bundle or not case_acl:
        return current
    allowed = {repair_text(value) for value in case_acl if repair_text(value)}
    source_table = repair_text(current.get("source_table"))
    source_id = int(current.get("source_id") or 0)
    document_keys = {
        repair_text(current.get("source_uid")),
        f"{source_table}:{source_id}",
    }
    if allowed.intersection(document_keys):
        return current
    allowed_chunk_ids = {
        int(chunk.get("chunk_id") or 0)
        for chunk in bundle.get("context_chunks") or []
        if f"{source_table}:{source_id}:{int(chunk.get('chunk_id') or 0)}" in allowed
    }
    if int(current.get("chunk_id") or 0) not in allowed_chunk_ids:
        current.pop("context_bundle", None)
        current["context_expanded"] = False
        return current
    chunks = [chunk for chunk in bundle.get("context_chunks") or [] if int(chunk.get("chunk_id") or 0) in allowed_chunk_ids]
    bundle["context_chunks"] = chunks
    bundle["chunk_ids"] = [int(chunk.get("chunk_id") or 0) for chunk in chunks]
    bundle["chunk_indexes"] = [int(chunk.get("chunk_index") or 0) for chunk in chunks]
    bundle["text"] = _merge_overlap([repair_text(chunk.get("excerpt")) for chunk in chunks])
    bundle["estimated_tokens"] = _estimate_tokens(bundle["text"])
    current["context_bundle"] = bundle
    current["context_expanded"] = len(chunks) > 1
    return current
