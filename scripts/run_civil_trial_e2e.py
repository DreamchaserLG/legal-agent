from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from run_appeal_real_case_e2e import (
    CdpClient,
    api_json,
    find_edge,
    free_port,
    stop_process,
    wait_for,
)
from test_civil_trial_workflow import concrete_trial_payload


TMP_DIR = ROOT_DIR / "tmp"
ASSET_DIR = ROOT_DIR / "docs" / "assets"
RESULT_PATH = TMP_DIR / "civil_trial_e2e_result.json"


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def launch_browser(
    *, edge_path: Path, base_url: str, run_id: str, session: requests.Session,
    timeout: float, profile_path: Path, browser_log,
) -> tuple[subprocess.Popen, CdpClient, str]:
    debug_port = free_port()
    browser = subprocess.Popen(
        [
            str(edge_path), "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--remote-allow-origins=*",
            f"--remote-debugging-port={debug_port}", f"--user-data-dir={profile_path}", "about:blank",
        ],
        cwd=ROOT_DIR, stdout=browser_log, stderr=subprocess.STDOUT,
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
    target_url = f"{base_url}/hearing?run_id={quote(run_id)}"
    target = debug_session.put(f"{debug_url}/json/new?about:blank", timeout=5).json()
    client = CdpClient(target["webSocketDebuggerUrl"])
    client.call("Page.enable")
    client.call("Runtime.enable")
    client.call("Network.enable")
    client.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1440, "height": 1100, "deviceScaleFactor": 1, "mobile": False},
    )
    for cookie in session.cookies:
        result = client.call("Network.setCookie", {"name": cookie.name, "value": cookie.value, "url": base_url})
        if not result.get("success"):
            raise RuntimeError(f"failed to set browser cookie {cookie.name}")
    client.call("Page.navigate", {"url": target_url})
    client.wait_for(
        "document.readyState === 'complete' && location.pathname === '/hearing' && !document.querySelector('#trial-workbench').hidden",
        timeout=timeout,
        label="civil trial page load",
    )
    return browser, client, target_url


def navigate_and_wait(client: CdpClient, target_url: str, expression: str, *, timeout: float, label: str) -> None:
    client.call("Page.navigate", {"url": target_url})
    client.wait_for(f"document.readyState === 'complete' && ({expression})", timeout=timeout, label=label)


def screenshot_initial(client: CdpClient) -> dict[str, Any]:
    client.evaluate("document.querySelector('#trial-workbench').scrollIntoView({block:'start'}); true")
    time.sleep(0.4)
    client.screenshot(ASSET_DIR / "civil-trial-northlake-initial.png")
    return client.evaluate(
        """(() => ({
          title: document.querySelector('#trial-phase-title').textContent,
          witnesses: document.querySelectorAll('#trial-witnesses article').length,
          exhibits: document.querySelectorAll('#trial-exhibits article').length,
          horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1
        }))()"""
    )


def screenshot_evidence(client: CdpClient, target_url: str, *, timeout: float) -> dict[str, Any]:
    navigate_and_wait(
        client, target_url,
        "document.querySelectorAll('#trial-transcript article').length >= 12 && document.querySelectorAll('#trial-objections article').length >= 1",
        timeout=timeout, label="evidence and objection render",
    )
    client.evaluate("document.querySelector('#trial-transcript').scrollIntoView({block:'start'}); true")
    client.evaluate("document.querySelector('#trial-transcript').scrollTop = document.querySelector('#trial-transcript').scrollHeight; true")
    time.sleep(0.4)
    client.screenshot(ASSET_DIR / "civil-trial-northlake-evidence.png")
    return client.evaluate(
        """(() => ({
          events: document.querySelectorAll('#trial-transcript article').length,
          objections: document.querySelectorAll('#trial-objections article').length,
          status: document.querySelector('#trial-status').textContent
        }))()"""
    )


