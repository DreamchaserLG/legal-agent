from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import requests
from sqlalchemy import text

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import settings
from app.core.database import engine
from app.service.common_service import plain_text_preview, repair_text, sha256_text, split_keywords, upsert_source_item
from app.service.legal_data_service import ensure_legal_data_tables, sync_canada_legal_data
from app.service.rag_service import get_rag_status
from app.service.risk_assessment_service import ensure_risk_assessment_tables
from app.service.vector_store_service import get_vector_status
from app.service.hybrid_retrieval_service import rebuild_hybrid_index


HF_ROWS_URL = "https://datasets-server.huggingface.co/rows"
HF_RESOLVE_URL = "https://huggingface.co/datasets/{dataset}/resolve/main/{config}/train.parquet"
HF_MIRROR_RESOLVE_URL = "https://hf-mirror.com/datasets/{dataset}/resolve/main/{config}/train.parquet"
XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

A2AJ_CASE_PARQUET_CONFIGS = (
    "BCCA", "BCSC", "CART", "CHRT", "CIRB", "CITT", "CMAC", "CT", "FC", "FCA", "FPSLREB",
    "NSCA", "NSFC", "NSPC", "NSSC", "NSSM", "OHSTC", "OIC", "ONCA", "PSDPT", "RAD", "RLLR",
    "RPD", "SCC", "SCT", "SST", "TATC", "TCC", "YKCA",
)

CASE_COURT_LEVELS = {
    "SCC": "Supreme Court of Canada",
    "FCA": "Federal Court of Appeal",
    "FC": "Federal Court",
    "ONCA": "Ontario Court of Appeal",
    "BCCA": "British Columbia Court of Appeal",
    "BCSC": "Supreme Court of British Columbia",
    "TCC": "Tax Court of Canada",
    "CHRT": "Canadian Human Rights Tribunal",
    "SST": "Social Security Tribunal",
}

JURISDICTION_CODES = {
    "FED": "Canada",
    "ON": "Ontario",
    "BC": "British Columbia",
    "AB": "Alberta",
    "NS": "Nova Scotia",
    "YT": "Yukon",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NT": "Northwest Territories",
    "SK": "Saskatchewan",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
}


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.update(
        {
            "Accept": "application/json,text/xml,application/xml,*/*",
            "User-Agent": "legal-demo-open-data-ingest/1.0",
        }
    )
    return session


def _first_text(*values) -> str:
    for value in values:
        if isinstance(value, list):
            value = ", ".join([repair_text(item) for item in value if repair_text(item)])
        clean = repair_text(value)
        if clean:
            return clean
    return ""


def _compact_list(value, limit: int = 100) -> list[str]:
    if not isinstance(value, list):
        return []
    result = []
    for item in value:
        clean = repair_text(item)
        if clean:
            result.append(clean)
        if len(result) >= limit:
            break
    return result


def _date_text(value) -> str:
    clean = repair_text(value)
    if not clean:
        return ""
    return clean[:10]


def _short_uid(*values: str, prefix: str = "") -> str:
    raw = "|".join([repair_text(value) for value in values if repair_text(value)])
    digest = sha256_text(raw or prefix)[:24]
    clean = re.sub(r"[^A-Za-z0-9_.:-]+", "-", raw).strip("-")[:180]
    return f"{prefix}:{clean or digest}:{digest}"[:240]


def _keywords(*values: str) -> list[str]:
    merged = []
    for value in values:
        merged.extend(split_keywords(value))
        merged.extend(re.findall(r"[A-Za-z][A-Za-z0-9'./-]{2,}", repair_text(value)))
    result = []
    seen = set()
    for item in merged:
        clean = repair_text(item).strip(" .'\"()[]{}")
        key = clean.lower()
        if len(clean) < 3 or key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= 24:
            break
    return result


