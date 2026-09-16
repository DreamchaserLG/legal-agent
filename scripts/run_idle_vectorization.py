"""在 Windows 空闲时以 CPU 低优先级补齐加拿大法律和案例的 BGE-M3 向量。"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path

# 必须在导入项目配置及 sentence-transformers 前生效。
WORKER_DEVICE = os.getenv("VECTOR_WORKER_DEVICE", "cpu").strip().lower()
if WORKER_DEVICE not in {"cpu", "cuda"}:
    raise RuntimeError("VECTOR_WORKER_DEVICE 仅支持 cpu 或 cuda。")
CPU_THREADS = max(1, int(os.getenv("VECTOR_WORKER_CPU_THREADS", "1") or 1))
if WORKER_DEVICE == "cpu":
    os.environ["CUDA_VISIBLE_DEVICES"] = "-1"
else:
    os.environ.pop("CUDA_VISIBLE_DEVICES", None)
os.environ["EMBEDDING_DEVICE"] = WORKER_DEVICE
os.environ["EMBEDDING_BATCH_SIZE"] = os.getenv(
    "VECTOR_WORKER_EMBEDDING_BATCH_SIZE", "2" if WORKER_DEVICE == "cpu" else "8"
)
os.environ["OMP_NUM_THREADS"] = str(CPU_THREADS if WORKER_DEVICE == "cpu" else 1)
os.environ["MKL_NUM_THREADS"] = str(CPU_THREADS if WORKER_DEVICE == "cpu" else 1)
os.environ["OPENBLAS_NUM_THREADS"] = str(CPU_THREADS if WORKER_DEVICE == "cpu" else 1)
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
# 小批量写入期间绝不触发 HNSW 构建；完整索引应在独立维护窗口创建。
os.environ["RAG_VECTOR_DEFER_INDEXES"] = "true"
os.environ["RAG_VECTOR_SKIP_GENERIC_INDEX"] = "true"

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import text

from app.core.database import engine, is_sqlite
from app.service.common_service import repair_text
from app.service.rag_service import (
    CANADA_LAW_SOURCE_CODES,
    _document_text,
    _source_kind,
    _upsert_chunk,
    ensure_rag_tables,
    split_into_chunks,
)
from app.service.vector_store_service import rebuild_chunk_embeddings


def _log(path: Path, event: str, **payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **payload}
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _pid_path(log_path: Path) -> Path:
    return log_path.parent / "idle_vectorization.pid"


def _switch_request_path(log_path: Path) -> Path:
    return log_path.parent / "vectorization_switch.request"


def _switch_requested(log_path: Path) -> bool:
    return _switch_request_path(log_path).exists()


def _write_pid(log_path: Path) -> None:
    path = _pid_path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{os.getpid()}\n", encoding="ascii")


def _clear_pid(log_path: Path) -> None:
    path = _pid_path(log_path)
    try:
        if path.exists() and path.read_text(encoding="ascii").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def _chunk_totals() -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT source_kind, COUNT(*) AS total
                FROM rag_chunks
                WHERE source_kind IN ('law', 'case')
                GROUP BY source_kind
                """
            )
        ).mappings().all()
    totals = {"law": 0, "case": 0}
    totals.update({str(row["source_kind"]): int(row["total"] or 0) for row in rows})
    return totals


def _embedding_progress(payload: dict[str, object], totals: dict[str, int]) -> dict[str, dict[str, int | float]]:
    """返回本批次写入后的统一结构化进度，供 JSON 与文本日志共同使用。"""
    result = payload.get("result")
    written = int(result.get("embeddings_written") or 0) if isinstance(result, dict) else 0
    source = str(payload.get("source_filter") or "")
    pending_before = payload.get("pending") if isinstance(payload.get("pending"), dict) else {}
    pending_after = {kind: max(0, int(pending_before.get(kind) or 0)) for kind in ("case", "law")}
    if source in pending_after:
        pending_after[source] = max(0, pending_after[source] - written)

    progress: dict[str, dict[str, int | float]] = {}
    for kind in ("case", "law"):
        total = max(0, int(totals.get(kind) or 0))
        pending = pending_after[kind]
        completed = max(0, total - pending)
        progress[kind] = {
            "total": total,
            "completed": completed,
            "pending": pending,
            "percentage": round((completed * 100 / total) if total else 0.0, 4),
        }
    return progress


