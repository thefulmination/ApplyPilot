from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from applypilot.apply import browser_preflight, lifecycle_fault, pgqueue
from applypilot.fleet import queue
from applypilot.fleet.worker import WorkerLoop

pytestmark = pytest.mark.usefixtures("acquisition_admitted")


def _isolate_lifecycle_faults(monkeypatch, tmp_path):
    monkeypatch.setattr(lifecycle_fault.config, "DB_PATH", tmp_path / "applypilot.db")


def test_browser_readiness_runs_all_zero_model_checks(monkeypatch):
    calls = []
    monkeypatch.setattr(
        browser_preflight,
        "_check_cdp_http",
        lambda port, timeout: calls.append(("http", port)) or (True, "cdp_http_ready"),
    )
    monkeypatch.setattr(
        browser_preflight,
        "_check_playwright_cdp",
        lambda port, timeout: calls.append(("playwright", port)) or (True, "playwright_cdp_ready"),
    )
    monkeypatch.setattr(
        browser_preflight,
        "_check_mcp_package",
        lambda timeout: calls.append(("mcp", timeout)) or (True, "mcp_package_ready"),
    )

    result = browser_preflight.check_browser_readiness(9442, timeout=2)

    assert result["ready"] is True
    assert [call[0] for call in calls] == ["http", "playwright", "mcp"]
    assert len(result["checks"]) == 3


def test_browser_readiness_stops_before_mcp_when_cdp_fails(monkeypatch):
    monkeypatch.setattr(
        browser_preflight,
        "_check_cdp_http",
        lambda port, timeout: (False, "cdp_http:ConnectionRefusedError"),
    )
    monkeypatch.setattr(
        browser_preflight,
        "_check_playwright_cdp",
        lambda *args: (_ for _ in ()).throw(AssertionError("must short-circuit")),
    )

    result = browser_preflight.check_browser_readiness(9442)

    assert result == {
        "ready": False,
        "reason": "cdp_http:ConnectionRefusedError",
        "checks": [{
            "check": "cdp_http",
            "ok": False,
            "reason": "cdp_http:ConnectionRefusedError",
        }],
    }


def test_worker_requeues_untouched_browser_preflight_failure(fleet_db, monkeypatch, tmp_path):
    _isolate_lifecycle_faults(monkeypatch, tmp_path)
    url = "https://example.com/jobs/browser-down"
    with pgqueue.connect(fleet_db) as conn:
        policy = "browser-ats-policy"
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
                "VALUES (%s,'ats','active')",
                (policy,),
            )
            cur.execute(
                "UPDATE fleet_config SET ats_policy_version=%s,ats_apply_mode='steady',"
                "paused=FALSE,ats_paused=FALSE WHERE id=1",
                (policy,),
            )
        conn.commit()
        queue.push_apply_jobs(conn, [{
            "url": url, "company": "Acme", "title": "Role",
            "application_url": url, "score": 9.0, "target_host": "example.com",
            "decision_id": "decision-browser", "policy_version": policy,
            "decision_action": "apply", "qualification_verdict": "qualified",
            "qualification_score": 9.0, "qualification_floor": 7.0,
            "preference_score": 8.0, "outcome_score": 8.0, "final_score": 9.0,
            "decision_confidence": 0.9, "decision_created_at": now,
            "decision_expires_at": now + timedelta(days=1), "input_hash": "hash-browser",
        }], approved_batch="batch-browser")

    loop = WorkerLoop(
        lambda: pgqueue.connect(fleet_db),
        "w-browser",
        home_ip="1.2.3.4",
        role="apply",
        apply_fn=lambda _job: {
            "run_status": "failed:browser_preflight:cdp_http:ConnectionRefusedError",
            "est_cost_usd": 0.0,
            "application_tool_calls": 0,
            "infrastructure_preflight_failure": True,
        },
        sw_version="0.3.0",
    )

    result = loop.run_once()
    assert result["action"] == "infrastructure_parked"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, attempts, lease_owner, est_cost_usd, apply_error, "
            "apply_status, infrastructure_failure_count "
            "FROM apply_queue WHERE url=%s",
            (url,),
        )
        row = cur.fetchone()
        cur.execute(
            "SELECT count(*) AS n FROM apply_result_events "
            "WHERE url=%s AND status <> 'leased'",
            (url,),
        )
        events = cur.fetchone()["n"]
        cur.execute(
            "SELECT state, closed_at FROM fleet_worker_lease_ledger "
            "WHERE lane='ats' AND url=%s ORDER BY leased_at DESC LIMIT 1",
            (url,),
        )
        ledger = cur.fetchone()
        cur.execute(
            "SELECT scope_key, count_24h FROM rate_governor "
            "WHERE scope_key IN ('global','home_ip:1.2.3.4','host:example.com')",
        )
        governor_counts = {item["scope_key"]: item["count_24h"] for item in cur.fetchall()}
    assert row["status"] == "failed"
    assert row["attempts"] == 0
    assert row["lease_owner"] is None
    assert float(row["est_cost_usd"] or 0) == 0
    assert row["apply_error"].startswith("failed:browser_preflight:")
    assert row["apply_status"] == "infrastructure_pending"
    assert row["infrastructure_failure_count"] == 1
    assert events == 0
    assert ledger["state"] == "terminal"
    assert ledger["closed_at"] is not None
    assert governor_counts == {
        "global": 0,
        "home_ip:1.2.3.4": 0,
        "host:example.com": 0,
    }


def test_apply_fn_restarts_browser_once_before_reporting_failure(monkeypatch, tmp_path):
    _isolate_lifecycle_faults(monkeypatch, tmp_path)
    from applypilot.apply import chrome, launcher
    from applypilot.fleet import apply_worker_main

    launched = []
    cleaned = []
    run_calls = []
    readiness = iter((
        {"ready": False, "reason": "cdp_http:URLError", "checks": []},
        {"ready": False, "reason": "playwright_cdp:Error", "checks": []},
    ))
    monkeypatch.setattr(
        browser_preflight,
        "check_browser_readiness",
        lambda port: next(readiness),
    )
    monkeypatch.setattr(browser_preflight, "clear_readiness_cache", lambda: None)
    monkeypatch.setattr(
        chrome,
        "launch_chrome",
        lambda worker_id, **kwargs: launched.append(worker_id) or object(),
    )
    monkeypatch.setattr(
        chrome,
        "cleanup_worker",
        lambda worker_id, proc: cleaned.append(worker_id) or True,
    )
    monkeypatch.setattr(
        launcher,
        "run_job",
        lambda *args, **kwargs: run_calls.append(1) or ("applied", 1),
    )

    result = apply_worker_main.make_apply_fn("sonnet", "codex", slot=6)(
        {"url": "https://example.com/jobs/restart"}
    )

    assert launched == [6, 6]
    assert cleaned == [6, 6]
    assert run_calls == []
    assert result["infrastructure_restart_attempted"] is True
    assert result["run_status"] == "failed:browser_preflight:playwright_cdp:Error"
