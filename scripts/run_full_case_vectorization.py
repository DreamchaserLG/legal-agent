from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# 批处理默认不占用游戏使用的 CUDA；显式设置环境变量时仍可覆盖。
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "-1")
os.environ.setdefault("EMBEDDING_DEVICE", "cpu")
os.environ.setdefault("EMBEDDING_BATCH_SIZE", "2")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")

from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text, sha256_text
from app.service.rag_service import (
    _doc_structured_fields,
    _document_text,
    _json_text,
    _source_kind,
    _upsert_chunk,
    ensure_rag_tables,
    split_into_chunks,
)
from app.service.vector_store_service import ensure_vector_tables, rebuild_chunk_embeddings


CHECKPOINT_NAME = "a2aj_case_bge_m3_v1"


def _log(path: Path, event: str, **payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _ensure_checkpoint_table() -> None:
    if is_sqlite():
        statement = """
        CREATE TABLE IF NOT EXISTS vectorization_checkpoints (
            job_name TEXT PRIMARY KEY,
            last_source_id BIGINT NOT NULL DEFAULT 0,
            phase TEXT NOT NULL DEFAULT 'chunking',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    else:
        statement = """
        CREATE TABLE IF NOT EXISTS vectorization_checkpoints (
            job_name VARCHAR(120) PRIMARY KEY,
            last_source_id BIGINT NOT NULL DEFAULT 0,
            phase VARCHAR(40) NOT NULL DEFAULT 'chunking',
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    with engine.begin() as conn:
        conn.execute(text(statement))
        if is_sqlite():
            conn.execute(
                text(
                    "INSERT OR IGNORE INTO vectorization_checkpoints (job_name) VALUES (:job_name)"
                ),
                {"job_name": CHECKPOINT_NAME},
            )
        else:
            conn.execute(
                text(
                    "INSERT INTO vectorization_checkpoints (job_name) VALUES (:job_name) "
                    "ON CONFLICT (job_name) DO NOTHING"
                ),
                {"job_name": CHECKPOINT_NAME},
            )


def _checkpoint() -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT last_source_id, phase FROM vectorization_checkpoints WHERE job_name = :job_name"
            ),
            {"job_name": CHECKPOINT_NAME},
        ).mappings().one()
    return dict(row)


def _free_gb() -> float:
    return round(shutil.disk_usage(ROOT_DIR.drive + "\\").free / 1024**3, 2)


