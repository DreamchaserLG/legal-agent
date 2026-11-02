from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from app.core.config import settings
from app.service.analysis_service import analyze_sentence_search
from app.service.common_service import repair_text


@dataclass
class AnalysisStreamRun:
    run_id: str
    user_id: int
    tenant_id: str
    request: dict[str, Any]
    status: str = "queued"
    events: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    paused: bool = False
    cancelled: bool = False
    control_revision: int = 0
    additional_queries: list[str] = field(default_factory=list)
    condition: threading.Condition = field(default_factory=threading.Condition, repr=False)


_RUNS: dict[str, AnalysisStreamRun] = {}
_LOCK = threading.RLock()
_EXECUTOR = ThreadPoolExecutor(
    max_workers=max(1, min(int(settings.analysis_stream_worker_threads), 8)),
    thread_name_prefix="analysis-stream",
)


def _cleanup() -> None:
    cutoff = time.time() - max(300, int(settings.analysis_stream_event_ttl_seconds))
    with _LOCK:
        expired = [key for key, run in _RUNS.items() if run.updated_at < cutoff and run.status in {"completed", "failed"}]
        for key in expired:
            _RUNS.pop(key, None)


def _emit(run: AnalysisStreamRun, event_type: str, message: str, payload: dict[str, Any] | None = None) -> None:
    with run.condition:
        event = {
            "seq": len(run.events) + 1,
            "event_type": repair_text(event_type),
            "message": repair_text(message),
            "payload": payload or {},
            "timestamp": time.time(),
        }
        run.events.append(event)
        run.updated_at = time.time()
        run.condition.notify_all()


def _worker(run: AnalysisStreamRun) -> None:
    run.status = "running"
    _emit(run, "run_started", "分析任务已进入后台，页面将持续接收研究进度。")
    try:
        payload = dict(run.request)
        payload.update({
            "tenant_id": run.tenant_id,
            "user_id": run.user_id,
            "event_callback": lambda t, m, p=None: _emit(run, t, m, p),
            "research_control": lambda: research_control_snapshot(run),
        })
        run.result = analyze_sentence_search(**payload)
        run.status = "completed"
        _emit(run, "run_completed", "分析已完成，正在打开结构化结果页面。", {
            "result_summary": {
                "laws": len(((run.result or {}).get("module_packet") or {}).get("relevant_laws") or []),
                "cases": len(((run.result or {}).get("module_packet") or {}).get("case_law_rows") or []),
                "rounds": len(((run.result or {}).get("agentic_research") or {}).get("rounds") or []),
            }
        })
    except Exception as exc:
        run.status = "failed"
        run.error = repair_text(str(exc))[:1000]
        _emit(run, "run_failed", "分析未完成，已保留执行轨迹。", {"error": run.error})


def create_analysis_stream_run(request_payload: dict[str, Any], *, user_id: int, tenant_id: str) -> dict[str, Any]:
    _cleanup()
    run = AnalysisStreamRun(
        run_id=uuid4().hex,
        user_id=int(user_id),
        tenant_id=repair_text(tenant_id) or f"user:{user_id}",
        request=request_payload,
    )
    with _LOCK:
        _RUNS[run.run_id] = run
    _emit(run, "run_queued", "任务已创建，等待分析工作线程。")
    _EXECUTOR.submit(_worker, run)
    return serialize_analysis_stream_run(run, include_result=False)


def get_analysis_stream_run(run_id: str, *, user_id: int, include_result: bool = False) -> AnalysisStreamRun:
    with _LOCK:
        run = _RUNS.get(repair_text(run_id))
    if not run or run.user_id != int(user_id):
        raise ValueError("Analysis stream run not found")
    return run


def serialize_analysis_stream_run(run: AnalysisStreamRun, *, include_result: bool = False) -> dict[str, Any]:
    payload = {
        "run_id": run.run_id, "status": run.status, "error": run.error,
        "last_seq": len(run.events), "created_at": run.created_at, "updated_at": run.updated_at,
        "request": run.request,
        "control": {
            "paused": run.paused, "cancelled": run.cancelled,
            "revision": run.control_revision, "additional_queries": list(run.additional_queries),
        },
    }
    if include_result:
        payload["result"] = run.result
    return payload


def wait_for_analysis_events(run: AnalysisStreamRun, after_seq: int, timeout: float = 15.0) -> list[dict[str, Any]]:
    with run.condition:
        available = [event for event in run.events if int(event["seq"]) > int(after_seq)]
        if not available and run.status not in {"completed", "failed"}:
            run.condition.wait(timeout=max(0.1, min(timeout, 30.0)))
        return [event for event in run.events if int(event["seq"]) > int(after_seq)]


def research_control_snapshot(run: AnalysisStreamRun) -> dict[str, Any]:
    with run.condition:
        return {
            "paused": run.paused,
            "cancelled": run.cancelled,
            "revision": run.control_revision,
            "additional_queries": list(run.additional_queries),
        }


def apply_analysis_stream_control(
    run_id: str, *, user_id: int, action: str, query: str = ""
) -> dict[str, Any]:
    run = get_analysis_stream_run(run_id, user_id=user_id)
    clean_action = repair_text(action).lower()
    clean_query = repair_text(query)
    if run.status in {"completed", "failed"}:
        raise ValueError("Analysis stream run is already terminal")
    with run.condition:
        if clean_action == "pause":
            run.paused = True
            message = "研究将在当前检索步骤结束后暂停。"
        elif clean_action == "resume":
            run.paused = False
            message = "研究已继续。"
        elif clean_action == "cancel":
            run.cancelled = True
            run.paused = False
            message = "已请求终止；当前数据库查询结束后不再进入下一轮。"
        elif clean_action == "refine":
            if not clean_query:
                raise ValueError("A refinement query is required")
            if clean_query not in run.additional_queries:
                run.additional_queries.append(clean_query[:500])
            run.paused = False
            message = "人工补充查询已加入下一轮研究计划。"
        else:
            raise ValueError("Unsupported analysis control action")
        run.control_revision += 1
        run.updated_at = time.time()
        run.condition.notify_all()
    _emit(run, f"control_{clean_action}", message, {
        "action": clean_action, "query": clean_query[:500], "revision": run.control_revision,
    })
    return serialize_analysis_stream_run(run, include_result=False)
