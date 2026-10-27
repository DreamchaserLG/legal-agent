from __future__ import annotations

import signal
import threading

from app.core.config import settings
from app.core.database import is_sqlite
from app.service.ingestion_task_service import ensure_ingestion_tables, process_next_ingestion_task
from app.service.legal_agent_runtime_service import ensure_legal_agent_runtime_tables, process_next_legal_agent_run


def main():
    if not is_sqlite():
        ensure_ingestion_tables()
    ensure_legal_agent_runtime_tables()
    stop_event = threading.Event()

    def _handle_signal(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_signal)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle_signal)

    wait_seconds = max(0.25, min(settings.ingestion_worker_poll_seconds, settings.agent_worker_poll_seconds))
    while not stop_event.is_set():
        handled = process_next_legal_agent_run(settings.agent_worker_name or None)
        if not is_sqlite() and settings.remote_search_enabled and settings.async_hydration_enabled:
            handled = process_next_ingestion_task(settings.ingestion_worker_name or None) or handled
        if not handled:
            stop_event.wait(wait_seconds)


if __name__ == "__main__":
    main()
