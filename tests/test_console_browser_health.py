"""Tests for browser/backend health readback in /api/diagnostics and dashboard page."""
from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import console_app, heartbeat


@pytest.fixture()
def live_server(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-console-browser")
    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def _seed_browser_heartbeat_error(conn, worker_id: str, machine_owner: str, role: str, state: str, *, error_text: str | None = None, log_text: str | None = None) -> None:
    heartbeat.beat(
        conn,
        worker_id,
        machine_owner=machine_owner,
        role=role,
        state=state,
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE worker_heartbeat SET last_error=%s, recent_log=%s WHERE worker_id=%s",
            (error_text, log_text, worker_id),
        )


def test_browser_health_classifies_heartbeat_errors(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        _seed_browser_heartbeat_error(
            conn,
            "m2-0",
            machine_owner="m2",
            role="apply",
            state="applying",
            error_text="browser service unavailable: connection refused to playwright backend",
        )
        conn.commit()
    diagnostics = console_app.diagnostics()["browser_health"]
    assert diagnostics["summary"]["workers"] >= 1
    assert diagnostics["summary"]["problem_workers"] == 1
    issue = diagnostics["issues"][0]
    assert issue["worker_id"] == "m2-0"
    assert issue["machine"] == "tarpon"
    assert issue["issue"] == "browser service unavailable"
    assert diagnostics["summary"]["by_issue"]["browser service unavailable"] == 1


def test_diagnostics_payload_includes_browser_health(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        _seed_browser_heartbeat_error(
            conn,
            "m4-0",
            machine_owner="m4",
            role="compute",
            state="idle",
            log_text="playwright disconnected while collecting page",
        )
        conn.commit()
    payload = console_app.diagnostics()
    assert "browser_health" in payload
    assert set(payload["browser_health"].keys()) == {"issues", "summary"}
    assert payload["browser_health"]["summary"]["problem_workers"] == 1
    assert payload["browser_health"]["summary"]["by_issue"]["playwright/mcp disconnected"] == 1


def test_browser_health_does_not_classify_plain_mcp_tool_calls(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        _seed_browser_heartbeat_error(
            conn,
            "m2-1",
            machine_owner="m2",
            role="apply",
            state="applying",
            log_text="Selected demographic fields.\n  >> mcp_tool_call\n  >> mcp_tool_call\nRESULT:APPLIED",
        )
        conn.commit()
    payload = console_app.diagnostics()
    assert payload["browser_health"]["summary"]["problem_workers"] == 0
    assert payload["browser_health"]["issues"] == []


def test_browser_health_keeps_stale_errors_out_of_actionable_problem_count(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        _seed_browser_heartbeat_error(
            conn, "stale-browser", machine_owner="m2", role="apply", state="idle",
            error_text="browser service unavailable",
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET last_beat=now()-interval '1 hour' WHERE worker_id='stale-browser'"
            )
        conn.commit()

    health = console_app.diagnostics()["browser_health"]
    assert health["summary"]["problem_workers"] == 0
    assert health["summary"]["stale_problem_workers"] == 1
    assert health["issues"] == []


def test_browser_health_keeps_paused_worker_log_history_out_of_current_problems(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        _seed_browser_heartbeat_error(
            conn, "paused-browser", machine_owner="m2", role="apply", state="paused",
            log_text="CAPTCHA present from the prior job",
        )
        conn.commit()

    health = console_app.diagnostics()["browser_health"]
    assert health["summary"]["problem_workers"] == 0
    assert health["summary"]["inactive_problem_workers"] == 1
    assert health["issues"] == []


def test_console_page_has_browser_health_section_and_js_renderer(live_server) -> None:
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")
    assert "Browser and backend health" in html
    assert 'id="browserHealth"' in html
    assert "function renderDiagnostics" in html
    assert "function markDiagnosticsStale" in html
    assert 'id="bProblem"' in html
    assert 'id="bProblemHint"' in html


def test_browser_health_endpoint_fills_and_html_contract(monkeypatch, fleet_db, live_server) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        _seed_browser_heartbeat_error(
            conn,
            "m2-0",
            machine_owner="m2",
            role="apply",
            state="applying",
            log_text="CAPTCHA present in page; login gate blocked",
        )
        conn.commit()
    with urllib.request.urlopen(f"{live_server}/api/diagnostics") as resp:
        body = json.loads(resp.read().decode("utf-8"))
    assert "browser_health" in body
    assert body["browser_health"]["summary"]["problem_workers"] == 1


def test_every_browser_issue_pattern_has_operator_guidance() -> None:
    missing = [
        issue
        for issue, _ in console_app._BROWSER_ISSUE_PATTERNS
        if issue not in console_app._BROWSER_ISSUE_GUIDANCE
    ]
    assert missing == []


def test_status_recommendation_surfaces_usage_limit_before_monitoring() -> None:
    rec = console_app._status_recommendation(
        gate={"paused": False, "ats_paused": False, "canary_enabled": False},
        queue_apply={"queued": 1, "approved": 1},
        apply_state={"code": "ready_to_apply"},
        workers=[{"alive": True}],
        linkedin={"halted": False, "queued": 0},
        discovery=None,
        doctor_sig=None,
        agents={"blocked_workers": 0},
        browser={
            "issues": [{
                "issue": "usage limit",
                "worker_id": "home-0",
                "machine": "home",
                "current_model": "codex",
                "age": 12,
            }]
        },
    )
    assert rec["title"] == "Resolve browser/model limit"
    assert "usage limit on home" in rec["reason"]
    assert "switch" in rec["reason"].lower() or "wait" in rec["reason"].lower()
