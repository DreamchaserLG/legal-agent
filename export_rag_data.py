from __future__ import annotations

import argparse
import json
import sys

from app.service.rag_service import export_rag_chunks, rebuild_rag_index

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compatibility wrapper: rebuild and export local RAG chunks as JSONL."
    )
    parser.add_argument("--source", default="all", help="all, canada, ofac, canlii, case, law, or a source_code.")
    parser.add_argument("--output", default="", help="Output JSONL path. Defaults to data_archive/exports.")
    parser.add_argument("--limit", type=int, default=None, help="Maximum source documents/chunks to process.")
    parser.add_argument("--skip-rebuild", action="store_true", help="Export existing chunks without rebuilding first.")
    parser.add_argument("--json", action="store_true", help="Print JSON output.")
    args = parser.parse_args()

    payload = {}
    if not args.skip_rebuild:
        payload["rebuild"] = rebuild_rag_index(source_filter=args.source, limit=args.limit)
        if payload["rebuild"].get("status") != "completed":
            print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
            return 1
    payload["export"] = export_rag_chunks(
        source_filter=args.source,
        output_path=args.output or None,
        limit=args.limit,
    )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    else:
        if payload.get("rebuild"):
            print(
                "rebuild: "
                f"{payload['rebuild'].get('documents_seen')} documents, "
                f"{payload['rebuild'].get('chunks_written')} chunks"
            )
        print(f"export: {payload['export'].get('chunks_exported')} chunks -> {payload['export'].get('path')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
