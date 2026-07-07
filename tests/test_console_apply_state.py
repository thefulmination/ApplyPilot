"""Tests for apply-readiness state surfaced by /api/status."""
from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import console_app, queue


def test_apply_state_ready_when_leaseable_jobs_exist(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        queue.push_apply_jobs(
            conn,
            [{
                "url": "https://boards.greenhouse.io/acme/jobs/1",
                "company": "Acme",
                "title": "Engineer",
                "application_url": "https://boards.greenhouse.io/acme/jobs/1/apply",
                "score": 9.0,
                "target_host": "boards.greenhouse.io",
            }],
            approved_batch="batch-1",
        )
        conn.commit()

    payload = console_app.build_status()
    state = payload["apply_state"]
    assert state["code"] == "ready_to_apply"
    assert state["severity"] == "ok"
    assert payload["queue"]["apply"]["approved"] == 1
    assert payload["queue"]["apply"]["base_leasable"] == 1
    assert payload["gate"]["leasable"] == 1


def test_apply_state_reports_dedup_blocked_when_all_approved_rows_blocked(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        queue.push_apply_jobs(
            conn,
            [{
                "url": "https://boards.greenhouse.io/acme/jobs/2",
                "company": "Acme",
                "title": "Analyst",
                "application_url": "https://boards.greenhouse.io/acme/jobs/2/apply",
                "score": 8.8,
                "target_host": "boards.greenhouse.io",
            }],
            approved_batch="batch-2",
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) "
                "VALUES ('acme::analyst', 'Acme', 'https://already/applied')"
            )
            cur.execute(
                "UPDATE apply_queue SET dedup_key='acme::analyst' WHERE url='https://boards.greenhouse.io/acme/jobs/2'"
            )
            cur.execute(
                "UPDATE fleet_config SET ats_apply_mode='canary', canary_enabled=TRUE, canary_remaining=1 WHERE id=1"
            )
        conn.commit()

    payload = console_app.build_status()
    state = payload["apply_state"]
    assert state["code"] == "ready_jobs_not_leaseable"
    assert state["severity"] == "warn"
    assert payload["queue"]["apply"]["approved"] == 1
    assert payload["queue"]["apply"]["dedup_blocked"] == 1
    assert payload["gate"]["leasable"] == 0


def test_apply_state_reflects_paused_and_ats_paused(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config "
                "SET paused=TRUE, ats_paused=FALSE, canary_enabled=FALSE, canary_remaining=NULL, "
                "spend_cap_usd=0 WHERE id=1"
            )
            cur.execute(
                "INSERT INTO apply_queue (url, company, title, application_url, score, apply_domain, target_host, lane, status, approved_batch, est_cost_usd) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, 'ats', 'queued', 'batch-1', 2.0) "
                "ON CONFLICT (url) DO UPDATE SET status=EXCLUDED.status, lane='ats', approved_batch=EXCLUDED.approved_batch",
                (
                    "https://boards.greenhouse.io/acme/jobs/3",
                    "Acme",
                    "Designer",
                    "https://boards.greenhouse.io/acme/jobs/3/apply",
                    7.5,
                    "boards.greenhouse.io",
                    "boards.greenhouse.io",
                ),
            )
        conn.commit()

    payload = console_app.build_status()
    state = payload["apply_state"]
    assert state["code"] == "paused"
    assert state["severity"] == "halted"
    assert "pause" in (state["reason"] or "").lower()

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=FALSE, ats_paused=TRUE, ats_pause_source='operator' WHERE id=1")
        conn.commit()

    payload = console_app.build_status()
    state = payload["apply_state"]
    assert state["code"] == "ats_paused"
    assert state["severity"] == "halted"
    assert "ats lane is paused" in (state["reason"] or "").lower()


def test_apply_state_reflects_spend_cap_reached(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        queue.push_apply_jobs(
            conn,
            [{
                "url": "https://boards.greenhouse.io/acme/jobs/4",
                "company": "Acme",
                "title": "Manager",
                "application_url": "https://boards.greenhouse.io/acme/jobs/4/apply",
                "score": 9.0,
                "target_host": "boards.greenhouse.io",
            }],
            approved_batch="batch-2",
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET est_cost_usd=%s WHERE url=%s",
                (4.5, "https://boards.greenhouse.io/acme/jobs/4"),
            )
            cur.execute("UPDATE fleet_config SET spend_cap_usd=3, canary_enabled=FALSE WHERE id=1")
        conn.commit()

    payload = console_app.build_status()
    state = payload["apply_state"]
    assert state["code"] == "spend_cap_reached"
    assert state["severity"] == "halted"
    assert state["reason"].lower().startswith("spend cap reached")


def test_apply_state_warns_when_no_leaseable_jobs_and_not_halted(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    payload = console_app.build_status()
    state = payload["apply_state"]
    assert state["code"] == "no_leaseable_jobs"
    assert state["severity"] == "warn"
    assert "no leaseable ats jobs" in (state["reason"] or "").lower()