def screenshot_final(client: CdpClient, target_url: str, *, timeout: float) -> dict[str, Any]:
    navigate_and_wait(client, target_url, "!document.querySelector('#trial-final-report').hidden", timeout=timeout, label="final judgment render")
    client.evaluate("document.querySelector('#trial-workbench').scrollIntoView({block:'start'}); true")
    time.sleep(0.4)
    client.screenshot(ASSET_DIR / "civil-trial-northlake-final.png")
    client.evaluate("""(() => { const node = document.querySelector('#trial-final-report'); window.scrollTo({top: window.scrollY + node.getBoundingClientRect().top - 92, behavior:'instant'}); return true; })()""")
    time.sleep(0.4)
    client.screenshot(ASSET_DIR / "civil-trial-northlake-judgment.png")
    desktop = client.evaluate(
        """(() => ({
          viewport: [innerWidth, innerHeight],
          horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
          events: document.querySelectorAll('#trial-transcript article').length,
          completedPhases: document.querySelectorAll('.trial-phase-node.completed').length,
          findings: document.querySelectorAll('.trial-finding-list article').length,
          scores: document.querySelectorAll('.trial-score-grid > article').length,
          actionFormHidden: getComputedStyle(document.querySelector('#trial-action-form')).display === 'none',
          status: document.querySelector('#trial-status').textContent
        }))()"""
    )
    client.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
    )
    time.sleep(0.8)
    client.evaluate("""(() => { const node = document.querySelector('#trial-final-report'); window.scrollTo({top: window.scrollY + node.getBoundingClientRect().top - 250, behavior:'instant'}); return true; })()""")
    time.sleep(0.4)
    client.screenshot(ASSET_DIR / "civil-trial-northlake-mobile.png")
    mobile = client.evaluate(
        """(() => ({
          viewport: [innerWidth, innerHeight],
          horizontalOverflow: document.documentElement.scrollWidth > innerWidth + 1,
          reportVisible: !document.querySelector('#trial-final-report').hidden,
          phaseStripOverflow: getComputedStyle(document.querySelector('#trial-phase-strip')).overflowX
        }))()"""
    )
    return {"desktop": desktop, "mobile": mobile}


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the bounded Ontario civil trial browser evaluation.")
    parser.add_argument("--edge-path", default="")
    parser.add_argument("--startup-timeout", type=float, default=35.0)
    parser.add_argument("--request-timeout", type=float, default=20.0)
    parser.add_argument("--browser-timeout", type=float, default=35.0)
    parser.add_argument("--skip-browser", action="store_true")
    args = parser.parse_args()

    TMP_DIR.mkdir(exist_ok=True)
    ASSET_DIR.mkdir(parents=True, exist_ok=True)
    server_port = free_port()
    database_path = TMP_DIR / f"civil-trial-e2e-{server_port}.db"
    server_log_path = TMP_DIR / f"civil-trial-e2e-{server_port}-server.log"
    browser_log_path = TMP_DIR / f"civil-trial-e2e-{server_port}-edge.log"
    server: subprocess.Popen | None = None
    browser: subprocess.Popen | None = None
    client: CdpClient | None = None
    profile_context: tempfile.TemporaryDirectory | None = None
    result: dict[str, Any] = {"status": "failed", "failed_stage": "startup"}

    with server_log_path.open("w", encoding="utf-8") as server_log, browser_log_path.open("w", encoding="utf-8") as browser_log:
        try:
            env = os.environ.copy()
            env.update({
                "DATABASE_URL": f"sqlite:///{database_path.as_posix()}",
                "APP_PORT": str(server_port), "SESSION_SECRET": "civil-trial-e2e-secret",
                "LLM_PROVIDER": "none", "CANLII_REALTIME_SEARCH_ENABLED": "false",
                "ARCHIVE_BOOTSTRAP_ENABLED": "false", "STARTUP_CANADA_SYNC_MODE": "off",
                "DEMO_HISTORY_SEED_ENABLED": "false", "NO_PROXY": "127.0.0.1,localhost",
            })
            log(f"starting isolated server on port {server_port}")
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(server_port)],
                cwd=ROOT_DIR, env=env, stdout=server_log, stderr=subprocess.STDOUT,
            )
            base_url = f"http://127.0.0.1:{server_port}"
            health_session = requests.Session()
            health_session.trust_env = False
            wait_for(
                "Uvicorn health check", lambda: health_session.get(f"{base_url}/health", timeout=2).json().get("status") == "ok",
                timeout=args.startup_timeout, process=server,
            )

            result["failed_stage"] = "authentication"
            session = requests.Session()
            session.trust_env = False
            username = f"civil_trial_{int(time.time())}"
            api_json(
                session, "POST", f"{base_url}/api/auth/register", timeout=args.request_timeout,
                json={"username": username, "email": f"{username}@example.test", "password": "CivilTrial123", "confirmPassword": "CivilTrial123"},
            )

            result["failed_stage"] = "run_creation"
            run = api_json(
                session, "POST", f"{base_url}/api/civil-trial/runs", timeout=args.request_timeout,
                json=concrete_trial_payload().model_dump(),
            )
            run_id = str(run["id"])
            browser_metrics: dict[str, Any] = {}
            target_url = ""
            if not args.skip_browser:
                result["failed_stage"] = "browser_initial_render"
                profile_context = tempfile.TemporaryDirectory(prefix="civil-trial-edge-", dir=TMP_DIR)
                browser, client, target_url = launch_browser(
                    edge_path=find_edge(args.edge_path), base_url=base_url, run_id=run_id, session=session,
                    timeout=args.browser_timeout, profile_path=Path(profile_context.name), browser_log=browser_log,
                )
                browser_metrics["initial"] = screenshot_initial(client)

            result["failed_stage"] = "workflow"
            captured_evidence = False
            safety_count = 0
            while run.get("status") != "completed":
                safety_count += 1
                if safety_count > 80:
                    raise RuntimeError("civil trial exceeded the 80-action safety limit")
                if run.get("status") == "adjourned":
                    run = api_json(
                        session, "POST", f"{base_url}/api/civil-trial/runs/{run_id}/actions",
                        timeout=args.request_timeout, json={"action": "resume"},
                    )
                else:
                    run = api_json(
                        session, "POST", f"{base_url}/api/civil-trial/runs/{run_id}/actions",
                        timeout=args.request_timeout, json={"action": "advance", "content": ""},
                    )
                if client is not None and not captured_evidence and len(run.get("objections", [])) >= 1:
                    browser_metrics["evidence"] = screenshot_evidence(client, target_url, timeout=args.browser_timeout)
                    captured_evidence = True

            if run.get("status") != "completed":
                raise RuntimeError(f"workflow stopped with status {run.get('status')}")
            if client is not None:
                result["failed_stage"] = "browser_final_render"
                browser_metrics.update(screenshot_final(client, target_url, timeout=args.browser_timeout))

            result = {
                "status": "passed", "run_id": run_id, "actions": safety_count,
                "workflow": run, "browser": browser_metrics,
                "artifacts": {
                    "server_log": str(server_log_path.relative_to(ROOT_DIR)),
                    "browser_log": str(browser_log_path.relative_to(ROOT_DIR)),
                    "screenshots": [
                        "docs/assets/civil-trial-northlake-initial.png",
                        "docs/assets/civil-trial-northlake-evidence.png",
                        "docs/assets/civil-trial-northlake-final.png",
                        "docs/assets/civil-trial-northlake-judgment.png",
                        "docs/assets/civil-trial-northlake-mobile.png",
                    ] if not args.skip_browser else [],
                },
            }
            log("civil trial E2E passed")
            return 0
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
            log(f"FAILED at {result.get('failed_stage')}: {result['error']}")
            return 1
        finally:
            RESULT_PATH.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
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