def _write_batch_progress(log_path: Path, payload: dict[str, object], totals: dict[str, int]) -> None:
    """每个固定向量批次完成后写入一行可直接 tail 的进度日志。"""
    result = payload.get("result")
    if payload.get("action") != "embedding" or not isinstance(result, dict):
        return
    source = str(payload.get("source_filter") or "")
    written = int(result.get("embeddings_written") or 0)
    progress = payload.get("progress")
    if not isinstance(progress, dict):
        progress = _embedding_progress(payload, totals)

    def describe(kind: str) -> str:
        values = progress.get(kind, {})
        total = int(values.get("total") or 0)
        pending = int(values.get("pending") or 0)
        completed = int(values.get("completed") or 0)
        percentage = float(values.get("percentage") or 0.0)
        return f"{kind}=完成 {completed}/{total} ({percentage:.2f}%), 待处理 {pending}"

    progress_path = log_path.parent / "vectorization_batch_progress.log"
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    encoding = "utf-8-sig" if not progress_path.exists() else "utf-8"
    line = (
        f"{datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')} | "
        f"设备={WORKER_DEVICE} | 来源={source} | 固定批次={result.get('chunks_seen', 0)} | "
        f"本批写入={written} | 状态={result.get('status', '')} | "
        f"{describe('case')} | {describe('law')} | 耗时={result.get('duration_seconds', 0)}秒\n"
    )
    with progress_path.open("a", encoding=encoding) as handle:
        handle.write(line)


def _limit_process_resources(cpu_core: int, cpu_cores: int) -> dict[str, object]:
    """CPU 模式让出资源；GPU 模式保留必要的 CPU 调度能力。"""
    result: dict[str, object] = {"priority": "unsupported", "cpu_core": cpu_core, "cpu_cores": cpu_cores}
    if os.name != "nt":
        return result
    kernel32 = ctypes.windll.kernel32
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.SetPriorityClass.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.SetPriorityClass.restype = wintypes.BOOL
    kernel32.SetProcessAffinityMask.argtypes = [wintypes.HANDLE, ctypes.c_size_t]
    kernel32.SetProcessAffinityMask.restype = wintypes.BOOL
    process = kernel32.GetCurrentProcess()
    priority_class = 0x40 if WORKER_DEVICE == "cpu" else 0x4000  # IDLE / BELOW_NORMAL
    if not kernel32.SetPriorityClass(process, priority_class):
        result["priority"] = f"failed:{ctypes.get_last_error()}"
        return result
    if WORKER_DEVICE == "cuda":
        result.update({"priority": "below_normal", "affinity": "unrestricted"})
        return result
    available = max(1, os.cpu_count() or 1)
    selected = max(0, min(cpu_core, available - 1))
    count = max(1, min(cpu_cores, available - selected))
    mask = sum(1 << core for core in range(selected, selected + count))
    result.update({"priority": "idle", "cpu_core": selected, "cpu_cores": count})
    if not kernel32.SetProcessAffinityMask(process, mask):
        result["affinity"] = f"failed:{ctypes.get_last_error()}"
    else:
        result["affinity"] = ",".join(f"core_{core}" for core in range(selected, selected + count))
    return result


