"""
Operational CanLII ingestion entry point.

This command intentionally uses the application's compliant ingestion path:
database-page discovery, RSS feeds, keyword-scoped hydration, de-duplication,
database upsert, and local archive export. It does not implement full-site
bulk downloading, anti-bot bypassing, or multi-IP proxy rotation.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from typing import Any

from app.core.config import settings
from app.service.archive_service import (
    export_source_items_snapshot,
    get_archive_status,
    rebuild_local_archive_from_db,
)
from app.service.data_quality_service import get_source_quality_snapshot
from app.service.canlii_service import (
    configure_canlii_network,
    sync_canlii_api_case_metadata,
    sync_canlii_by_keywords,
    sync_canlii_demo,
)


def _split_keywords(values: list[str] | None) -> list[str]:
    if not values:
        return []

    keywords: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in re.split(r"[,;\n\r\t ]+", str(value or "")):
            keyword = item.strip()
            if not keyword:
                continue
            lowered = keyword.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            keywords.append(keyword)
    return keywords


def _print_result(result: dict[str, Any], as_json: bool = False):
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    command = result.get("command") or "canlii"
    status = result.get("status") or "ok"
    print(f"{command}: {status}")
    for key, value in result.items():
        if key in {"command", "status"}:
            continue
        print(f"  {key}: {value}")


def _runtime_options() -> dict[str, Any]:
    return {
        "configured_database_pages": len(settings.canlii_database_pages),
        "remote_database_page_limit": settings.canlii_remote_database_page_limit,
        "request_delay_seconds": settings.canlii_request_delay_seconds,
        "case_text_char_limit": settings.canlii_case_text_char_limit,
        "proxy_configured": bool(settings.canlii_http_proxy),
        "bulk_sync_enabled": settings.canlii_bulk_sync_enabled,
        "api_key_configured": bool(settings.canlii_api_key),
        "api_base_url": settings.canlii_api_base_url,
        "api_page_size": settings.canlii_api_page_size,
    }


def _is_successful_ingest_result(result: dict[str, Any]) -> bool:
    if result.get("status") in {"success", "partial_success"}:
        return True
    return result.get("error_type") in {"no_match", "missing_keywords"}


def command_status(args: argparse.Namespace) -> int:
    status = get_archive_status()
    status.update(
        {
            "command": "status",
            "status": "ok",
            "runtime": _runtime_options(),
            "source_quality": get_source_quality_snapshot(),
        }
    )
    _print_result(status, args.json)
    return 0


def command_sync_rss(args: argparse.Namespace) -> int:
    result = sync_canlii_demo()
    result.update({"command": "sync-rss"})
    _print_result(result, args.json)
    return 0 if result.get("status") in {"success", "partial_success"} else 1


def command_hydrate(args: argparse.Namespace) -> int:
    keywords = _split_keywords(args.keywords)
    if not keywords:
        _print_result(
            {
                "command": "hydrate-keywords",
                "status": "failed",
                "message": "At least one keyword is required.",
            },
            args.json,
        )
        return 2

    result = sync_canlii_by_keywords(keywords, target_count=args.target_count)
    result.update({"command": "hydrate-keywords", "keywords": keywords})
    _print_result(result, args.json)
    return 0 if _is_successful_ingest_result(result) else 1


def command_api_metadata(args: argparse.Namespace) -> int:
    result = sync_canlii_api_case_metadata(
        language=args.language,
        database_ids=args.database,
        max_databases=args.max_databases,
        max_cases_per_database=args.max_cases_per_database,
        page_size=args.page_size,
        fetch_detail=args.details,
    )
    result.update({"command": "api-metadata"})
    _print_result(result, args.json)
    return 0 if result.get("status") in {"success", "partial_success", "skipped"} else 1


def command_export(args: argparse.Namespace) -> int:
    result = export_source_items_snapshot(source_filter=args.source)
    result.update({"command": "export", "status": "ok"})
    _print_result(result, args.json)
    return 0


def command_rebuild_archive(args: argparse.Namespace) -> int:
    result = rebuild_local_archive_from_db(source_filter=args.source)
    result.update({"command": "rebuild-archive", "status": "ok"})
    _print_result(result, args.json)
    return 0


def command_run(args: argparse.Namespace) -> int:
    steps: list[dict[str, Any]] = []
    exit_code = 0

    rss_result = sync_canlii_demo()
    steps.append({"step": "sync-rss", **rss_result})
    if rss_result.get("status") not in {"success", "partial_success"}:
        exit_code = 1

    keywords = _split_keywords(args.keywords)
    if keywords:
        hydrate_result = sync_canlii_by_keywords(keywords, target_count=args.target_count)
        hydrate_result["keywords"] = keywords
        steps.append({"step": "hydrate-keywords", **hydrate_result})
        if not _is_successful_ingest_result(hydrate_result):
            exit_code = 1

    rebuild_result = rebuild_local_archive_from_db(source_filter="canlii")
    steps.append({"step": "rebuild-archive", **rebuild_result})

    if not args.skip_export:
        export_result = export_source_items_snapshot(source_filter="canlii")
        steps.append({"step": "export", **export_result})

    final_status = get_archive_status()
    result = {
        "command": "run",
        "status": "ok" if exit_code == 0 else "partial_failure",
        "steps": steps,
        "archive_status": final_status,
        "runtime": _runtime_options(),
    }
    _print_result(result, args.json)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run compliant CanLII ingestion, archive rebuild, and export.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--proxy", default=None, help="Single outbound proxy URL for this run.")
    parser.add_argument("--delay", type=float, default=None, help="Request delay in seconds for this run.")

    subparsers = parser.add_subparsers(dest="command")

    status_parser = subparsers.add_parser("status", help="Show database/archive status.")
    status_parser.set_defaults(func=command_status)

    sync_parser = subparsers.add_parser("sync-rss", help="Sync configured/discovered CanLII RSS feeds.")
    sync_parser.set_defaults(func=command_sync_rss)

    hydrate_parser = subparsers.add_parser(
        "hydrate-keywords",
        help="Fetch keyword-matching RSS items and limited case text.",
    )
    hydrate_parser.add_argument("--keywords", nargs="+", required=True, help="Keywords or comma-separated keywords.")
    hydrate_parser.add_argument("--target-count", type=int, default=None, help="Desired item count.")
    hydrate_parser.set_defaults(func=command_hydrate)

    api_parser = subparsers.add_parser(
        "api-metadata",
        help="Import all allowed CanLII API case metadata by database.",
    )
    api_parser.add_argument("--language", choices=["en", "fr"], default="en", help="API language.")
    api_parser.add_argument(
        "--database",
        action="append",
        help="Limit to a CanLII databaseId. Repeat for multiple databases, for example --database onca --database scc.",
    )
    api_parser.add_argument("--max-databases", type=int, default=None, help="Optional database limit for testing.")
    api_parser.add_argument(
        "--max-cases-per-database",
        type=int,
        default=None,
        help="Optional per-database case limit for testing.",
    )
    api_parser.add_argument("--page-size", type=int, default=None, help="API page size, max 100.")
    api_parser.add_argument(
        "--details",
        action="store_true",
        help="Fetch per-case API metadata detail. This is slower and still does not fetch case full text.",
    )
    api_parser.set_defaults(func=command_api_metadata)

    export_parser = subparsers.add_parser("export", help="Write a JSONL snapshot from source_items.")
    export_parser.add_argument("--source", default="canlii", help="Source filter, default: canlii.")
    export_parser.set_defaults(func=command_export)

    rebuild_parser = subparsers.add_parser("rebuild-archive", help="Rewrite per-item archive files from database.")
    rebuild_parser.add_argument("--source", default="canlii", help="Source filter, default: canlii.")
    rebuild_parser.set_defaults(func=command_rebuild_archive)

    run_parser = subparsers.add_parser("run", help="Sync RSS, optionally hydrate keywords, rebuild archive, export.")
    run_parser.add_argument("--keywords", nargs="*", help="Optional keywords or comma-separated keywords.")
    run_parser.add_argument("--target-count", type=int, default=None, help="Desired item count for keyword hydration.")
    run_parser.add_argument("--skip-export", action="store_true", help="Skip JSONL snapshot export.")
    run_parser.set_defaults(func=command_run)

    parser.set_defaults(func=command_run, command="run", keywords=[], target_count=None, skip_export=False)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    configure_canlii_network(proxy=args.proxy, delay_seconds=args.delay)
    try:
        return int(args.func(args))
    except Exception as exc:
        _print_result(
            {
                "command": getattr(args, "command", "canlii"),
                "status": "failed",
                "error": str(exc),
                "runtime": _runtime_options(),
            },
            getattr(args, "json", False),
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
