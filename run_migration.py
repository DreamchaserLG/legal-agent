#!/usr/bin/env python3
"""全自动将旧案例数据迁移为 BGE-M3 分块语义向量。

脚本默认优先读取旧 case_embeddings，随后兼容当前项目的 legal_cases 和
rag_chunks。也可通过 SOURCE_* 与 A2AJ_* 环境变量接入任意 PostgreSQL 旧库。
没有 input() 或人工确认分支；结束时最后一行恒为 CI 可识别的结果标记。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Sequence

# 国内服务器优先使用 Hugging Face 镜像；必须在导入 huggingface_hub 前设置。
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

try:
    import psycopg2
    from dotenv import load_dotenv
    from pgvector.psycopg2 import register_vector
    from psycopg2 import sql
    from psycopg2.extras import execute_values
    from psycopg2.pool import ThreadedConnectionPool
    from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
    from tqdm import tqdm
except ModuleNotFoundError as dependency_error:
    print(json.dumps({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "level": "ERROR",
        "event": "dependency_missing",
        "message": f"缺少迁移依赖: {dependency_error}. 请先执行 pip install -r requirements.txt。",
    }, ensure_ascii=False))
    print("[RESULT]: FAILED")
    sys.exit(1)


load_dotenv()

MIGRATION_NAME = "case_chunks_bge_m3_v2"
MODEL_NAME = "BAAI/bge-m3"
VECTOR_DIMENSION = 1024
CHUNK_SIZE_TOKENS = 512
CHUNK_OVERLAP_TOKENS = 64
DEFAULT_BATCH_SIZE = 500
BACKUP_PREFIX = "case_embeddings_backup_"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class MigrationError(RuntimeError):
    """迁移无法安全继续时抛出。"""


class QualityGateError(MigrationError):
    """质量门禁失败时抛出，禁止切换新表。"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_text() -> str:
    return utc_now().isoformat(timespec="milliseconds")


def validate_identifier(value: str, setting_name: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value or ""):
        raise MigrationError(f"{setting_name} 不是安全的 PostgreSQL 标识符: {value!r}")
    return value


def normalize_db_url(url: str) -> str:
    clean = url.strip().replace("postgresql+psycopg2://", "postgresql://", 1)
    if not clean.startswith(("postgresql://", "postgres://")):
        raise MigrationError("DB_URL 必须为 PostgreSQL 连接串，不能使用 SQLite。")
    return clean


def bool_env(name: str, default: bool = True) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def int_env(name: str, default: int, *, minimum: int = 0, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise MigrationError(f"环境变量 {name} 必须是整数。") from exc
    if value < minimum:
        raise MigrationError(f"环境变量 {name} 不能小于 {minimum}。")
    return min(value, maximum) if maximum is not None else value


@dataclass(frozen=True)
class Settings:
    db_url: str
    a2aj_db_url: str
    batch_size: int
    embedding_batch_size: int
    auto_approve: bool
    source_table: str
    source_id_column: str
    source_content_column: str
    source_case_number_column: str
    source_court_column: str
    source_decision_date_column: str
    a2aj_source_table: str
    a2aj_id_column: str
    a2aj_content_column: str
    a2aj_case_number_column: str
    a2aj_court_column: str
    a2aj_decision_date_column: str
    a2aj_id_offset: int
    hf_cache_dir: Path
    log_path: Path

    @classmethod
    def from_env(cls) -> "Settings":
        db_url = os.getenv("DB_URL", "").strip() or os.getenv("DATABASE_URL", "").strip()
        if not db_url:
            raise MigrationError("未设置 DB_URL（或兼容的 DATABASE_URL）。")
        a2aj_url = os.getenv("A2AJ_DB_URL", "").strip()
        return cls(
            db_url=normalize_db_url(db_url),
            a2aj_db_url=normalize_db_url(a2aj_url) if a2aj_url else "",
            batch_size=int_env("BATCH_SIZE", DEFAULT_BATCH_SIZE, minimum=1),
            embedding_batch_size=int_env("EMBEDDING_BATCH_SIZE", 32, minimum=1, maximum=256),
            auto_approve=bool_env("AUTO_APPROVE", True),
            source_table=os.getenv("SOURCE_TABLE", "").strip(),
            source_id_column=os.getenv("SOURCE_ID_COLUMN", "id").strip(),
            source_content_column=os.getenv("SOURCE_CONTENT_COLUMN", "content").strip(),
            source_case_number_column=os.getenv("SOURCE_CASE_NUMBER_COLUMN", "case_number").strip(),
            source_court_column=os.getenv("SOURCE_COURT_COLUMN", "court").strip(),
            source_decision_date_column=os.getenv("SOURCE_DECISION_DATE_COLUMN", "decision_date").strip(),
            a2aj_source_table=os.getenv("A2AJ_SOURCE_TABLE", "").strip(),
            a2aj_id_column=os.getenv("A2AJ_ID_COLUMN", "id").strip(),
            a2aj_content_column=os.getenv("A2AJ_CONTENT_COLUMN", "content").strip(),
            a2aj_case_number_column=os.getenv("A2AJ_CASE_NUMBER_COLUMN", "case_number").strip(),
            a2aj_court_column=os.getenv("A2AJ_COURT_COLUMN", "court").strip(),
            a2aj_decision_date_column=os.getenv("A2AJ_DECISION_DATE_COLUMN", "decision_date").strip(),
            a2aj_id_offset=int_env("A2AJ_ID_OFFSET", 0, minimum=0),
            hf_cache_dir=Path(os.getenv("HF_HOME", "data/model_cache/huggingface")),
            log_path=Path(os.getenv("MIGRATION_LOG_PATH", "logs/case_chunk_migration.jsonl")),
        )


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": utc_text(),
            "level": record.levelname,
            "event": getattr(record, "event", "message"),
            "message": record.getMessage(),
        }
        fields = getattr(record, "fields", {})
        if isinstance(fields, dict):
            payload.update(fields)
        return json.dumps(payload, ensure_ascii=False, default=str)