def _source_page(after_id: int, page_size: int) -> list[dict]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT id, source_code, source_uid, title, item_url, published_at,
                       summary, raw_text, raw_json
                FROM source_items
                WHERE source_code = 'a2aj_case'
                  AND id > :after_id
                  AND COALESCE(raw_text, '') <> ''
                ORDER BY id
                LIMIT :page_size
                """
            ),
            {"after_id": after_id, "page_size": page_size},
        ).mappings().all()
    return [dict(row) for row in rows]


def _document(row: dict) -> dict:
    metadata = row.get("raw_json") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "source_table": "source_items",
        "source_id": int(row["id"]),
        "source_code": repair_text(row.get("source_code")),
        "source_uid": repair_text(row.get("source_uid")),
        "title": repair_text(row.get("title")),
        "source_url": repair_text(row.get("item_url")),
        "published_at": row.get("published_at"),
        "metadata": metadata,
        "text": _document_text(
            title=row.get("title"),
            summary=row.get("summary"),
            body=row.get("raw_text"),
            metadata=metadata,
        ),
    }


def _write_chunk_page(rows: list[dict]) -> tuple[int, int]:
    documents = 0
    chunks_written = 0
    payloads: list[dict] = []
    cleanup_rows: list[dict] = []
    for row in rows:
        doc = _document(row)
        chunks = split_into_chunks(doc["text"])
        if not chunks:
            continue
        documents += 1
        source_kind = _source_kind(doc.get("source_code", ""), doc.get("source_table", ""))
        structured = _doc_structured_fields(doc, source_kind)
        for index, chunk in enumerate(chunks):
            clean_chunk = repair_text(chunk)
            payloads.append(
                {
                    "source_kind": source_kind,
                    "source_table": doc["source_table"],
                    "source_id": doc["source_id"],
                    "source_code": repair_text(doc.get("source_code")),
                    "source_uid": repair_text(doc.get("source_uid")),
                    "title": repair_text(doc.get("title")),
                    "source_url": repair_text(doc.get("source_url")),
                    "jurisdiction": structured["jurisdiction"],
                    "document_type": structured["document_type"],
                    "court_level": structured["court_level"],
                    "language": structured["language"],
                    "citation": structured["citation"],
                    "published_at": doc.get("published_at"),
                    "chunk_index": index,
                    "text_content": clean_chunk,
                    "content_hash": sha256_text(clean_chunk),
                    "metadata_json": _json_text(doc.get("metadata") or {}),
                    "search_text": " ".join([repair_text(doc.get("title")), clean_chunk]),
                }
            )
        chunks_written += len(chunks)
        cleanup_rows.append(
            {
                "source_table": doc["source_table"],
                "source_id": doc["source_id"],
                "chunk_count": len(chunks),
            }
        )
    with engine.begin() as conn:
        if is_sqlite():
            # The production path is PostgreSQL; retain the existing safe behavior for local SQLite tests.
            for payload in payloads:
                _upsert_chunk(
                    conn,
                    {
                        "source_table": payload["source_table"],
                        "source_id": payload["source_id"],
                        "source_code": payload["source_code"],
                        "source_uid": payload["source_uid"],
                        "title": payload["title"],
                        "source_url": payload["source_url"],
                        "published_at": payload["published_at"],
                        "metadata": json.loads(payload["metadata_json"]),
                    },
                    payload["chunk_index"],
                    payload["text_content"],
                )
        elif payloads:
            conn.execute(
                text(
                    """
                    INSERT INTO rag_chunks (
                        source_kind, source_table, source_id, source_code, source_uid,
                        title, source_url, jurisdiction, document_type, court_level,
                        language, citation, published_at, chunk_index, text_content,
                        content_hash, metadata_json, search_vector, created_at, updated_at
                    ) VALUES (
                        :source_kind, :source_table, :source_id, :source_code, :source_uid,
                        :title, :source_url, :jurisdiction, :document_type, :court_level,
                        :language, :citation, :published_at, :chunk_index, :text_content,
                        :content_hash, CAST(:metadata_json AS jsonb),
                        to_tsvector('english', :search_text), CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (source_table, source_id, chunk_index)
                    DO UPDATE SET
                        source_kind = EXCLUDED.source_kind,
                        source_code = EXCLUDED.source_code,
                        source_uid = EXCLUDED.source_uid,
                        title = EXCLUDED.title,
                        source_url = EXCLUDED.source_url,
                        jurisdiction = EXCLUDED.jurisdiction,
                        document_type = EXCLUDED.document_type,
                        court_level = EXCLUDED.court_level,
                        language = EXCLUDED.language,
                        citation = EXCLUDED.citation,
                        published_at = EXCLUDED.published_at,
                        text_content = EXCLUDED.text_content,
                        content_hash = EXCLUDED.content_hash,
                        metadata_json = EXCLUDED.metadata_json,
                        search_vector = EXCLUDED.search_vector,
                        updated_at = CURRENT_TIMESTAMP
                    """
                ),
                payloads,
            )
        for cleanup in cleanup_rows:
            conn.execute(
                text(
                    """
                    DELETE FROM rag_chunks
                    WHERE source_table = :source_table
                      AND source_id = :source_id
                      AND chunk_index >= :chunk_count
                    """
                ),
                {
                    **cleanup,
                },
            )
        conn.execute(
            text(
                """
                UPDATE vectorization_checkpoints
                SET last_source_id = :last_source_id,
                    phase = 'chunking',
                    updated_at = CURRENT_TIMESTAMP
                WHERE job_name = :job_name
                """
            ),
            {"job_name": CHECKPOINT_NAME, "last_source_id": int(rows[-1]["id"])},
        )
    return documents, chunks_written


def _remaining() -> dict:
    sql = """
    SELECT
        (SELECT COUNT(*) FROM source_items WHERE source_code = 'a2aj_case' AND COALESCE(raw_text, '') <> '') AS source_cases,
        (SELECT COUNT(DISTINCT source_id) FROM rag_chunks WHERE source_table = 'source_items' AND source_code = 'a2aj_case') AS chunked_cases,
        (SELECT COUNT(*) FROM rag_chunks WHERE source_table = 'source_items' AND source_code = 'a2aj_case') AS case_chunks,
        (SELECT COUNT(*)
         FROM rag_chunks rc
         LEFT JOIN rag_chunk_embeddings rce ON rce.chunk_id = rc.id
         WHERE rc.source_table = 'source_items' AND rc.source_code = 'a2aj_case'
           AND (rce.chunk_id IS NULL OR rce.content_hash <> rc.content_hash)) AS pending_embeddings
    """
    with engine.connect() as conn:
        return dict(conn.execute(text(sql)).mappings().one())


def _drop_bulk_insert_indexes() -> None:
    if is_sqlite():
        return
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text("DROP INDEX CONCURRENTLY IF EXISTS idx_rag_chunk_embeddings_vector"))
        conn.execute(text("DROP INDEX CONCURRENTLY IF EXISTS idx_rag_chunk_embeddings_active_hnsw"))


def run(args: argparse.Namespace) -> dict:
    log_path = Path(args.log_path)
    started = time.perf_counter()
    # HNSW incremental maintenance is much slower and larger than one final concurrent build.
    os.environ["RAG_VECTOR_DEFER_INDEXES"] = "true"
    os.environ["RAG_VECTOR_SKIP_GENERIC_INDEX"] = "true"
    ensure_rag_tables()
    ensure_vector_tables()
    _ensure_checkpoint_table()
    _drop_bulk_insert_indexes()
    checkpoint = _checkpoint()
    _log(log_path, "started", checkpoint=checkpoint, free_gb=_free_gb(), page_size=args.page_size)

    pages = 0
    documents = 0
    chunks = 0
    while True:
        if _free_gb() < args.min_free_gb:
            raise RuntimeError(
                f"磁盘保护阈值触发：剩余 {_free_gb()} GiB，小于 {args.min_free_gb} GiB；已保留断点。"
            )
        checkpoint = _checkpoint()
        rows = _source_page(int(checkpoint["last_source_id"]), args.page_size)
        if not rows:
            break
        page_documents, page_chunks = _write_chunk_page(rows)
        pages += 1
        documents += page_documents
        chunks += page_chunks
        if pages % args.progress_every_pages == 0:
            status = _remaining()
            _log(
                log_path,
                "chunk_progress",
                pages=pages,
                documents_written=documents,
                chunks_written=chunks,
                last_source_id=int(rows[-1]["id"]),
                free_gb=_free_gb(),
                **status,
            )

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE vectorization_checkpoints SET phase = 'embedding', updated_at = CURRENT_TIMESTAMP "
                "WHERE job_name = :job_name"
            ),
            {"job_name": CHECKPOINT_NAME},
        )

    embedding_batches = 0
    embeddings = 0
    while True:
        if _free_gb() < args.min_free_gb:
            raise RuntimeError(
                f"磁盘保护阈值触发：剩余 {_free_gb()} GiB，小于 {args.min_free_gb} GiB；已保留断点。"
            )
        result = rebuild_chunk_embeddings(
            source_filter="case", limit=args.embedding_batch_limit, module="canada"
        )
        if result["status"] != "completed":
            raise RuntimeError(f"向量生成失败：{result['error_message']}")
        written = int(result["embeddings_written"])
        embeddings += written
        embedding_batches += 1
        if embedding_batches % args.progress_every_embeddings == 0 or written == 0:
            status = _remaining()
            _log(
                log_path,
                "embedding_progress",
                embedding_batches=embedding_batches,
                embeddings_written=embeddings,
                batch_written=written,
                free_gb=_free_gb(),
                **status,
            )
        if written == 0:
            break

    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE vectorization_checkpoints SET phase = 'completed', updated_at = CURRENT_TIMESTAMP "
                "WHERE job_name = :job_name"
            ),
            {"job_name": CHECKPOINT_NAME},
        )
    os.environ["RAG_VECTOR_DEFER_INDEXES"] = "false"
    ensure_vector_tables()
    status = _remaining()
    payload = {
        "status": "completed",
        "documents_written": documents,
        "chunks_written": chunks,
        "embeddings_written": embeddings,
        "free_gb": _free_gb(),
        "duration_seconds": round(time.perf_counter() - started, 3),
        **status,
    }
    _log(log_path, "completed", **payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="A2AJ 全量案例切片与 BGE-M3 向量化（可断点恢复）。")
    parser.add_argument("--page-size", type=int, default=100)
    parser.add_argument("--embedding-batch-limit", type=int, default=512)
    parser.add_argument("--min-free-gb", type=float, default=20.0)
    parser.add_argument("--progress-every-pages", type=int, default=10)
    parser.add_argument("--progress-every-embeddings", type=int, default=10)
    parser.add_argument("--log-path", default="logs/full_case_vectorization.jsonl")
    args = parser.parse_args(argv)
    if args.page_size < 1 or args.embedding_batch_limit < 1:
        parser.error("批次大小必须大于 0")
    try:
        payload = run(args)
        print(json.dumps(payload, ensure_ascii=False, default=str))
        print("[RESULT]: SUCCESS")
        return 0
    except Exception as exc:
        payload = {"status": "failed", "error": str(exc)}
        print(json.dumps(payload, ensure_ascii=False))
        print("[RESULT]: FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
