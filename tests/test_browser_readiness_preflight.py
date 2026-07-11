from __future__ import annotations

from applypilot.apply import browser_preflight, pgqueue
from applypilot.fleet.worker import WorkerLoop


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


def test_worker_requeues_untouched_browser_preflight_failure(fleet_db):
    url = "https://example.com/jobs/browser-down"
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, application_url, score, status, lane, approved_batch, target_host) "
                "VALUES (%s,%s,9.0,'queued','ats','batch-browser','example.com')",
                (url, url),
            )
        conn.commit()

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
        cur.execute("SELECT count(*) AS n FROM apply_result_events WHERE url=%s", (url,))
        events = cur.fetchone()["n"]
    assert row["status"] == "failed"
    assert row["attempts"] == 0
    assert row["lease_owner"] is None
    assert float(row["est_cost_usd"] or 0) == 0
    assert row["apply_error"].startswith("failed:browser_preflight:")
    assert row["apply_status"] == "infrastructure_pending"
    assert row["infrastructure_failure_count"] == 1
    assert events == 0


def test_apply_fn_restarts_browser_once_before_reporting_failure(monkeypatch):
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
