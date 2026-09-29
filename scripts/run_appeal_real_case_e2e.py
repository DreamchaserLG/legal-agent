from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

import requests
import websocket


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from test_appeal_real_case_hryniak import REAL_AUTHORITIES, real_case_payload


TMP_DIR = ROOT_DIR / "tmp"
ASSET_DIR = ROOT_DIR / "docs" / "assets"
RESULT_PATH = TMP_DIR / "appeal_real_case_hryniak_e2e.json"
DEFAULT_EDGE_PATHS = (
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def wait_for(
    label: str,
    predicate: Callable[[], Any],
    *,
    timeout: float,
    process: subprocess.Popen | None = None,
    interval: float = 0.25,
) -> Any:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process is not None and process.poll() is not None:
            raise RuntimeError(f"{label}: process exited with code {process.returncode}")
        try:
            value = predicate()
            if value:
                return value
        except Exception as exc:  # The final timeout reports the latest diagnostic.
            last_error = exc
        time.sleep(interval)
    detail = f"; last_error={last_error}" if last_error else ""
    raise TimeoutError(f"{label} timed out after {timeout:.1f}s{detail}")


def stop_process(process: subprocess.Popen | None, label: str) -> None:
    if process is None or process.poll() is not None:
        return
    log(f"stopping {label} (pid={process.pid})")
    process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def api_json(
    session: requests.Session,
    method: str,
    url: str,
    *,
    timeout: float,
    **kwargs: Any,
) -> dict[str, Any]:
    response = session.request(method, url, timeout=timeout, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:1000]}
    if not response.ok:
        raise RuntimeError(f"{method} {url} returned {response.status_code}: {body}")
    if not isinstance(body, dict):
        raise RuntimeError(f"{method} {url} returned non-object JSON")
    return body


def inject_authorities(database_path: Path, run_id: str) -> None:
    with sqlite3.connect(database_path, timeout=10) as connection:
        row = connection.execute(
            "SELECT state_json FROM appeal_runs WHERE id = ?", (run_id,)
        ).fetchone()
        if not row:
            raise RuntimeError(f"appeal run {run_id} was not persisted")
        state = json.loads(row[0])
        state["authorities"] = [dict(item) for item in REAL_AUTHORITIES]
        connection.execute(
            "UPDATE appeal_runs SET state_json = ? WHERE id = ?",
            (json.dumps(state, ensure_ascii=False), run_id),
        )
        connection.commit()


def stage_input(stage: str) -> dict[str, Any]:
    if stage == "appellant_main":
        return {
            "content": (
                "For Hryniak, the reversible error is legal and procedural. The Court of Appeal accepted that the "
                "full appreciation test would require a trial for records like this, yet it upheld final civil fraud "
                "liability on a paper record. Rule 20.04 and Hryniak v. Mauldin require a fair and just determination. "
                "The affidavits from 18 witnesses, three weeks of cross-examinations and 28-volume record support remitting the action "
                "for trial."
            ),
            "issue_ids": ["I-001", "I-002"],
            "record_ids": ["R-002", "R-003", "R-006", "R-007"],
            "authority_ids": ["A-001", "A-002", "A-003", "A-004"],
        }
    if stage == "appellant_answer":
        return {
            "content": (
                "Correctness applies to the legal test for summary judgment. A factual finding receives deference only "
                "after the court confirms that a trial is unnecessary. The Court of Appeal itself identified a record "
                "that would normally require trial, so the legal threshold was not met."
            ),
            "issue_ids": ["I-001", "I-002"],
            "record_ids": ["R-002", "R-006", "R-007"],
            "authority_ids": ["A-001", "A-002", "A-003", "A-004"],
        }
    if stage == "appellant_reply":
        return {
            "content": (
                "Replying only to the respondent: reliance on the fraud finding does not answer whether Rule 20 allowed "
                "final liability on this credibility-heavy record. The appeal should be allowed and remitted for trial."
            ),
            "issue_ids": ["I-001"],
            "record_ids": ["R-002", "R-006", "R-007", "R-008"],
            "authority_ids": ["A-001", "A-002", "A-003"],
        }
    return {}