def _hf_rows(dataset: str, config: str, *, offset: int, length: int) -> list[dict]:
    params = {
        "dataset": dataset,
        "config": config,
        "split": "train",
        "offset": max(0, int(offset)),
        "length": max(1, min(int(length), 100)),
    }
    last_error: Exception | None = None
    for attempt in range(1, 6):
        try:
            response = _session().get(
                HF_ROWS_URL,
                params=params,
                timeout=max(int(getattr(settings, "request_timeout", 30)), 30),
            )
            response.raise_for_status()
            payload = response.json()
            rows = payload.get("rows") or []
            return [dict(item.get("row") or {}) for item in rows]
        except requests.RequestException as exc:
            last_error = exc
            if attempt < 5:
                time.sleep(min(2**attempt, 20))
    raise RuntimeError(
        f"Hugging Face rows 接口连续 5 次请求失败：dataset={dataset} config={config} offset={params['offset']}"
    ) from last_error


def _parquet_cache_path(dataset: str, config: str, cache_dir: str) -> Path:
    safe_dataset = re.sub(r"[^A-Za-z0-9_.-]+", "_", dataset)
    safe_config = re.sub(r"[^A-Za-z0-9_.-]+", "_", config)
    return Path(cache_dir) / safe_dataset / safe_config / "train.parquet"


def _download_parquet(dataset: str, config: str, cache_dir: str, *, max_download_mb: int | None = None) -> Path:
    path = _parquet_cache_path(dataset, config, cache_dir)
    if path.exists() and path.stat().st_size > 0:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    urls = (
        HF_MIRROR_RESOLVE_URL.format(dataset=dataset, config=config),
        HF_RESOLVE_URL.format(dataset=dataset, config=config),
    )
    tmp_path = path.with_suffix(".parquet.tmp")
    last_error: Exception | None = None
    for url in urls:
        try:
            response = _session().get(url, stream=True, timeout=max(int(getattr(settings, "request_timeout", 30)), 60))
            response.raise_for_status()
            size_text = response.headers.get("content-length")
            if size_text and max_download_mb and int(size_text) > max_download_mb * 1024 * 1024:
                raise RuntimeError(f"Parquet file is larger than limit: {int(size_text)} bytes > {max_download_mb} MB")
            with tmp_path.open("wb") as handle:
                shutil.copyfileobj(response.raw, handle)
            tmp_path.replace(path)
            return path
        except Exception as exc:
            last_error = exc
            if tmp_path.exists():
                tmp_path.unlink()
    raise RuntimeError(f"Parquet 下载失败：dataset={dataset} config={config}") from last_error


def _python_value(value):
    if isinstance(value, dict):
        return {key: _python_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_python_value(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _parquet_rows(dataset: str, config: str, *, offset: int, length: int, cache_dir: str, max_download_mb: int | None) -> list[dict]:
    try:
        import pyarrow.parquet as pq
    except Exception as exc:
        raise RuntimeError("Parquet import requires pyarrow.") from exc

    path = _download_parquet(dataset, config, cache_dir, max_download_mb=max_download_mb)
    target_offset = max(0, int(offset))
    target_length = max(1, int(length))
    selected: list[dict] = []
    seen = 0
    parquet_file = pq.ParquetFile(path)
    for batch in parquet_file.iter_batches(batch_size=max(target_length, 128)):
        rows = batch.to_pylist()
        if seen + len(rows) <= target_offset:
            seen += len(rows)
            continue
        start = max(target_offset - seen, 0)
        for row in rows[start:]:
            selected.append({key: _python_value(value) for key, value in dict(row).items()})
            if len(selected) >= target_length:
                return selected
        seen += len(rows)
    return selected


def _ensure_a2aj_parquet_checkpoint_table() -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS a2aj_parquet_checkpoints (
                    config_name TEXT PRIMARY KEY,
                    last_processed_row BIGINT NOT NULL DEFAULT 0,
                    completed BOOLEAN NOT NULL DEFAULT FALSE,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )


def _a2aj_parquet_checkpoint(config: str) -> tuple[int, bool]:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                """
                SELECT last_processed_row, completed
                FROM a2aj_parquet_checkpoints
                WHERE config_name = :config
                """
            ),
            {"config": config},
        ).mappings().first()
    if not row:
        return 0, False
    return int(row.get("last_processed_row") or 0), bool(row.get("completed"))


