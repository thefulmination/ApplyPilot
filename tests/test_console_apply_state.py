"""Tests for apply-readiness state surfaced by /api/status."""
from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import console_app, queue


def test_status_exposes_ranked_uncertain_liveness_reasons(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO apply_queue
                (url, company, title, application_url, score, lane, status,
                 target_host, liveness_status, liveness_reason, liveness_checked_at)
            VALUES
                ('live-1','Acme','A','https://acme.test/a',9,'ats','queued',
                 'acme.test','live','ok_200',now()),
                ('uncertain-1','Acme','B','https://acme.test/b',9,'ats','queued',
                 'acme.test','uncertain','redirect_login',now()),
                ('uncertain-2','Beta','C','https://beta.test/c',9,'ats','queued',
                 'beta.test','uncertain','redirect_login',now()),
                ('uncertain-3','Gamma','D','https://gamma.test/d',9,'ats','queued',
                 'gamma.test','uncertain',NULL,now()),
                ('unchecked-1','Delta','E','https://delta.test/e',9,'ats','queued',
                 'delta.test',NULL,NULL,NULL)
            """
        )
        conn.commit()

    payload = console_app.build_status()
    summary = payload["liveness"]
    assert summary["available"] is True
    assert summary["error"] is None
    assert summary["totals"] == {"live": 1, "dead": 0, "uncertain": 3, "unchecked": 1}
    assert summary["uncertain_reasons"][0]["reason"] == "redirect_login"
    assert summary["uncertain_reasons"][0]["rows"] == 2
    assert summary["uncertain_reasons"][0]["hosts"] == 2
    assert summary["uncertain_reasons"][0]["latest_checked_at"] is not None
    assert summary["uncertain_reasons"][0]["max_consecutive_uncertain"] == 0
    assert summary["uncertain_reasons"][0]["retry_class"] == "structural"
    assert summary["uncertain_reasons"][0]["retry_after_seconds"] == 24 * 60 * 60
    assert summary["uncertain_reasons"][1]["reason"] == "missing_reason"
    assert 'id="livenessBody"' in console_app._INDEX_HTML
    assert "uncertain_reasons" in console_app._INDEX_HTML


def test_status_does_not_report_zero_liveness_when_query_fails(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    monkeypatch.setattr(
        console_app,
        "_liveness_summary",
        lambda _conn: (_ for _ in ()).throw(RuntimeError("schema drift")),
    )

    summary = console_app.build_status()["liveness"]

    assert summary == {
        "available": False,
        "error": "RuntimeError",
        "totals": None,
        "uncertain_reasons": [],
        "host_cooldowns": [],
    }
    assert "Telemetry unavailable" in console_app._INDEX_HTML


def test_status_exposes_active_host_liveness_cooldowns(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO apply_queue
                (url, company, title, application_url, score, lane, status,
                 target_host, liveness_required, liveness_status, liveness_reason,
                 liveness_checked_at)
            VALUES
                ('cooldown-1','Acme','A','https://acme.test/a',9,'ats','queued',
                 'acme.test',TRUE,'uncertain','server_503',now()),
                ('cooldown-2','Acme','B','https://acme.test/b',8,'ats','queued',
                 'acme.test',TRUE,NULL,NULL,NULL),
                ('specific-1','Beta','C','https://beta.test/c',9,'ats','queued',
                 'beta.test',TRUE,'uncertain','redirect_login',now())
            """
        )
        conn.commit()

    cooldowns = console_app.build_status()["liveness"]["host_cooldowns"]
    assert len(cooldowns) == 1
    assert cooldowns[0]["host"] == "acme.test"
    assert cooldowns[0]["reason"] == "server_503"
    assert cooldowns[0]["deferred_rows"] == 2
    assert 0 < cooldowns[0]["remaining_seconds"] <= 30 * 60


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
    assert state["code"] == "operator_paused"
    assert state["severity"] == "info"
    assert "operator" in (state["reason"] or "").lower()


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
