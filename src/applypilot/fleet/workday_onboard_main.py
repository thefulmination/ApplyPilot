"""Bootstrap isolated Workday tenant accounts for supervised canaries."""
from __future__ import annotations

import argparse
import json
import sqlite3
import time

import httpx

from applypilot import config, tenants
from applypilot.apply import tenant_sessions
from applypilot.apply import credential_vault
from applypilot.apply.chrome import cleanup_worker, launch_chrome
from applypilot.apply.workday_onboarding import bootstrap_workday_account
from applypilot.database import get_connection
from applypilot.fleet.rollout import fresh_workday_candidates
from applypilot.fleet.workday_rollout_main import _enter_application


def _wait_cdp(port: int, timeout: float = 30) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"http://localhost:{port}/json/version", timeout=1).status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("cdp_start_timeout")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--headless", action="store_true")
    parser.add_argument("--base-port", type=int, default=9420)
    args = parser.parse_args()
    limit = max(1, min(args.limit, 5))

    profile = config.load_profile()
    personal = profile.get("personal") or {}
    email, profile_password = personal.get("email"), personal.get("password")
    if not email:
        parser.error("profile personal.email is required")

    conn = get_connection()
    conn.row_factory = sqlite3.Row
    candidates = fresh_workday_candidates(conn, limit=limit * 3)
    unique = []
    seen = set()
    for job in candidates:
        if job["target_host"] in seen:
            continue
        seen.add(job["target_host"])
        unique.append(job)
        if len(unique) == limit:
            break

    output = []
    for index, job in enumerate(unique):
        host = job["target_host"]
        password = profile_password or credential_vault.get_or_create(host)
        tenants.set_tenant(conn, host, "supervised")
        row = tenants.set_session_state(conn, host, "supervised", reason="onboarding")
        session = tenant_sessions.select_session(host, profile_id=row["profile_id"])
        worker_id, port, process = 120 + index, args.base_port + index, None
        try:
            process = launch_chrome(
                worker_id, port=port, headless=args.headless, kill_existing=False,
                profile_dir=session["profile_dir"],
            )
            _wait_cdp(port)
            from playwright.sync_api import sync_playwright
            with sync_playwright() as playwright:
                browser = playwright.chromium.connect_over_cdp(f"http://localhost:{port}")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.new_page()
                try:
                    target = job.get("application_url") or job["url"]
                    page.goto(target, wait_until="domcontentloaded", timeout=30000)
                    if not _enter_application(page):
                        result_status, result_reason = "parked", "apply_control_missing"
                    else:
                        result = bootstrap_workday_account(
                            page, email=email, password=password, host=host,
                        )
                        result_status, result_reason = result.status, result.reason
                finally:
                    page.close()
            if result_status == "ready":
                tenants.set_session_state(
                    conn, host, "ready", ttl_hours=24, reason=result_reason,
                )
        except Exception as exc:
            result_status, result_reason = "parked", f"onboarding_error:{type(exc).__name__}"
        finally:
            if process is not None:
                cleanup_worker(worker_id, process)
        output.append({"host": host, "status": result_status, "reason": result_reason})
    print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