def _gpu_snapshot() -> dict[str, int | bool | str]:
    command = [
        "nvidia-smi",
        "--query-gpu=memory.used,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True, text=True, timeout=8)
        first = next((line.strip() for line in completed.stdout.splitlines() if line.strip()), "")
        parts = [part.strip() for part in first.split(",")]
        if len(parts) != 2:
            raise ValueError(f"无法解析 nvidia-smi 输出：{first}")
        return {"available": True, "memory_used_mb": int(parts[0]), "gpu_utilization": int(parts[1])}
    except Exception as exc:
        # 无法读取状态时保持暂停，比错误地在游戏期间运行更安全。
        return {"available": False, "reason": str(exc)}


def _next_unchunked_law_source() -> dict | None:
    """只挑选没有任意切片的新法规源记录，已完成文档不会被重复切片。"""
    sql = """
        SELECT si.id, si.source_code, si.source_uid, si.title, si.item_url,
               si.published_at, si.summary, si.raw_text, si.raw_json
        FROM source_items si
        WHERE si.source_code = ANY(:source_codes)
          AND COALESCE(si.raw_text, '') <> ''
          AND NOT EXISTS (
              SELECT 1
              FROM rag_chunks rc
              WHERE rc.source_table = 'source_items' AND rc.source_id = si.id
          )
        ORDER BY si.id
        LIMIT 1
    """
    with engine.connect() as conn:
        row = conn.execute(text(sql), {"source_codes": sorted(CANADA_LAW_SOURCE_CODES)}).mappings().first()
    return dict(row) if row else None


def _chunk_law_source(row: dict) -> dict[str, int]:
    metadata = row.get("raw_json") if isinstance(row.get("raw_json"), dict) else {}
    document = {
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
    chunks = split_into_chunks(document["text"])
    if not chunks:
        return {"documents": 0, "chunks": 0}
    with engine.begin() as conn:
        for index, chunk in enumerate(chunks):
            _upsert_chunk(conn, document, index, chunk)
    return {"documents": 1, "chunks": len(chunks)}


def _pending_counts() -> dict[str, int]:
    sql = """
        SELECT rc.source_kind, COUNT(*) AS pending
        FROM rag_chunks rc
        LEFT JOIN rag_chunk_embeddings rce ON rce.chunk_id = rc.id
          AND rce.embedding_provider = 'sentence_transformers'
          AND rce.embedding_model = 'BAAI/bge-m3'
        WHERE rc.source_kind IN ('law', 'case') AND rce.chunk_id IS NULL
        GROUP BY rc.source_kind
    """
    with engine.connect() as conn:
        rows = conn.execute(text(sql)).mappings().all()
    result = {"law": 0, "case": 0}
    result.update({str(row["source_kind"]): int(row["pending"] or 0) for row in rows})
    return result


def _validate_hnsw_state() -> list[dict[str, object]]:
    """检查 HNSW 索引状态；无效索引仅警告不阻断，B-Tree 与检查点写入不受影响。"""
    if is_sqlite():
        return []
    sql = """
        SELECT c.relname AS index_name, i.indisvalid, i.indisready
        FROM pg_class c
        JOIN pg_index i ON i.indexrelid = c.oid
        JOIN pg_am am ON am.oid = c.relam
        WHERE c.relnamespace = 'public'::regnamespace AND am.amname = 'hnsw'
        ORDER BY c.relname
    """
    try:
        with engine.connect() as conn:
            rows = [dict(row) for row in conn.execute(text(sql)).mappings().all()]
    except Exception as exc:
        print(f"[WARN] HNSW 索引状态检查跳过：{exc}")
        return []
    invalid = [row for row in rows if not row["indisvalid"] or not row["indisready"]]
    if invalid:
        print(f"[WARN] 检测到无效 HNSW 索引（不阻断向量写入）：{invalid}")
    return rows


def _run_one_action(batch_limit: int, dry_run: bool, preferred_source: str) -> dict[str, object]:
    law_row = _next_unchunked_law_source()
    if law_row:
        if dry_run:
            return {"action": "law_chunk", "dry_run": True, "source_id": int(law_row["id"])}
        result = _chunk_law_source(law_row)
        return {"action": "law_chunk", "source_id": int(law_row["id"]), **result}

    pending = _pending_counts()
    if pending.get(preferred_source, 0):
        source_filter = preferred_source
    else:
        source_filter = "case" if preferred_source == "law" else "law"
    if not pending[source_filter]:
        return {"action": "idle", "pending": pending}
    if dry_run:
        return {"action": "embedding", "dry_run": True, "source_filter": source_filter, "pending": pending}
    result = rebuild_chunk_embeddings(source_filter=source_filter, limit=batch_limit, module="canada")
    return {"action": "embedding", "source_filter": source_filter, "pending": pending, "result": result}


def run(args: argparse.Namespace) -> int:
    _write_pid(args.log_path)
    totals: dict[str, int] = {"law": 0, "case": 0}
    hnsw_indexes: list[dict[str, object]] = []
    resource_limit: dict[str, object] = {"cpu": args.cpu_cores}
    try:
        _log(
            args.log_path,
            "scheduler_started",
            device=WORKER_DEVICE,
            resource_limit=resource_limit,
            batch_limit=args.batch_limit,
            hnsw_indexes=[],
            totals_ready=False,
        )
    except Exception as exc:
        print(f"[WARN] scheduler_started 日志写入失败：{exc}")
    try:
        resource_limit = _limit_process_resources(args.cpu_core, args.cpu_cores)
    except Exception as exc:
        print(f"[WARN] 资源限制设置失败，继续运行：{exc}")
    try:
        hnsw_indexes = _validate_hnsw_state()
    except Exception as exc:
        print(f"[WARN] HNSW 校验失败（不阻断）：{exc}")
        hnsw_indexes = []
    try:
        totals = _chunk_totals()
    except Exception as exc:
        print(f"[WARN] totals 初始统计失败，将在后续执行中重试：{exc}")
    try:
        _log(
            args.log_path,
            "scheduler_initialized",
            device=WORKER_DEVICE,
            resource_limit=resource_limit,
            batch_limit=args.batch_limit,
            hnsw_indexes=hnsw_indexes,
            totals=totals,
        )
    except Exception as exc:
        print(f"[WARN] scheduler_initialized 日志写入失败：{exc}")
    try:
        idle_since: float | None = None
        actions = 0
        next_source = "law"
        while True:
            if _switch_requested(args.log_path):
                _log(
                    args.log_path,
                    "scheduler_stopped_for_switch",
                    device=WORKER_DEVICE,
                    resource_limit=resource_limit,
                    hnsw_indexes=hnsw_indexes,
                )
                return 0
            gpu = _gpu_snapshot()
            ignore_gpu_gate = os.getenv("VECTOR_WORKER_IGNORE_GPU_GATE", "false").lower() in {"1", "true", "yes", "on"}
            busy = not ignore_gpu_gate and (
                not gpu.get("available", False)
                or int(gpu.get("gpu_utilization", 100)) >= args.busy_gpu_percent
                or int(gpu.get("memory_used_mb", 1_000_000)) >= args.busy_memory_mb
            )
            if busy:
                idle_since = None
                _log(args.log_path, "paused_for_gpu", gpu=gpu)
            else:
                now = time.monotonic()
                idle_since = idle_since or now
                idle_seconds = now - idle_since
                if idle_seconds >= args.min_idle_seconds:
                    try:
                        if totals.get("law", 0) == 0 and totals.get("case", 0) == 0:
                            totals = _chunk_totals()
                    except Exception:
                        pass
                    payload = _run_one_action(args.batch_limit, args.dry_run, next_source)
                    source_filter = payload.get("source_filter")
                    if source_filter in {"law", "case"}:
                        next_source = "case" if source_filter == "law" else "law"
                    actions += 1
                    if payload.get("action") == "embedding":
                        payload["progress"] = _embedding_progress(payload, totals)
                    _log(args.log_path, "action_completed", gpu=gpu, resource_limit=resource_limit, **payload)
                    _write_batch_progress(args.log_path, payload, totals)
                    if args.once or (args.max_actions and actions >= args.max_actions):
                        print(json.dumps(payload, ensure_ascii=False, default=str))
                        print("[RESULT]: SUCCESS")
                        return 0
                else:
                    _log(args.log_path, "waiting_for_idle_window", gpu=gpu, idle_seconds=round(idle_seconds, 1))
            if args.once:
                print(json.dumps({"status": "paused", "gpu": gpu}, ensure_ascii=False, default=str))
                print("[RESULT]: SUCCESS")
                return 0
            time.sleep(args.poll_seconds)
    finally:
        _clear_pid(args.log_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="仅在 GPU 空闲时以 CPU 低优先级运行 BGE-M3 向量化。")
    parser.add_argument("--batch-limit", type=int, default=2, help="每次空闲窗口最多写入的向量数。")
    parser.add_argument("--cpu-core", type=int, default=0, help="后台进程固定使用的逻辑核心编号。")
    parser.add_argument("--cpu-cores", type=int, default=1, help="CPU 模式使用的连续逻辑核心数。")
    parser.add_argument("--busy-gpu-percent", type=int, default=15)
    parser.add_argument("--busy-memory-mb", type=int, default=2500)
    parser.add_argument("--min-idle-seconds", type=int, default=90)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--max-actions", type=int, default=0, help="0 表示持续运行。")
    parser.add_argument("--once", action="store_true", help="只检查并尝试执行一个动作。")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-path", type=Path, default=Path("logs/idle_vectorization.jsonl"))
    args = parser.parse_args(argv)
    if args.batch_limit < 1 or args.cpu_cores < 1 or args.poll_seconds < 1 or args.min_idle_seconds < 0:
        parser.error("批次、轮询间隔和空闲时间必须为有效正数。")
    try:
        ensure_rag_tables()
        return run(args)
    except Exception as exc:
        _log(args.log_path, "scheduler_failed", error=str(exc))
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        print("[RESULT]: FAILED")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