class CdpClient:
    def __init__(self, websocket_url: str):
        self.socket = websocket.create_connection(
            websocket_url,
            timeout=15,
            suppress_origin=True,
            http_proxy_host=None,
            http_proxy_port=None,
        )
        self.sequence = 0

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sequence += 1
        request_id = self.sequence
        self.socket.send(
            json.dumps(
                {"id": request_id, "method": method, "params": params or {}},
                ensure_ascii=False,
            )
        )
        while True:
            message = json.loads(self.socket.recv())
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"{method}: {message['error']}")
            return message.get("result", {})

    def evaluate(self, expression: str) -> Any:
        result = self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(f"browser evaluation failed: {result['exceptionDetails']}")
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, *, timeout: float, label: str) -> Any:
        return wait_for(label, lambda: self.evaluate(expression), timeout=timeout)

    def screenshot(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        result = self.call(
            "Page.captureScreenshot",
            {"format": "png", "captureBeyondViewport": False, "fromSurface": True},
        )
        path.write_bytes(base64.b64decode(result["data"]))

    def close(self) -> None:
        try:
            self.call("Browser.close")
        except Exception:
            pass
        try:
            self.socket.close()
        except Exception:
            pass


def find_edge(explicit_path: str) -> Path:
    candidates = [Path(explicit_path)] if explicit_path else list(DEFAULT_EDGE_PATHS)
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("Microsoft Edge was not found; pass --edge-path")


def launch_browser(
    *,
    edge_path: Path,
    base_url: str,
    run_id: str,
    session: requests.Session,
    timeout: float,
    profile_path: Path,
    browser_log,
) -> tuple[subprocess.Popen, CdpClient, str]:
    debug_port = free_port()
    browser = subprocess.Popen(
        [
            str(edge_path),
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--remote-allow-origins=*",
            f"--remote-debugging-port={debug_port}",
            f"--user-data-dir={profile_path}",
            "about:blank",
        ],
        cwd=ROOT_DIR,
        stdout=browser_log,
        stderr=subprocess.STDOUT,
    )
    debug_url = f"http://127.0.0.1:{debug_port}"
    debug_session = requests.Session()
    debug_session.trust_env = False
    wait_for(
        "Edge DevTools endpoint",
        lambda: debug_session.get(f"{debug_url}/json/version", timeout=2).ok,
        timeout=timeout,
        process=browser,
    )
    target_url = f"{base_url}/appeal-simulation?run_id={quote(run_id)}"
    target = debug_session.put(f"{debug_url}/json/new?about:blank", timeout=5).json()
    client = CdpClient(target["webSocketDebuggerUrl"])
    client.call("Page.enable")
    client.call("Runtime.enable")
    client.call("Network.enable")
    client.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1416, "height": 1108, "deviceScaleFactor": 1, "mobile": False},
    )
    for cookie in session.cookies:
        result = client.call(
            "Network.setCookie",
            {"name": cookie.name, "value": cookie.value, "url": base_url},
        )
        if not result.get("success"):
            raise RuntimeError(f"failed to set browser cookie {cookie.name}")
    client.call("Page.navigate", {"url": target_url})
    client.wait_for(
        "document.readyState === 'complete' && location.pathname === '/appeal-simulation'",
        timeout=timeout,
        label="appeal page load",
    )
    return browser, client, target_url


def browser_initial_state(client: CdpClient, *, timeout: float) -> dict[str, Any]:
    client.wait_for(
        "!document.querySelector('#appeal-workbench').hidden && document.querySelectorAll('#appeal-authority-list article').length === 5",
        timeout=timeout,
        label="persisted appeal run render",
    )
    client.evaluate("document.querySelector('#appeal-workbench').scrollIntoView({block: 'start'}); true")
    time.sleep(0.4)
    client.screenshot(ASSET_DIR / "appeal-real-case-hryniak-initial.png")
    return client.evaluate(
        """(() => ({
          stage: document.querySelector('#appeal-stage-title').textContent,
          authorities: document.querySelectorAll('#appeal-authority-list article').length,
          records: document.querySelectorAll('#appeal-record-list article').length,
          horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1
        }))()"""
    )