def build_logger(log_path: Path) -> logging.Logger:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("case_chunk_migration")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    formatter = JsonLineFormatter()
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(log_path, encoding="utf-8")):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


def log_event(logger: logging.Logger, event: str, message: str, **fields: Any) -> None:
    logger.info(message, extra={"event": event, "fields": fields})


class DatabasePool:
    """带 ping 检测的连接池；失联时重建连接池供下一次批次自动重试。"""

    def __init__(self, dsn: str, minconn: int = 1, maxconn: int = 6):
        self.dsn = dsn
        self.minconn = minconn
        self.maxconn = maxconn
        self._lock = threading.Lock()
        self._pool = self._new_pool()

    def _new_pool(self) -> ThreadedConnectionPool:
        return ThreadedConnectionPool(self.minconn, self.maxconn, self.dsn, connect_timeout=15)

    def _reconnect(self) -> None:
        with self._lock:
            old_pool = self._pool
            self._pool = self._new_pool()
            old_pool.closeall()

    @contextmanager
    def connection(self, *, autocommit: bool = False) -> Iterator[Any]:
        conn = None
        try:
            conn = self._pool.getconn()
            conn.autocommit = autocommit
            with conn.cursor() as cursor:
                cursor.execute("SELECT 1")
            if not autocommit:
                conn.rollback()  # ping 不应占用后续业务事务。
            yield conn
            if not autocommit:
                conn.commit()
        except (psycopg2.InterfaceError, psycopg2.OperationalError):
            if conn is not None:
                self._pool.putconn(conn, close=True)
                conn = None
            self._reconnect()
            raise
        except Exception:
            if conn is not None and not autocommit:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                self._pool.putconn(conn)

    def close(self) -> None:
        self._pool.closeall()


@dataclass(frozen=True)
class SourceSpec:
    name: str
    pool: DatabasePool
    query: sql.Composed
    id_offset: int = 0


@dataclass(frozen=True)
class CaseRow:
    source_id: int
    case_number: str
    court: str
    decision_date: Any
    raw_text: str


