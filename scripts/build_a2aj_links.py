from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections.abc import Iterable, Iterator
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from app.core.database import engine


STATUTE_CITATION_RE = re.compile(
    r"""
    \b
    (?P<series>
        R\s*\.?\s*S\s*\.?\s*C\s*\.? |
        R\s*\.?\s*S\s*\.?\s*O\s*\.? |
        S\s*\.?\s*C\s*\.? |
        R\s*\.?\s*S\s*\.?\s*A\s*\.? |
        S\s*\.?\s*A\s*\.?
    )
    \s*(?P<year>\d{4})\s*,?\s*
    c\s*\.?\s*(?P<chapter>[A-Za-z0-9][A-Za-z0-9.\-]*)
    \b
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _clean(value: object) -> str:
    return str(value or "").strip()


def _identifier(row: dict) -> str:
    return (
        _clean(row.get("citation_en"))
        or _clean(row.get("citation2_en"))
        or _clean(row.get("citation"))
        or _clean(row.get("citation2"))
    )


def normalize_statute_citation(raw: str) -> str:
    """Normalize RSC/RSO/SC-style citations while preserving meaningful chapter punctuation."""
    match = STATUTE_CITATION_RE.search(_clean(raw))
    if not match:
        return ""
    series = re.sub(r"[^A-Za-z]", "", match.group("series")).upper()
    chapter = re.sub(r"\s+", "", match.group("chapter")).upper().rstrip(".,;:")
    return f"{series} {match.group('year')} C {chapter}"


def extract_statute_citations(text_value: object) -> Iterator[tuple[str, str]]:
    for match in STATUTE_CITATION_RE.finditer(_clean(text_value)):
        raw = match.group(0).strip()
        normalized = normalize_statute_citation(raw)
        if normalized:
            yield raw, normalized


def _as_text_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [_clean(item) for item in value if _clean(item)]
    if isinstance(value, tuple):
        return [_clean(item) for item in value if _clean(item)]
    return []


def iter_parquet_rows(root: Path, columns: list[str]) -> Iterable[dict]:
    try:
        import pyarrow.parquet as pq
    except Exception as exc:
        raise RuntimeError("读取本地 A2AJ Parquet 需要 pyarrow。") from exc
    paths = sorted(root.rglob("*.parquet"))
    if not paths:
        return
    for path in paths:
        parquet = pq.ParquetFile(path)
        selected = [column for column in columns if column in parquet.schema_arrow.names]
        for batch in parquet.iter_batches(batch_size=512, columns=selected):
            for row in batch.to_pylist():
                yield dict(row)


def load_statutes(statute_root: Path, *, allow_db_mirror: bool) -> tuple[list[dict], str]:
    parquet_rows = list(
        iter_parquet_rows(statute_root, ["citation_en", "unofficial_text_en"])
    )
    if parquet_rows:
        return parquet_rows, "local_parquet"
    if not allow_db_mirror:
        raise RuntimeError(f"本地法规 Parquet 目录没有文件：{statute_root}")

    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT raw_json ->> 'citation' AS citation_en, raw_text AS unofficial_text_en
                FROM source_items
                WHERE source_code = 'a2aj_law'
                  AND raw_json ->> 'source_dataset' = 'a2aj/canadian-laws'
                ORDER BY id
                """
            )
        ).mappings().all()
    return [dict(row) for row in rows], "local_postgresql_a2aj_law_mirror"


def statute_index(statutes: list[dict]) -> tuple[dict[str, str], int]:
    index: dict[str, str] = {}
    collisions = 0
    for row in statutes:
        statute_id = _clean(row.get("citation_en"))
        normalized = normalize_statute_citation(statute_id)
        if not statute_id or not normalized:
            continue
        if normalized in index and index[normalized] != statute_id:
            collisions += 1
            continue
        index[normalized] = statute_id
    return index, collisions


def build_case_to_case(case_root: Path, output_path: Path) -> dict:
    records = 0
    cases_seen = 0
    cases_without_id = 0
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "cited_case_id", "relation"])
        writer.writeheader()
        for row in iter_parquet_rows(
            case_root,
            ["citation_en", "citation2_en", "citation", "citation2", "cases_cited_en", "cases_cited"],
        ):
            cases_seen += 1
            case_id = _identifier(row)
            if not case_id:
                cases_without_id += 1
                continue
            seen: set[str] = set()
            cited_cases = row.get("cases_cited_en") or row.get("cases_cited")
            for cited_case_id in _as_text_list(cited_cases):
                if cited_case_id in seen:
                    continue
                seen.add(cited_case_id)
                writer.writerow({"case_id": case_id, "cited_case_id": cited_case_id, "relation": "CITES"})
                records += 1
    return {"records": records, "cases_seen": cases_seen, "cases_without_id": cases_without_id}