def browser_final_state(client: CdpClient, target_url: str, *, timeout: float) -> dict[str, Any]:
    client.call("Page.navigate", {"url": target_url})
    client.wait_for(
        "document.readyState === 'complete' && !document.querySelector('#appeal-final-report').hidden",
        timeout=timeout,
        label="completed appeal report render",
    )
    client.evaluate("document.querySelector('#appeal-workbench').scrollIntoView({block: 'start'}); true")
    time.sleep(0.4)
    client.screenshot(ASSET_DIR / "appeal-real-case-hryniak-final.png")
    client.evaluate("document.querySelector('#appeal-final-report').scrollIntoView({block: 'start'}); true")
    time.sleep(0.4)
    client.screenshot(ASSET_DIR / "appeal-real-case-hryniak-report.png")
    desktop = client.evaluate(
        """(() => ({
          viewport: [innerWidth, innerHeight],
          horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
          messages: document.querySelectorAll('#appeal-transcript article').length,
          completedStages: document.querySelectorAll('.appeal-stage-node.completed').length,
          scores: document.querySelectorAll('#appeal-score-list article').length,
          reportVisible: !document.querySelector('#appeal-final-report').hidden,
          status: document.querySelector('#appeal-run-status').textContent
        }))()"""
    )
    client.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
    )
    time.sleep(0.8)
    client.evaluate(
        """(() => {
          const report = document.querySelector('#appeal-final-report');
          const targetTop = 255;
          const top = window.scrollY + report.getBoundingClientRect().top - targetTop;
          window.scrollTo({top, behavior: 'instant'});
          return true;
        })()"""
    )
    client.wait_for(
        "Math.abs(document.querySelector('#appeal-final-report').getBoundingClientRect().top - 255) < 4",
        timeout=5,
        label="mobile report scroll",
    )
    time.sleep(0.3)
    client.screenshot(ASSET_DIR / "appeal-real-case-hryniak-mobile.png")
    mobile = client.evaluate(
        """(() => ({
          viewport: [innerWidth, innerHeight],
          horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
          reportVisible: !document.querySelector('#appeal-final-report').hidden,
          stageStripOverflow: getComputedStyle(document.querySelector('#appeal-stage-strip')).overflowX
        }))()"""
    )
    return {"desktop": desktop, "mobile": mobile}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bounded Hryniak appeal browser evaluation.")
    parser.add_argument("--edge-path", default="")
    parser.add_argument("--startup-timeout", type=float, default=35.0)
    parser.add_argument("--request-timeout", type=float, default=20.0)
    parser.add_argument("--browser-timeout", type=float, default=35.0)
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args()

    TMP_DIR.mkdir(exist_ok=True)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    server_port = free_port()
    database_path = TMP_DIR / f"appeal-real-case-{server_port}.db"
    server_log_path = TMP_DIR / f"appeal-real-case-{server_port}-server.log"
    browser_log_path = TMP_DIR / f"appeal-real-case-{server_port}-edge.log"
    server: subprocess.Popen | None = None
    browser: subprocess.Popen | None = None
    client: CdpClient | None = None
    profile_context: tempfile.TemporaryDirectory | None = None
    result: dict[str, Any] = {"status": "failed", "failed_stage": "startup"}

    with server_log_path.open("w", encoding="utf-8") as server_log, browser_log_path.open(
        "w", encoding="utf-8"
    ) as browser_log:
        try:
            env = os.environ.copy()
            env.update(
                {
                    "DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
                    "APP_PORT": str(server_port),
                    "SESSION_SECRET": "appeal-real-case-e2e-secret",
                    "LLM_PROVIDER": "none",
                    "CANLII_REALTIME_SEARCH_ENABLED": "false",
                    "ARCHIVE_BOOTSTRAP_ENABLED": "false",
                    "STARTUP_CANADA_SYNC_MODE": "off",
                    "DEMO_HISTORY_SEED_ENABLED": "false",
                    "NO_PROXY": "127.0.0.1,localhost",
                }
            )
            log(f"starting isolated server on port {server_port}")
            server = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "uvicorn",
                    "app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(server_port),
                ],
                cwd=ROOT_DIR,
                env=env,
                stdout=server_log,
                stderr=subprocess.STDOUT,
            )
            base_url = f"http://127.0.0.1:{server_port}"
            health_session = requests.Session()
            health_session.trust_env = False
            wait_for(
                "Uvicorn health check",
                lambda: health_session.get(f"{base_url}/health", timeout=2).json().get("status") == "ok",
                timeout=args.startup_timeout,
                process=server,
            )

            result["failed_stage"] = "authentication"
            session = requests.Session()
            session.trust_env = False
            username = f"appeal_case_{int(time.time())}"
            api_json(
                session,
                "POST",
                f"{base_url}/api/auth/register",
                timeout=args.request_timeout,
                json={
                    "username": username,
                    "email": f"{username}@example.test",
                    "password": "AppealReal123",
                    "confirmPassword": "AppealReal123",
                },
            )

            result["failed_stage"] = "run_creation"
            payload_model = real_case_payload()
            payload = (
                payload_model.model_dump()
                if hasattr(payload_model, "model_dump")
                else payload_model.dict()
            )
            run = api_json(
                session,
                "POST",
                f"{base_url}/api/appeal/runs",
                timeout=args.request_timeout,
                json=payload,
            )
            run_id = str(run["id"])
            inject_authorities(database_path, run_id)
            run = api_json(
                session,
                "GET",
                f"{base_url}/api/appeal/runs/{run_id}",
                timeout=args.request_timeout,
            )
            if len(run.get("authorities", [])) != len(REAL_AUTHORITIES):
                raise RuntimeError("real authority fixture was not visible through the API")

            browser_metrics: dict[str, Any] = {}
            target_url = ""
            if not args.skip_browser:
                result["failed_stage"] = "browser_initial_render"
                edge_path = find_edge(args.edge_path)
                profile_context = tempfile.TemporaryDirectory(
                    prefix="appeal-edge-", dir=TMP_DIR
                )
                browser, client, target_url = launch_browser(
                    edge_path=edge_path,
                    base_url=base_url,
                    run_id=run_id,
                    session=session,
                    timeout=args.browser_timeout,
                    profile_path=Path(profile_context.name),
                    browser_log=browser_log,
                )
                browser_metrics["initial"] = browser_initial_state(
                    client, timeout=args.browser_timeout
                )

            result["failed_stage"] = "workflow"
            stage_history: list[str] = []
            for _ in range(12):
                if run.get("status") == "completed":
                    break
                stage = str(run.get("active_stage", ""))
                if not stage:
                    raise RuntimeError("workflow response did not include active_stage")
                stage_history.append(stage)
                log(f"advancing stage: {stage}")
                run = api_json(
                    session,
                    "POST",
                    f"{base_url}/api/appeal/runs/{run_id}/advance",
                    timeout=args.request_timeout,
                    json=stage_input(stage),
                )
            else:
                raise RuntimeError("workflow exceeded the 12-step safety limit")
            if run.get("status") != "completed":
                raise RuntimeError(f"workflow stopped with status {run.get('status')}")

            if client is not None:
                result["failed_stage"] = "browser_final_render"
                browser_metrics.update(
                    browser_final_state(client, target_url, timeout=args.browser_timeout)
                )

            result = {
                "status": "passed",
                "run_id": run_id,
                "stage_history": stage_history,
                "workflow": run,
                "browser": browser_metrics,
                "timeouts_seconds": {
                    "startup": args.startup_timeout,
                    "request": args.request_timeout,
                    "browser": args.browser_timeout,
                },
                "artifacts": {
                    "server_log": str(server_log_path.relative_to(ROOT_DIR)),
                    "browser_log": str(browser_log_path.relative_to(ROOT_DIR)),
                    "screenshots": [
                        "docs/assets/appeal-real-case-hryniak-initial.png",
                        "docs/assets/appeal-real-case-hryniak-final.png",
                        "docs/assets/appeal-real-case-hryniak-report.png",
                        "docs/assets/appeal-real-case-hryniak-mobile.png",
                    ]
                    if not args.skip_browser
                    else [],
                },
            }
            log("real-case appeal E2E passed")
            return 0
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            log(f"FAILED at {result.get('failed_stage')}: {result['error']}")
            return 1
        finally:
            RESULT_PATH.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if client is not None:
                client.close()
            stop_process(browser, "Edge")
            stop_process(server, "Uvicorn")
            if profile_context is not None:
                try:
                    profile_context.cleanup()
                except OSError:
                    log(f"Edge profile cleanup deferred: {profile_context.name}")


if __name__ == "__main__":
    raise SystemExit(main())