def _update_a2aj_parquet_checkpoint(config: str, processed_rows: int, completed: bool) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                INSERT INTO a2aj_parquet_checkpoints (config_name, last_processed_row, completed, updated_at)
                VALUES (:config, :processed_rows, :completed, CURRENT_TIMESTAMP)
                ON CONFLICT (config_name) DO UPDATE SET
                    last_processed_row = EXCLUDED.last_processed_row,
                    completed = EXCLUDED.completed,
                    updated_at = CURRENT_TIMESTAMP
                """
            ),
            {"config": config, "processed_rows": processed_rows, "completed": completed},
        )


def ingest_a2aj_case_parquets(*, cache_dir: str, max_download_mb: int | None, max_text_chars: int, batch_size: int = 100) -> dict:
    """按法院配置流式导入 A2AJ Parquet，并将每个配置的进度持久化。"""
    try:
        import pyarrow.parquet as pq
    except Exception as exc:
        raise RuntimeError("A2AJ Parquet 导入需要 pyarrow。") from exc

    _ensure_a2aj_parquet_checkpoint_table()
    result = {
        "source": "a2aj_parquet",
        "configs": [],
        "processed": 0,
        "skipped_empty_text": 0,
        "errors": [],
    }
    for config in A2AJ_CASE_PARQUET_CONFIGS:
        processed_rows, completed = _a2aj_parquet_checkpoint(config)
        config_result = {"config": config, "resumed_at": processed_rows, "processed": 0, "completed": completed}
        if completed:
            result["configs"].append(config_result)
            continue
        try:
            path = _download_parquet(
                settings.a2aj_hf_case_dataset,
                config,
                cache_dir,
                max_download_mb=max_download_mb,
            )
            parquet_file = pq.ParquetFile(path)
            seen_rows = 0
            for record_batch in parquet_file.iter_batches(batch_size=max(1, batch_size)):
                rows = record_batch.to_pylist()
                next_seen_rows = seen_rows + len(rows)
                if next_seen_rows <= processed_rows:
                    seen_rows = next_seen_rows
                    continue
                start_index = max(processed_rows - seen_rows, 0)
                for row in rows[start_index:]:
                    item_id = _upsert_a2aj_case(
                        {key: _python_value(value) for key, value in dict(row).items()},
                        max_text_chars=max_text_chars,
                    )
                    result["processed"] += 1
                    config_result["processed"] += 1
                    if item_id is None:
                        result["skipped_empty_text"] += 1
                seen_rows = next_seen_rows
                processed_rows = seen_rows
                _update_a2aj_parquet_checkpoint(config, processed_rows, completed=False)
            _update_a2aj_parquet_checkpoint(config, processed_rows, completed=True)
            config_result["completed"] = True
        except Exception as exc:
            result["errors"].append({"config": config, "error": str(exc)})
        result["configs"].append(config_result)
        if result["errors"]:
            break
    result["status"] = "success" if not result["errors"] else "failed"
    return result


def _a2aj_rows(
    dataset: str,
    config: str,
    *,
    offset: int,
    length: int,
    mode: str,
    cache_dir: str,
    max_download_mb: int | None,
) -> list[dict]:
    mode = repair_text(mode).lower() or "auto"
    if mode == "viewer":
        return _hf_rows(dataset, config, offset=offset, length=length)
    if mode == "parquet":
        return _parquet_rows(
            dataset,
            config,
            offset=offset,
            length=length,
            cache_dir=cache_dir,
            max_download_mb=max_download_mb,
        )
    if mode == "auto":
        try:
            return _hf_rows(dataset, config, offset=offset, length=length)
        except RuntimeError:
            return _parquet_rows(
                dataset,
                config,
                offset=offset,
                length=length,
                cache_dir=cache_dir,
                max_download_mb=max_download_mb,
            )
    raise ValueError(f"Unsupported A2AJ mode: {mode}")


def _law_source_code(dataset_code: str) -> str:
    code = repair_text(dataset_code).upper()
    if code == "LEGISLATION-FED":
        return "ca_federal_act"
    if code == "REGULATIONS-FED":
        return "ca_federal_regulation"
    if code == "LEGISLATION-ON":
        return "on_statute"
    if code == "REGULATIONS-ON":
        return "on_regulation"
    if code.startswith("REGULATIONS-"):
        return "a2aj_regulation"
    return "a2aj_law"


def _law_document_type(dataset_code: str) -> str:
    return "regulation" if repair_text(dataset_code).upper().startswith("REGULATIONS-") else "statute"


def _law_jurisdiction(dataset_code: str) -> str:
    suffix = repair_text(dataset_code).upper().split("-")[-1]
    return JURISDICTION_CODES.get(suffix, "Canada")


def _upsert_a2aj_case(row: dict, *, max_text_chars: int) -> int | None:
    dataset_code = _first_text(row.get("dataset")).upper()
    citation = _first_text(row.get("citation_en"), row.get("citation_fr"), row.get("citation2_en"), row.get("citation2_fr"))
    title = _first_text(row.get("name_en"), row.get("name_fr"), citation, "Untitled case")
    text_value = _first_text(row.get("unofficial_text_en"), row.get("unofficial_text_fr"))
    normalized_text = repair_text(text_value)
    if not normalized_text:
        return None
    stored_text = normalized_text if max_text_chars <= 0 else normalized_text[:max_text_chars]
    source_url = _first_text(row.get("url_en"), row.get("url_fr"))
    document_date = _date_text(_first_text(row.get("document_date_en"), row.get("document_date_fr")))
    source_uid = _short_uid(dataset_code, citation, title, source_url, prefix="a2aj-case")
    raw_json = {
        "source_dataset": getattr(settings, "a2aj_hf_case_dataset", "a2aj/canadian-case-law"),
        "source_channel": "huggingface_parquet",
        "document_type": "case",
        "country": "Canada",
        "jurisdiction": "Canada",
        "database_id": dataset_code,
        "database_name": CASE_COURT_LEVELS.get(dataset_code, dataset_code),
        "court_level": CASE_COURT_LEVELS.get(dataset_code, dataset_code),
        "court_name": CASE_COURT_LEVELS.get(dataset_code, dataset_code),
        "case_type": "Case",
        "citation": citation,
        "citation2": _first_text(row.get("citation2_en"), row.get("citation2_fr")),
        "language": "en" if repair_text(row.get("unofficial_text_en")) else "fr",
        "cases_cited": _compact_list(row.get("cases_cited_en") or row.get("cases_cited_fr")),
        "cases_citing": _compact_list(row.get("cases_citing_en") or row.get("cases_citing_fr")),
        "citing_cases_count": row.get("citing_cases_count"),
        "upstream_license": row.get("upstream_license"),
    }
    item_id = upsert_source_item(
        source_code="a2aj_case",
        source_uid=source_uid,
        title=title,
        item_url=source_url,
        published_at=document_date or None,
        summary=plain_text_preview(text_value)[:1200] or title,
        raw_text=stored_text,
        raw_json=raw_json,
    )
    from app.service.common_service import replace_item_keywords

    replace_item_keywords(item_id, _keywords(title, citation, dataset_code, text_value[:3000]))
    return int(item_id)


def _upsert_a2aj_law(row: dict, *, max_text_chars: int) -> int:
    dataset_code = _first_text(row.get("dataset")).upper()
    citation = _first_text(row.get("citation_en"), row.get("citation_fr"), row.get("citation2_en"), row.get("citation2_fr"))
    title = _first_text(row.get("name_en"), row.get("name_fr"), citation, "Untitled law")
    text_value = _first_text(row.get("unofficial_text_en"), row.get("unofficial_text_fr"))
    source_url = _first_text(row.get("source_url_en"), row.get("source_url_fr"), row.get("url_en"), row.get("url_fr"))
    document_date = _date_text(_first_text(row.get("document_date_en"), row.get("document_date_fr")))
    source_code = _law_source_code(dataset_code)
    document_type = _law_document_type(dataset_code)
    source_uid = _short_uid(dataset_code, citation, title, source_url, prefix="a2aj-law")
    raw_json = {
        "source_dataset": getattr(settings, "a2aj_hf_law_dataset", "a2aj/canadian-laws"),
        "source_channel": "huggingface_rows",
        "document_type": document_type,
        "law_kind": document_type,
        "legal_type": document_type,
        "country": "Canada",
        "jurisdiction": _law_jurisdiction(dataset_code),
        "level": "federal" if dataset_code.endswith("-FED") else "provincial",
        "database_id": dataset_code,
        "citation": citation,
        "language": "en" if repair_text(row.get("unofficial_text_en")) else "fr",
        "num_sections": row.get("num_sections_en") or row.get("num_sections_fr"),
        "upstream_license": row.get("upstream_license"),
    }
    item_id = upsert_source_item(
        source_code=source_code,
        source_uid=source_uid,
        title=title,
        item_url=source_url,
        published_at=document_date or None,
        summary=plain_text_preview(text_value)[:1200] or citation or title,
        raw_text=repair_text(text_value)[:max_text_chars],
        raw_json=raw_json,
    )
    from app.service.common_service import replace_item_keywords

    replace_item_keywords(item_id, _keywords(title, citation, dataset_code, text_value[:3000]))
    return int(item_id)


def ingest_a2aj(
    *,
    case_configs: list[str],
    law_configs: list[str],
    cases_per_config: int,
    laws_per_config: int,
    max_text_chars: int,
    offset: int,
    batches: int,
    batch_delay_seconds: float,
    mode: str,
    cache_dir: str,
    max_download_mb: int | None,
) -> dict:
    result = {
        "source": "a2aj_huggingface",
        "case_configs": case_configs,
        "law_configs": law_configs,
        "cases": 0,
        "laws": 0,
        "errors": [],
        "batches_completed": 0,
        "next_offset": int(offset or 0),
    }
    if cases_per_config <= 0:
        result["case_status"] = "skipped"
    if laws_per_config <= 0:
        result["law_status"] = "skipped"

    # batches=0 表示持续拉取；瞬时网络失败保留同一 offset 自动重试，空页才视为完成。
    current_offset = max(0, int(offset or 0))
    remaining_batches = max(0, int(batches))
    consecutive_batch_failures = 0
    max_continuous_failures = 60
    while remaining_batches == 0 or result["batches_completed"] < remaining_batches:
        batch_has_rows = False
        batch_failed = False
        if cases_per_config > 0:
            for config in case_configs:
                try:
                    rows = _a2aj_rows(
                        settings.a2aj_hf_case_dataset,
                        config,
                        offset=current_offset,
                        length=cases_per_config,
                        mode=mode,
                        cache_dir=cache_dir,
                        max_download_mb=max_download_mb,
                    )
                    batch_has_rows = batch_has_rows or bool(rows)
                    for row in rows:
                        _upsert_a2aj_case(row, max_text_chars=max_text_chars)
                        result["cases"] += 1
                except Exception as exc:
                    batch_failed = True
                    result["errors"].append({"config": config, "type": "case", "offset": current_offset, "error": str(exc)})
        if laws_per_config > 0:
            for config in law_configs:
                try:
                    rows = _a2aj_rows(
                        settings.a2aj_hf_law_dataset,
                        config,
                        offset=current_offset,
                        length=laws_per_config,
                        mode=mode,
                        cache_dir=cache_dir,
                        max_download_mb=max_download_mb,
                    )
                    batch_has_rows = batch_has_rows or bool(rows)
                    for row in rows:
                        _upsert_a2aj_law(row, max_text_chars=max_text_chars)
                        result["laws"] += 1
                except Exception as exc:
                    batch_failed = True
                    result["errors"].append({"config": config, "type": "law", "offset": current_offset, "error": str(exc)})
        if batch_failed:
            consecutive_batch_failures += 1
            if remaining_batches == 0 and consecutive_batch_failures <= max_continuous_failures:
                retry_delay = min(max(float(batch_delay_seconds), 1.0) * (2 ** min(consecutive_batch_failures, 6)), 60.0)
                time.sleep(retry_delay)
                continue
            break
        if not batch_has_rows:
            break
        consecutive_batch_failures = 0
        result["batches_completed"] += 1
        current_offset += max(cases_per_config, laws_per_config, 1)
        result["next_offset"] = current_offset
        if batch_delay_seconds > 0 and (remaining_batches == 0 or result["batches_completed"] < remaining_batches):
            time.sleep(float(batch_delay_seconds))
    result["status"] = "success" if result["cases"] or result["laws"] else "failed"
    return result


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_text(root: ElementTree.Element, names: set[str]) -> str:
    for element in root.iter():
        if _local_name(element.tag) in names:
            clean = repair_text(" ".join(element.itertext()))
            if clean:
                return clean
    return ""


def _xml_attr(root: ElementTree.Element, name: str) -> str:
    for key, value in root.attrib.items():
        if key == name or key.endswith("}" + name):
            return repair_text(value)
    return ""


def _xml_text(root: ElementTree.Element, max_text_chars: int) -> str:
    return plain_text_preview(" ".join(root.itertext()))[:max_text_chars]


def _upsert_laws_lois_xml_file(path: Path, root_dir: Path, *, max_text_chars: int) -> int:
    root = ElementTree.parse(path).getroot()
    root_kind = _local_name(root.tag)
    is_regulation = root_kind.lower() == "regulation"
    document_type = "regulation" if is_regulation else "statute"
    title = _first_text(
        _find_text(root, {"ShortTitle"}),
        _find_text(root, {"LongTitle"}),
        _find_text(root, {"RunningHead"}),
        path.stem,
    )
    citation = _first_text(
        _find_text(root, {"InstrumentNumber"}),
        _find_text(root, {"Chapter"}),
        _xml_attr(root, "chapter"),
        _xml_attr(root, "id"),
    )
    relative_path = path.relative_to(root_dir).as_posix()
    text_value = _xml_text(root, max_text_chars)
    language = _xml_attr(root, "lang") or root.attrib.get(XML_LANG, "")
    source_code = "ca_federal_regulation" if is_regulation else "ca_federal_act"
    raw_json = {
        "source_dataset": "justicecanada/laws-lois-xml",
        "source_channel": "local_xml_repository",
        "official_source": "Justice Canada Laws-Lois XML",
        "relative_path": relative_path,
        "document_type": document_type,
        "law_kind": document_type,
        "legal_type": document_type,
        "country": "Canada",
        "jurisdiction": "Canada",
        "level": "federal",
        "citation": citation,
        "language": language or "en",
        "current_date": _xml_attr(root, "current-date"),
        "last_amended_date": _xml_attr(root, "lastAmendedDate"),
        "pit_date": _xml_attr(root, "pit-date"),
    }
    item_id = upsert_source_item(
        source_code=source_code,
        source_uid=_short_uid(relative_path, citation, title, prefix="laws-lois-xml"),
        title=title,
        item_url=f"https://github.com/justicecanada/laws-lois-xml/blob/main/{relative_path}",
        published_at=_xml_attr(root, "current-date") or _xml_attr(root, "pit-date") or None,
        summary=text_value[:1200] or title,
        raw_text=text_value,
        raw_json=raw_json,
    )
    from app.service.common_service import replace_item_keywords

    replace_item_keywords(item_id, _keywords(title, citation, relative_path, text_value[:3000]))
    return int(item_id)


def ingest_laws_lois_xml(*, local_dir: str, limit: int, offset: int, max_text_chars: int) -> dict:
    root_dir = Path(local_dir)
    if not root_dir.exists():
        return {
            "source": "laws_lois_xml",
            "status": "skipped",
            "processed": 0,
            "message": f"Local XML repository not found: {root_dir}",
        }
    xml_files = sorted(root_dir.rglob("*.xml"))
    if offset and offset > 0:
        xml_files = xml_files[int(offset) :]
    if limit and limit > 0:
        xml_files = xml_files[: int(limit)]
    processed = 0
    errors = []
    for path in xml_files:
        try:
            _upsert_laws_lois_xml_file(path, root_dir, max_text_chars=max_text_chars)
            processed += 1
        except Exception as exc:
            errors.append({"path": str(path), "error": str(exc)})
    return {
        "source": "laws_lois_xml",
        "status": "success" if processed else "failed",
        "offset": int(offset or 0),
        "selected_files": len(xml_files),
        "processed": processed,
        "errors": errors[:20],
    }


def run_ingest(args: argparse.Namespace) -> dict:
    ensure_legal_data_tables()
    ensure_risk_assessment_tables()
    steps = []
    if args.source in {"all", "a2aj"}:
        case_configs = args.case_config or settings.a2aj_demo_case_configs
        law_configs = args.law_config or settings.a2aj_demo_law_configs
        steps.append(
            ingest_a2aj(
                case_configs=case_configs,
                law_configs=law_configs,
                cases_per_config=args.cases_per_config,
                laws_per_config=args.laws_per_config,
                max_text_chars=args.max_text_chars,
                offset=args.a2aj_offset,
                batches=args.a2aj_batches,
                batch_delay_seconds=args.a2aj_batch_delay_seconds,
                mode=args.a2aj_mode,
                cache_dir=args.a2aj_cache_dir,
                max_download_mb=args.a2aj_max_download_mb,
            )
        )
    if args.source in {"all", "laws-lois-xml"}:
        steps.append(
            ingest_laws_lois_xml(
                local_dir=args.laws_lois_dir,
                limit=args.laws_lois_limit,
                offset=args.laws_lois_offset,
                max_text_chars=args.max_text_chars,
            )
        )

    sync_result = {"status": "skipped"}
    if not args.skip_sync:
        sync_result = sync_canada_legal_data(force=True)
    rebuild_result = {"status": "skipped"}
    if not args.skip_rebuild:
        rebuild_result = rebuild_hybrid_index(
            source_filter="canada",
            module="canada",
            limit=args.rebuild_limit,
        )
    return {
        "status": "completed",
        "steps": steps,
        "sync": sync_result,
        "rebuild": rebuild_result,
        "rag_status": get_rag_status(),
        "vector_status": get_vector_status(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Import open Canadian legal data into the local PostgreSQL demo database.")
    parser.add_argument("--source", choices=["all", "a2aj", "laws-lois-xml"], default="all")
    parser.add_argument("--case-config", action="append", default=[], help="A2AJ case config, for example SCC or ONCA. Can repeat.")
    parser.add_argument("--law-config", action="append", default=[], help="A2AJ law config, for example LEGISLATION-FED. Can repeat.")
    parser.add_argument("--a2aj-mode", choices=["auto", "viewer", "parquet"], default="auto")
    parser.add_argument("--a2aj-offset", type=int, default=0)
    parser.add_argument("--a2aj-batches", type=int, default=1, help="连续拉取批次数；0 表示直到数据源耗尽。")
    parser.add_argument("--a2aj-batch-delay-seconds", type=float, default=0.2, help="A2AJ viewer 批次之间的等待秒数。")
    parser.add_argument("--a2aj-cache-dir", default="data/raw/a2aj")
    parser.add_argument("--a2aj-max-download-mb", type=int, default=600)
    parser.add_argument("--cases-per-config", type=int, default=getattr(settings, "a2aj_demo_cases_per_config", 5))
    parser.add_argument("--laws-per-config", type=int, default=getattr(settings, "a2aj_demo_laws_per_config", 5))
    parser.add_argument("--laws-lois-dir", default=getattr(settings, "laws_lois_xml_local_dir", "data/imports/laws-lois-xml"))
    parser.add_argument("--laws-lois-limit", type=int, default=getattr(settings, "laws_lois_xml_demo_limit", 10))
    parser.add_argument("--laws-lois-offset", type=int, default=0)
    parser.add_argument("--max-text-chars", type=int, default=50000)
    parser.add_argument("--rebuild-limit", type=int, default=None)
    parser.add_argument("--skip-sync", action="store_true")
    parser.add_argument("--skip-rebuild", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    payload = run_ingest(args)
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default))
    return 0 if payload.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