class BgeM3Embedder:
    """从镜像缓存加载模型，并在 CUDA OOM 时自动降级 CPU。"""

    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self.device = "cuda" if self._cuda_available() else "cpu"
        self.model: Any | None = None
        self.tokenizer: Any | None = None
        self.model_path: Path | None = None
        self._lock = threading.Lock()

    @staticmethod
    def _cuda_available() -> bool:
        try:
            import torch

            return bool(torch.cuda.is_available())
        except Exception:
            return False

    def _clear_cuda(self) -> None:
        try:
            import torch

            torch.cuda.empty_cache()
        except Exception:
            pass

    def _load_once(self) -> None:
        from huggingface_hub import snapshot_download
        from sentence_transformers import SentenceTransformer
        from transformers import AutoTokenizer

        self.settings.hf_cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path = snapshot_download(
            repo_id=MODEL_NAME,
            cache_dir=str(self.settings.hf_cache_dir),
            local_files_only=False,
        )
        # 禁止直接以仓库名构造 SentenceTransformer，确保下载、缓存和加载错误可控。
        model = SentenceTransformer(cache_path, device=self.device)
        model.max_seq_length = 8192
        model.to(self.device)
        tokenizer = AutoTokenizer.from_pretrained(cache_path, use_fast=True)
        self.model_path = Path(cache_path)
        self.model = model
        self.tokenizer = tokenizer
        log_event(
            self.logger,
            "embedding_model_loaded",
            "BGE-M3 模型加载完成",
            model_cache_path=str(self.model_path),
            max_seq_length=model.max_seq_length,
            device=self.device,
        )
        print(f"[INFO] Model cache path: {self.model_path}")
        print(f"[INFO] Max sequence length: {model.max_seq_length}")
        print("[INFO] Embedding model ready.")

    def ensure_loaded(self) -> None:
        with self._lock:
            if self.model is not None and self.tokenizer is not None:
                return
            last_error: OSError | None = None
            for attempt in range(1, 4):
                try:
                    self._load_once()
                    return
                except OSError as exc:
                    last_error = exc
                    log_event(
                        self.logger,
                        "embedding_model_retry",
                        "模型网络或缓存错误，10 秒后重试",
                        attempt=attempt,
                        max_attempts=3,
                        error=str(exc),
                    )
                    if attempt < 3:
                        time.sleep(10)
                except RuntimeError as exc:
                    if self.device == "cuda" and "out of memory" in str(exc).lower():
                        self.model = None
                        self.tokenizer = None
                        self.device = "cpu"
                        self._clear_cuda()
                        log_event(self.logger, "embedding_cpu_fallback", "模型加载显存不足，切换至 CPU", error=str(exc))
                        self._load_once()
                        return
                    raise
            raise MigrationError("BGE-M3 模型连续三次加载失败。") from last_error

    def token_count(self, text: str) -> int:
        self.ensure_loaded()
        assert self.tokenizer is not None
        return len(self.tokenizer.encode(text, add_special_tokens=False))

    def _token_windows(self, text: str) -> list[str]:
        """极长且无有效分隔符的文本按 tokenizer token 边界兜底。"""
        assert self.tokenizer is not None
        token_ids = self.tokenizer.encode(text, add_special_tokens=False)
        step = CHUNK_SIZE_TOKENS - CHUNK_OVERLAP_TOKENS
        return [
            self.tokenizer.decode(token_ids[index:index + CHUNK_SIZE_TOKENS], skip_special_tokens=True).strip()
            for index in range(0, len(token_ids), step)
            if token_ids[index:index + CHUNK_SIZE_TOKENS]
        ]

    def split(self, text: str) -> list[str]:
        """使用 RecursiveCharacterTextSplitter，长度和重叠均由 tokenizer 精确度量。"""
        self.ensure_loaded()
        from langchain_text_splitters import RecursiveCharacterTextSplitter

        clean = str(text or "").strip()
        if not clean:
            return []
        splitter = RecursiveCharacterTextSplitter(
            separators=["\n\n", "\n", "。", "！", "？", ".", "!", "?", " ", ""],
            chunk_size=CHUNK_SIZE_TOKENS,
            chunk_overlap=CHUNK_OVERLAP_TOKENS,
            length_function=self.token_count,
            is_separator_regex=False,
            keep_separator=True,
        )
        raw_chunks = [chunk.strip() for chunk in splitter.split_text(clean) if chunk.strip()]
        chunks: list[str] = []
        for chunk in raw_chunks:
            if self.token_count(chunk) <= CHUNK_SIZE_TOKENS:
                chunks.append(chunk)
            else:
                chunks.extend(piece for piece in self._token_windows(chunk) if piece)
        if not chunks:
            chunks = self._token_windows(clean)
        oversized = [index for index, chunk in enumerate(chunks) if self.token_count(chunk) > CHUNK_SIZE_TOKENS]
        if oversized:
            raise MigrationError(f"分块失败：存在超过 {CHUNK_SIZE_TOKENS} token 的分块。")
        return chunks

    @retry(
        retry=retry_if_exception_type((RuntimeError, OSError, ConnectionError)),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    def _encode_once(self, chunks: Sequence[str]) -> list[list[float]]:
        self.ensure_loaded()
        assert self.model is not None
        vectors = self.model.encode(
            list(chunks),
            batch_size=self.settings.embedding_batch_size,
            show_progress_bar=False,
            normalize_embeddings=True,
            convert_to_numpy=True,
        ).tolist()
        if any(len(vector) != VECTOR_DIMENSION for vector in vectors):
            actual = len(vectors[0]) if vectors else 0
            raise MigrationError(f"BGE-M3 输出向量维度错误：期望 {VECTOR_DIMENSION}，实际 {actual}。")
        return [[float(value) for value in vector] for vector in vectors]

    def encode(self, chunks: Sequence[str]) -> list[list[float]]:
        try:
            return self._encode_once(chunks)
        except RuntimeError as exc:
            if self.device == "cuda" and "out of memory" in str(exc).lower():
                with self._lock:
                    self.model = None
                    self.tokenizer = None
                    self.device = "cpu"
                    self._clear_cuda()
                log_event(self.logger, "embedding_cpu_fallback", "向量编码显存不足，切换至 CPU", error=str(exc))
                return self._encode_once(chunks)
            raise


class Migration:
    def __init__(self, settings: Settings, logger: logging.Logger):
        self.settings = settings
        self.logger = logger
        self.pool = DatabasePool(settings.db_url)
        self.a2aj_pool = DatabasePool(settings.a2aj_db_url) if settings.a2aj_db_url else None
        self.embedder = BgeM3Embedder(settings, logger)

    def close(self) -> None:
        self.pool.close()
        if self.a2aj_pool:
            self.a2aj_pool.close()

    def _table_exists(self, pool: DatabasePool, table: str) -> bool:
        with pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("SELECT to_regclass(%s) IS NOT NULL", (table,))
            return bool(cursor.fetchone()[0])

    def _columns(self, pool: DatabasePool, table: str) -> set[str]:
        with pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s
                """,
                (table,),
            )
            return {str(row[0]) for row in cursor.fetchall()}

    def preflight(self) -> None:
        with self.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
            cursor.execute("SELECT extname FROM pg_extension WHERE extname IN ('vector', 'pg_trgm')")
            installed = {str(row[0]) for row in cursor.fetchall()}
        missing = {"vector", "pg_trgm"} - installed
        if missing:
            raise MigrationError(f"PostgreSQL 扩展不可用: {', '.join(sorted(missing))}")
        log_event(self.logger, "preflight_complete", "PostgreSQL 扩展预检完成", extensions=sorted(installed))

    def create_checkpoint_table(self) -> None:
        with self.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS migration_checkpoint (
                    migration_name TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    last_processed_id BIGINT NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'running',
                    details JSONB NOT NULL DEFAULT '{}'::jsonb,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (migration_name, source_name)
                )
                """
            )

    def create_staging_tables(self) -> None:
        with self.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS cases_metadata_v2 (
                    id BIGSERIAL PRIMARY KEY,
                    case_number TEXT UNIQUE,
                    court TEXT,
                    decision_date DATE,
                    raw_text TEXT NOT NULL
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS case_chunks_v2 (
                    id BIGSERIAL PRIMARY KEY,
                    case_id BIGINT NOT NULL REFERENCES cases_metadata_v2(id) ON DELETE CASCADE,
                    chunk_index INT NOT NULL,
                    chunk_text TEXT NOT NULL,
                    embedding VECTOR(1024) NOT NULL,
                    content_tsv TSVECTOR GENERATED ALWAYS AS
                        (to_tsvector('english'::regconfig, COALESCE(chunk_text, ''))) STORED,
                    UNIQUE (case_id, chunk_index)
                )
                """
            )
        log_event(self.logger, "staging_tables_ready", "案例元数据与分块向量暂存表已就绪")

    def _checkpoint(self, source_name: str) -> tuple[int, str]:
        with self.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT last_processed_id, status FROM migration_checkpoint
                WHERE migration_name = %s AND source_name = %s
                """,
                (MIGRATION_NAME, source_name),
            )
            row = cursor.fetchone()
        return (int(row[0]), str(row[1])) if row else (0, "pending")

    def _save_checkpoint(self, source_name: str, last_id: int, status: str, **details: Any) -> None:
        with self.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO migration_checkpoint
                    (migration_name, source_name, last_processed_id, status, details, updated_at)
                VALUES (%s, %s, %s, %s, %s::jsonb, NOW())
                ON CONFLICT (migration_name, source_name) DO UPDATE SET
                    last_processed_id = EXCLUDED.last_processed_id,
                    status = EXCLUDED.status,
                    details = EXCLUDED.details,
                    updated_at = NOW()
                """,
                (MIGRATION_NAME, source_name, last_id, status, json.dumps(details, ensure_ascii=False)),
            )

    def _direct_source(
        self,
        name: str,
        pool: DatabasePool,
        table: str,
        id_column: str,
        content_column: str,
        case_number_column: str,
        court_column: str,
        decision_date_column: str,
        id_offset: int = 0,
    ) -> SourceSpec:
        table = validate_identifier(table, f"{name} 表名")
        id_column = validate_identifier(id_column, f"{name} ID 字段")
        content_column = validate_identifier(content_column, f"{name} 正文字段")
        columns = self._columns(pool, table)
        missing = {id_column, content_column} - columns
        if missing:
            raise MigrationError(f"{name} 数据源缺少字段: {', '.join(sorted(missing))}")

        def text_column(column: str) -> sql.Composed:
            return sql.SQL("COALESCE({}::text, '')").format(sql.Identifier(column)) if column in columns else sql.SQL("''")

        def date_column(column: str) -> sql.Composed:
            return sql.SQL("{}::date").format(sql.Identifier(column)) if column in columns else sql.SQL("NULL::date")

        for setting_name, column in (
            (f"{name} 案号字段", case_number_column),
            (f"{name} 法院字段", court_column),
            (f"{name} 判决日期字段", decision_date_column),
        ):
            if column:
                validate_identifier(column, setting_name)
        query = sql.SQL(
            "SELECT {id_col}::bigint, {case_number}, {court}, {decision_date}, {content} "
            "FROM {table} WHERE {id_col} > %s ORDER BY {id_col} ASC LIMIT %s"
        ).format(
            id_col=sql.Identifier(id_column),
            case_number=text_column(case_number_column),
            court=text_column(court_column),
            decision_date=date_column(decision_date_column),
            content=text_column(content_column),
            table=sql.Identifier(table),
        )
        return SourceSpec(name, pool, query, id_offset)

    def _automatic_source(self, name: str, pool: DatabasePool, id_offset: int = 0) -> SourceSpec:
        """无需人工填写表名时，按旧案例表、完整案例、旧 RAG 分块的优先级探测。"""
        if self._table_exists(pool, "case_embeddings") and {"id", "content"}.issubset(self._columns(pool, "case_embeddings")):
            return self._direct_source(name, pool, "case_embeddings", "id", "content", "", "", "", id_offset)
        if self._table_exists(pool, "legal_cases"):
            query = sql.SQL(
                "SELECT id::bigint, COALESCE(NULLIF(external_uid, ''), NULLIF(source_url, ''), title, id::text), "
                "COALESCE(court_name, ''), judgment_date, "
                "concat_ws(E'\\n', NULLIF(title, ''), NULLIF(summary, ''), NULLIF(facts, ''), "
                "NULLIF(judgment_result, ''), NULLIF(raw_text, '')) "
                "FROM legal_cases WHERE id > %s ORDER BY id ASC LIMIT %s"
            )
            return SourceSpec(name, pool, query, id_offset)
        if self._table_exists(pool, "rag_chunks"):
            query = sql.SQL(
                "SELECT source_id::bigint, source_id::text, COALESCE(MAX(court_level), ''), NULL::date, "
                "string_agg(text_content, E'\\n' ORDER BY chunk_index) "
                "FROM rag_chunks WHERE source_table = 'legal_cases' AND source_id > %s "
                "GROUP BY source_id ORDER BY source_id ASC LIMIT %s"
            )
            return SourceSpec(name, pool, query, id_offset)
        raise MigrationError(f"未在 {name} 数据库中找到可迁移案例源，请设置对应 SOURCE_TABLE 环境变量。")

    def _resolve_primary_source(self) -> SourceSpec:
        if self.settings.source_table:
            return self._direct_source(
                "primary", self.pool, self.settings.source_table, self.settings.source_id_column,
                self.settings.source_content_column, self.settings.source_case_number_column,
                self.settings.source_court_column, self.settings.source_decision_date_column,
            )
        return self._automatic_source("primary", self.pool)

    def resolve_sources(self) -> list[SourceSpec]:
        sources = [self._resolve_primary_source()]
        if self.settings.a2aj_source_table and not self.a2aj_pool:
            raise MigrationError("设置 A2AJ_SOURCE_TABLE 时必须同时设置 A2AJ_DB_URL。")
        if self.a2aj_pool:
            if self.settings.a2aj_source_table:
                sources.append(
                    self._direct_source(
                        "a2aj", self.a2aj_pool, self.settings.a2aj_source_table,
                        self.settings.a2aj_id_column, self.settings.a2aj_content_column,
                        self.settings.a2aj_case_number_column, self.settings.a2aj_court_column,
                        self.settings.a2aj_decision_date_column, self.settings.a2aj_id_offset,
                    )
                )
            else:
                sources.append(self._automatic_source("a2aj", self.a2aj_pool, self.settings.a2aj_id_offset))
        return sources

    def _read_batch(self, source: SourceSpec, last_id: int) -> list[CaseRow]:
        with source.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(source.query, (last_id, self.settings.batch_size))
            rows = cursor.fetchall()
        return [
            CaseRow(int(row[0]), str(row[1] or ""), str(row[2] or ""), row[3], str(row[4] or "").strip())
            for row in rows
        ]

    @staticmethod
    def _vector_literal(vector: Sequence[float]) -> str:
        return "[" + ",".join(f"{float(value):.8g}" for value in vector) + "]"

    def _embed_records(self, records: list[tuple[int, int, str]]) -> list[tuple[int, int, str, str]]:
        """按模型微批编码，避免 500 个长文书的全部分块同时占用显存。"""
        output: list[tuple[int, int, str, str]] = []
        progress = tqdm(total=len(records), desc="生成分块向量", unit="块", leave=False, dynamic_ncols=True)
        try:
            for start in range(0, len(records), self.settings.embedding_batch_size):
                part = records[start:start + self.settings.embedding_batch_size]
                vectors = self.embedder.encode([item[2] for item in part])
                output.extend(
                    (case_id, chunk_index, chunk_text, self._vector_literal(vector))
                    for (case_id, chunk_index, chunk_text), vector in zip(part, vectors)
                )
                progress.update(len(part))
        finally:
            progress.close()
        return output

    def _write_batch(self, cases: list[CaseRow], source: SourceSpec) -> tuple[int, int]:
        case_rows: list[tuple[int, str, str, Any, str]] = []
        chunk_records: list[tuple[int, int, str]] = []
        for case in cases:
            case_id = case.source_id + source.id_offset
            if case_id <= 0:
                raise MigrationError(f"案例 ID 必须为正数，当前值: {case_id}")
            if not case.raw_text:
                continue
            # 来源前缀保证多个数据库的 case_number 唯一，同时保留原始案号可追溯性。
            number = case.case_number.strip() or str(case.source_id)
            case_rows.append((case_id, f"{source.name}:{number}", case.court.strip(), case.decision_date, case.raw_text))
            for chunk_index, chunk_text in enumerate(self.embedder.split(case.raw_text)):
                chunk_records.append((case_id, chunk_index, chunk_text))
        if not case_rows:
            return 0, 0
        encoded_chunks = self._embed_records(chunk_records)
        case_ids = [row[0] for row in case_rows]
        with self.pool.connection() as conn, conn.cursor() as cursor:
            execute_values(
                cursor,
                """
                INSERT INTO cases_metadata_v2 (id, case_number, court, decision_date, raw_text)
                VALUES %s
                ON CONFLICT (id) DO UPDATE SET
                    case_number = EXCLUDED.case_number,
                    court = EXCLUDED.court,
                    decision_date = EXCLUDED.decision_date,
                    raw_text = EXCLUDED.raw_text
                """,
                case_rows,
                page_size=self.settings.batch_size,
            )
            cursor.execute("DELETE FROM case_chunks_v2 WHERE case_id = ANY(%s)", (case_ids,))
            if encoded_chunks:
                execute_values(
                    cursor,
                    """
                    INSERT INTO case_chunks_v2 (case_id, chunk_index, chunk_text, embedding)
                    VALUES %s
                    """,
                    encoded_chunks,
                    template="(%s, %s, %s, %s::vector)",
                    page_size=self.settings.batch_size,
                )
        return len(case_rows), len(encoded_chunks)

    def migrate_source(self, source: SourceSpec) -> tuple[int, int]:
        last_id, status = self._checkpoint(source.name)
        if status == "completed":
            log_event(self.logger, "source_skipped", "数据源已经完成，跳过", source=source.name, last_processed_id=last_id)
            return 0, 0
        cases_written = 0
        chunks_written = 0
        batch_no = 0
        progress = tqdm(desc=f"迁移 {source.name}", unit="案例", dynamic_ncols=True)
        try:
            while True:
                batch = self._read_batch(source, last_id)
                if not batch:
                    self._save_checkpoint(source.name, last_id, "completed", cases_written=cases_written, chunks_written=chunks_written)
                    break
                batch_no += 1
                started = time.perf_counter()
                written_cases, written_chunks = self._write_batch(batch, source)
                last_id = batch[-1].source_id
                cases_written += written_cases
                chunks_written += written_chunks
                elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
                self._save_checkpoint(
                    source.name,
                    last_id,
                    "running",
                    batch_no=batch_no,
                    cases_written=cases_written,
                    chunks_written=chunks_written,
                    duration_ms=elapsed_ms,
                )
                progress.update(len(batch))
                log_event(
                    self.logger,
                    "batch_completed",
                    "批次迁移完成",
                    source=source.name,
                    batch_no=batch_no,
                    source_cases=len(batch),
                    cases_written=written_cases,
                    chunks_written=written_chunks,
                    last_processed_id=last_id,
                    duration_ms=elapsed_ms,
                    device=self.embedder.device,
                )
        except Exception as exc:
            self._save_checkpoint(source.name, last_id, "failed", error=str(exc), cases_written=cases_written, chunks_written=chunks_written)
            raise
        finally:
            progress.close()
        return cases_written, chunks_written

    def _create_index(self, name: str, statement: str) -> None:
        with self.pool.connection(autocommit=True) as conn, conn.cursor() as cursor:
            cursor.execute(statement)
        log_event(self.logger, "index_completed", "并发索引创建完成", index=name)

    def build_indexes(self) -> None:
        statements = {
            "idx_chunks_embedding_v2": (
                "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_embedding_v2 "
                "ON case_chunks_v2 USING hnsw (embedding vector_cosine_ops) "
                "WITH (m = 16, ef_construction = 64)"
            ),
            "idx_chunks_tsv_v2": "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_chunks_tsv_v2 ON case_chunks_v2 USING gin (content_tsv)",
            "idx_case_chunks_v2_case": "CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_case_chunks_v2_case ON case_chunks_v2 (case_id, chunk_index)",
        }
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="create-index") as executor:
            futures = [executor.submit(self._create_index, name, statement) for name, statement in statements.items()]
            for future in as_completed(futures):
                future.result()

    def synchronize_sequences(self) -> None:
        """显式写入源案例 ID 后推进 BIGSERIAL，避免后续自动插入发生主键冲突。"""
        with self.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT setval(
                    pg_get_serial_sequence('cases_metadata_v2', 'id'),
                    GREATEST(COALESCE((SELECT MAX(id) FROM cases_metadata_v2), 1), 1),
                    true
                )
                """
            )

    def quality_gate(self) -> float:
        """抽样分块，以原 chunk_text 二次编码与写入向量的余弦验证稳定性。"""
        with self.pool.connection() as conn:
            register_vector(conn)
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT id, chunk_text, embedding FROM case_chunks_v2 ORDER BY random() LIMIT 1000"
                )
                rows = cursor.fetchall()
        if not rows:
            raise QualityGateError("质量门禁失败：没有任何有效案例分块。")
        scores: list[float] = []
        for start in range(0, len(rows), self.settings.embedding_batch_size):
            part = rows[start:start + self.settings.embedding_batch_size]
            vectors = self.embedder.encode([str(row[1]) for row in part])
            for row, regenerated in zip(part, vectors):
                stored = [float(value) for value in row[2]]
                if len(stored) != VECTOR_DIMENSION:
                    raise QualityGateError(f"质量门禁失败：分块 {row[0]} 的向量维度异常。")
                scores.append(sum(left * right for left, right in zip(stored, regenerated)))
        mean_score = sum(scores) / len(scores)
        log_event(self.logger, "quality_gate", "质量门禁完成", sample_size=len(scores), mean_cosine_similarity=mean_score)
        if mean_score < 0.95:
            raise QualityGateError(f"质量门禁失败：平均余弦相似度 {mean_score:.6f} < 0.95，未切换任何表。")
        return mean_score

    def _is_switched(self) -> bool:
        _, status = self._checkpoint("switch")
        return status == "switched" and self._table_exists(self.pool, "cases_metadata") and self._table_exists(self.pool, "case_chunks")

    def cleanup_expired_backups(self) -> list[str]:
        deadline = utc_now() - timedelta(hours=72)
        with self.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname = current_schema() AND tablename LIKE %s",
                (f"{BACKUP_PREFIX}%",),
            )
            tables = [str(row[0]) for row in cursor.fetchall()]
        removed: list[str] = []
        for table in tables:
            match = re.match(rf"^{BACKUP_PREFIX}(\d{{14}})", table)
            if not match:
                continue
            created = datetime.strptime(match.group(1), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            if created < deadline:
                with self.pool.connection() as conn, conn.cursor() as cursor:
                    cursor.execute(sql.SQL("DROP TABLE {}").format(sql.Identifier(table)))
                removed.append(table)
        if removed:
            log_event(self.logger, "backup_cleanup", "已清理超过 72 小时的旧向量备份", tables=removed)
        return removed

    def atomic_switch(self) -> str:
        if self._is_switched():
            log_event(self.logger, "switch_skipped", "新分块表已激活，保持幂等")
            return "already_switched"
        old_embeddings_exists = self._table_exists(self.pool, "case_embeddings")
        final_tables_exist = self._table_exists(self.pool, "cases_metadata") or self._table_exists(self.pool, "case_chunks")
        if final_tables_exist:
            raise MigrationError("目标正式表已存在但没有完成切换检查点，已停止以防覆盖现有数据。")
        stamp = utc_now().strftime("%Y%m%d%H%M%S")
        backup_table = f"{BACKUP_PREFIX}{stamp}"
        with self.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute("BEGIN")
            if old_embeddings_exists:
                cursor.execute("ALTER TABLE case_embeddings RENAME TO case_embeddings_old")
            cursor.execute("ALTER TABLE cases_metadata_v2 RENAME TO cases_metadata")
            cursor.execute("ALTER TABLE case_chunks_v2 RENAME TO case_chunks")
            if old_embeddings_exists:
                cursor.execute(sql.SQL("ALTER TABLE {} RENAME TO {}").format(
                    sql.Identifier("case_embeddings_old"), sql.Identifier(backup_table)
                ))
            cursor.execute("COMMIT")
        mode = "switched_with_backup" if old_embeddings_exists else "initial_activation"
        self._save_checkpoint("switch", 0, "switched", mode=mode, backup_table=backup_table if old_embeddings_exists else "")
        log_event(self.logger, "switch_completed", "分块向量表原子切换完成", mode=mode, backup_table=backup_table if old_embeddings_exists else None)
        return mode

    def deploy_hybrid_function(self) -> None:
        with self.pool.connection() as conn, conn.cursor() as cursor:
            cursor.execute(
                """
                CREATE OR REPLACE FUNCTION hybrid_search(
                    query_text TEXT,
                    query_vector VECTOR(1024),
                    result_limit INTEGER DEFAULT 10
                )
                RETURNS TABLE (
                    case_id BIGINT,
                    case_number TEXT,
                    court TEXT,
                    decision_date DATE,
                    chunk_index INTEGER,
                    chunk_text TEXT,
                    vector_score DOUBLE PRECISION,
                    text_score DOUBLE PRECISION,
                    rrf_score DOUBLE PRECISION
                )
                LANGUAGE sql STABLE AS $$
                    WITH params AS (
                        SELECT GREATEST(COALESCE(result_limit, 10), 1) AS max_rows
                    ),
                    vector_ranked AS (
                        SELECT cc.id,
                               1 - (cc.embedding <=> query_vector) AS vector_score,
                               row_number() OVER (ORDER BY cc.embedding <=> query_vector) AS rank_no
                        FROM case_chunks cc, params
                        WHERE query_vector IS NOT NULL
                        ORDER BY cc.embedding <=> query_vector
                        LIMIT (SELECT max_rows * 4 FROM params)
                    ),
                    text_ranked AS (
                        SELECT cc.id,
                               ts_rank_cd(cc.content_tsv, plainto_tsquery('english', COALESCE(query_text, ''))) AS text_score,
                               row_number() OVER (ORDER BY ts_rank_cd(cc.content_tsv, plainto_tsquery('english', COALESCE(query_text, ''))) DESC) AS rank_no
                        FROM case_chunks cc, params
                        WHERE COALESCE(query_text, '') <> ''
                          AND cc.content_tsv @@ plainto_tsquery('english', query_text)
                        ORDER BY ts_rank_cd(cc.content_tsv, plainto_tsquery('english', query_text)) DESC
                        LIMIT (SELECT max_rows * 4 FROM params)
                    ),
                    fused AS (
                        SELECT COALESCE(v.id, t.id) AS id,
                               COALESCE(v.vector_score, 0)::double precision AS vector_score,
                               COALESCE(t.text_score, 0)::double precision AS text_score,
                               (COALESCE(1.0 / (60 + v.rank_no), 0.0) + COALESCE(1.0 / (60 + t.rank_no), 0.0))::double precision AS rrf_score
                        FROM vector_ranked v FULL OUTER JOIN text_ranked t ON t.id = v.id
                    )
                    SELECT cm.id, cm.case_number, cm.court, cm.decision_date, cc.chunk_index, cc.chunk_text,
                           fused.vector_score, fused.text_score, fused.rrf_score
                    FROM fused
                    JOIN case_chunks cc ON cc.id = fused.id
                    JOIN cases_metadata cm ON cm.id = cc.case_id
                    ORDER BY fused.rrf_score DESC, fused.vector_score DESC
                    LIMIT (SELECT max_rows FROM params)
                $$
                """
            )
        log_event(self.logger, "hybrid_function_ready", "RRF 混合检索函数已部署", function="hybrid_search")

    def run(self) -> dict[str, Any]:
        started = time.perf_counter()
        self.preflight()
        self.create_checkpoint_table()
        self.cleanup_expired_backups()
        if self._is_switched():
            self.deploy_hybrid_function()
            return {"status": "already_switched", "duration_ms": round((time.perf_counter() - started) * 1000, 2)}
        self.create_staging_tables()
        sources = self.resolve_sources()
        log_event(self.logger, "sources_resolved", "迁移数据源已确定", sources=[source.name for source in sources])
        total_cases = 0
        total_chunks = 0
        for source in sources:
            cases, chunks = self.migrate_source(source)
            total_cases += cases
            total_chunks += chunks
        self.synchronize_sequences()
        self.build_indexes()
        mean_similarity = self.quality_gate()
        switch_mode = self.atomic_switch()
        self.deploy_hybrid_function()
        result = {
            "status": "switched",
            "cases_written": total_cases,
            "chunks_written": total_chunks,
            "mean_cosine_similarity": mean_similarity,
            "switch_mode": switch_mode,
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }
        log_event(self.logger, "migration_completed", "分块语义向量迁移完成", **result)
        return result


def main() -> int:
    migration: Migration | None = None
    logger: logging.Logger | None = None
    try:
        settings = Settings.from_env()
        logger = build_logger(settings.log_path)
        log_event(
            logger,
            "migration_started",
            "开始全自动 BGE-M3 分块语义向量迁移",
            model=MODEL_NAME,
            batch_size=settings.batch_size,
            chunk_size_tokens=CHUNK_SIZE_TOKENS,
            chunk_overlap_tokens=CHUNK_OVERLAP_TOKENS,
            auto_approve=settings.auto_approve,
            hf_endpoint=os.environ["HF_ENDPOINT"],
        )
        migration = Migration(settings, logger)
        migration.run()
        print("[RESULT]: SUCCESS")
        return 0
    except Exception as exc:
        if logger:
            log_event(logger, "migration_failed", "迁移失败；质量门禁失败时未切换表", error=str(exc), error_type=type(exc).__name__)
        else:
            print(json.dumps({"timestamp": utc_text(), "level": "ERROR", "event": "migration_failed", "message": str(exc)}, ensure_ascii=False))
        print("[RESULT]: FAILED")
        return 1
    finally:
        if migration:
            migration.close()


if __name__ == "__main__":
    sys.exit(main())