def build_case_to_statute(case_root: Path, citation_index: dict[str, str], output_path: Path) -> dict:
    records = 0
    cases_seen = 0
    cases_without_id = 0
    matched = 0
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["case_id", "statute_citation_raw", "statute_citation_normalized", "statute_id_matched"],
        )
        writer.writeheader()
        for row in iter_parquet_rows(
            case_root,
            ["citation_en", "citation2_en", "citation", "citation2", "unofficial_text_en", "unofficial_text"],
        ):
            cases_seen += 1
            case_id = _identifier(row)
            if not case_id:
                cases_without_id += 1
                continue
            seen: set[str] = set()
            text_value = row.get("unofficial_text_en") or row.get("unofficial_text")
            for raw, normalized in extract_statute_citations(text_value):
                if normalized in seen:
                    continue
                seen.add(normalized)
                statute_id = citation_index.get(normalized, "")
                writer.writerow(
                    {
                        "case_id": case_id,
                        "statute_citation_raw": raw,
                        "statute_citation_normalized": normalized,
                        "statute_id_matched": statute_id,
                    }
                )
                records += 1
                matched += int(bool(statute_id))
    return {
        "records": records,
        "matched_records": matched,
        "unmatched_records": records - matched,
        "cases_seen": cases_seen,
        "cases_without_id": cases_without_id,
    }


def build_statute_to_statute(statutes: list[dict], citation_index: dict[str, str], output_path: Path) -> dict:
    records = 0
    statutes_without_id = 0
    matched = 0
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["statute_id", "cited_statute_id", "citation_raw", "citation_normalized"],
        )
        writer.writeheader()
        for row in statutes:
            statute_id = _clean(row.get("citation_en"))
            if not statute_id:
                statutes_without_id += 1
                continue
            own_normalized = normalize_statute_citation(statute_id)
            seen: set[str] = set()
            for raw, normalized in extract_statute_citations(row.get("unofficial_text_en")):
                if normalized in seen or normalized == own_normalized:
                    continue
                seen.add(normalized)
                cited_statute_id = citation_index.get(normalized, "")
                writer.writerow(
                    {
                        "statute_id": statute_id,
                        "cited_statute_id": cited_statute_id,
                        "citation_raw": raw,
                        "citation_normalized": normalized,
                    }
                )
                records += 1
                matched += int(bool(cited_statute_id))
    return {
        "records": records,
        "matched_records": matched,
        "unmatched_records": records - matched,
        "statutes_without_id": statutes_without_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="仅基于本地 A2AJ 数据构建判例和法规关联 CSV。")
    parser.add_argument("--case-root", default="data/raw/a2aj/a2aj_canadian-case-law")
    parser.add_argument("--statute-root", default="data/raw/a2aj/a2aj_canadian-laws")
    parser.add_argument("--output-dir", default="outputs/a2aj_links")
    parser.add_argument("--no-db-law-mirror", action="store_true", help="法规 Parquet 缺失时不使用本地 PostgreSQL 镜像。")
    args = parser.parse_args(argv)

    case_root = Path(args.case_root)
    statute_root = Path(args.statute_root)
    output_dir = Path(args.output_dir)
    if not list(case_root.rglob("*.parquet")):
        parser.error(f"未找到本地判例 Parquet：{case_root}")
    output_dir.mkdir(parents=True, exist_ok=True)

    try:
        statutes, statute_source = load_statutes(statute_root, allow_db_mirror=not args.no_db_law_mirror)
        citations, collisions = statute_index(statutes)
        result = {
            "case_to_case_links": build_case_to_case(case_root, output_dir / "case_to_case_links.csv"),
            "case_to_statute_links": build_case_to_statute(case_root, citations, output_dir / "case_to_statute_links.csv"),
            "statute_to_statute_links": build_statute_to_statute(statutes, citations, output_dir / "statute_to_statute_links.csv"),
            "statute_input": {
                "records": len(statutes),
                "source": statute_source,
                "normalized_citation_ids": len(citations),
                "normalized_citation_collisions": collisions,
            },
            "output_dir": str(output_dir),
        }
        print(json.dumps(result, ensure_ascii=False))
        print("[RESULT]: SUCCESS")
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        print("[RESULT]: FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
