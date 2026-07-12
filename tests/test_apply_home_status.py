import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import apply_home_main


def test_print_status_includes_queue_diagnosis(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, score, status, lane, apply_domain, target_host, "
            "dedup_key, approved_batch, company, title) "
            "VALUES ('q1','https://example.test/q1',9,'queued','ats','boards.greenhouse.io',"
            "'boards.greenhouse.io','dk1','batch-1','HiringCafe','Chief of Staff')"
        )
        cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('dk1','HiringCafe')")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        apply_home_main._print_status(conn)

    out = capsys.readouterr().out
    assert "queue_diagnosis" in out
    assert "dedup_blocked" in out
    assert "ats_paused" in out
    assert "ats_pause_source" in out
    assert "fleet_config_updated_at" in out
    assert "desired_control" in out
    assert "apply_heartbeat_summary" in out
    assert "parkable_dead" in out
    assert "crash_liveness_task" in out


def test_crash_liveness_task_diagnosis_handles_missing_log(tmp_path):
    report = apply_home_main.crash_liveness_task_diagnosis(tmp_path / "missing.log")

    assert report["available"] is False
    assert report["healthy"] is False


def test_crash_liveness_task_diagnosis_rejects_nonzero_exit_and_stale_log(tmp_path):
    log = tmp_path / "crash-liveness.log"
    log.write_text(
        "[2020-01-01T00:00:00+00:00] start older_days=7 refresh_days=7 limit=25 once=True\n"
        "[2020-01-01T00:00:01+00:00] exit=1\n",
        encoding="utf-8",
    )

    report = apply_home_main.crash_liveness_task_diagnosis(log)

    assert report["available"] is True
    assert report["last_exit"] == 1
    assert report["healthy"] is False


def test_readiness_returns_explicit_resume_blockers(fleet_db, monkeypatch):
    monkeypatch.setattr(
        apply_home_main,
        "crash_liveness_task_diagnosis",
        lambda: {"available": True, "healthy": True, "last_exit": 0},
    )

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=true, ats_paused=true WHERE id=1")
        conn.commit()
        report = apply_home_main.readiness(conn)

    assert report["ready"] is False
    assert "operator_pause" in report["blockers"]
    assert "desired_workers_zero" in report["blockers"]
    assert report["heartbeat_blocks"] is False
    assert "crash_liveness_task" in report
    assert "next_action" in report


def test_readiness_strict_exit_is_non_mutating(fleet_db, monkeypatch, capsys):
    monkeypatch.setattr(
        apply_home_main,
        "crash_liveness_task_diagnosis",
        lambda: {"available": True, "healthy": True, "last_exit": 0},
    )
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET paused=true, ats_paused=true WHERE id=1")
        conn.commit()

    code = apply_home_main.main(["--dsn", fleet_db, "readiness", "--strict"])

    assert code == 2
    assert "operator_pause" in capsys.readouterr().out


def test_normalize_resolved_skipped_challenge_keeps_row_blocked(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, apply_status, "
            "lane, apply_error, updated_at) VALUES "
            "('stale-challenge','https://example.test/stale-challenge',9,'blocked',"
            "'challenge_pending','ats','challenge_pending',now())"
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, worker_id, kind, route, raised_at, resolved_at, outcome) "
            "VALUES ('stale-challenge','worker-1','login_gate','owner_inbox',"
            "now() - interval '2 days',now() - interval '1 day','skipped')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        assert apply_home_main.normalize_resolved_challenge_blocks(conn)["candidates"] == 1
        out = apply_home_main.normalize_resolved_challenge_blocks(conn, execute=True)
        assert out == {"dry_run": False, "candidates": 1, "normalized": 1}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, apply_status, apply_error FROM apply_queue WHERE url='stale-challenge'")
        row = cur.fetchone()
        assert (row["status"], row["apply_status"], row["apply_error"]) == (
            "blocked", "challenge_skipped", "challenge_skipped"
        )


def test_backfill_dedup_metadata_only_fills_unambiguous_values(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, company, score, status, apply_status, "
            "lane, dedup_key, apply_error) VALUES "
            "('dedup-job','https://example.test/apply','Acme',9,'blocked',"
            "'submission_uncertain:applied_set_protected','ats','dedup-1',"
            "'submission_uncertain:applied_set_protected')"
        )
        cur.execute("INSERT INTO applied_set (dedup_key) VALUES ('dedup-1')")
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        assert apply_home_main.backfill_dedup_metadata(conn)["candidates"] == 1
        out = apply_home_main.backfill_dedup_metadata(conn, execute=True)
        assert out == {"dry_run": False, "candidates": 1, "updated": 1}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT company, applied_url FROM applied_set WHERE dedup_key='dedup-1'")
        row = cur.fetchone()
        assert row["company"] == "Acme"
        assert row["applied_url"] == "https://example.test/apply"


def test_clear_terminal_leases_changes_only_lease_metadata(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, apply_status, apply_error, "
            "lease_owner, lease_expires_at) VALUES "
            "('terminal-lease','https://example.test/terminal-lease',9,'failed','ats','failed','failed:browser_unavailable',"
            "'worker-1',now() + interval '1 hour')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        assert apply_home_main.clear_terminal_leases(conn)["candidates"] == 1
        out = apply_home_main.clear_terminal_leases(conn, execute=True)
        assert out == {"dry_run": False, "candidates": 1, "cleared": 1}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, apply_error, lease_owner, lease_expires_at FROM apply_queue WHERE url='terminal-lease'")
        row = cur.fetchone()
        assert row["status"] == "failed"
        assert row["apply_error"] == "failed:browser_unavailable"
        assert row["lease_owner"] is None
        assert row["lease_expires_at"] is None


def test_quarantine_invalid_queued_parks_linkedin_and_missing_identity(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, company, title, score, status, lane) VALUES "
            "('https://www.linkedin.com/jobs/view/1','https://www.linkedin.com/jobs/view/1','Acme','Analyst',9,'queued','ats'),"
            "('https://example.test/job/2','https://example.test/job/2',NULL,'Analyst',9,'queued','ats'),"
            "('https://example.test/job/3','https://example.test/job/3','Acme','Analyst',9,'queued','ats')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        assert apply_home_main.quarantine_invalid_queued(conn)["candidates"] == 2
        out = apply_home_main.quarantine_invalid_queued(conn, execute=True)
        assert out == {"dry_run": False, "candidates": 2, "quarantined": 2}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT url,status,apply_status,apply_error FROM apply_queue ORDER BY url")
        rows = {r["url"]: r for r in cur.fetchall()}
        assert rows["https://www.linkedin.com/jobs/view/1"]["status"] == "blocked"
        assert rows["https://www.linkedin.com/jobs/view/1"]["apply_status"] == "manual_review_required"
        assert rows["https://example.test/job/2"]["status"] == "blocked"
        assert rows["https://example.test/job/3"]["status"] == "queued"


def test_quarantine_residual_failures_records_original_reason(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, company, title, score, status, apply_status, lane, apply_error) "
            "VALUES ('residual-failure','https://example.test/residual','Acme','Analyst',9,"
            "'failed','failed','ats','failed:unsupported_form')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        out = apply_home_main.quarantine_residual_failures(conn, execute=True)
        assert out == {"dry_run": False, "candidates": 1, "quarantined": 1}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, apply_status, apply_error FROM apply_queue "
            "WHERE url='residual-failure'"
        )
        row = cur.fetchone()
        assert (row["status"], row["apply_status"], row["apply_error"]) == (
            "blocked", "manual_review_required", "manual_review:residual_failure"
        )
        cur.execute(
            "SELECT target, message FROM fleet_console_audit "
            "WHERE action='quarantine_residual_failure'"
        )
        audit = cur.fetchone()
        assert audit["target"] == "residual-failure"
        assert audit["message"].endswith("original_reason=failed:unsupported_form")


def test_retry_infrastructure_pending_requires_zero_tools_and_dedup_clear(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, company, title, score, status, apply_status, lane, "
            "dedup_key, apply_error, infrastructure_failure_count, infrastructure_last_failure_at) "
            "VALUES "
            "('infra-safe','https://example.test/infra-safe','Acme','Analyst',9,'failed',"
            "'infrastructure_pending','ats','infra-safe-key','failed:browser_preflight:cdp',1,"
            "now() - interval '2 hours'),"
            "('infra-touched','https://example.test/infra-touched','Acme','Analyst',9,'failed',"
            "'infrastructure_pending','ats','infra-touched-key','failed:browser_preflight:cdp',1,"
            "now() - interval '2 hours')"
        )
        cur.execute(
            "INSERT INTO apply_result_events (url, application_tool_calls, status, apply_error) "
            "VALUES ('infra-safe',0,'failed','failed:browser_preflight:cdp'),"
            "('infra-touched',2,'failed','failed:browser_preflight:cdp')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        out = apply_home_main.retry_infrastructure_pending(
            conn, cooldown_hours=1, max_failures=1
        )
        assert out == {"dry_run": True, "candidates": 1, "requeued": 0}
        out = apply_home_main.retry_infrastructure_pending(
            conn, cooldown_hours=1, max_failures=1, execute=True
        )
        assert out == {"dry_run": False, "candidates": 1, "requeued": 1}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, apply_status, apply_error FROM apply_queue WHERE url='infra-safe'")
        row = cur.fetchone()
        assert (row["status"], row["apply_status"], row["apply_error"]) == (
            "queued", "queued", "requeued_by_repair:infrastructure_pending"
        )
        cur.execute("SELECT status FROM apply_queue WHERE url='infra-touched'")
        assert cur.fetchone()["status"] == "failed"


def test_quarantine_missing_infrastructure_evidence_never_requeues(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, company, title, score, status, "
            "apply_status, lane, apply_error) VALUES "
            "('infra-no-event','https://example.test/infra-no-event','Acme','Analyst',9,'failed',"
            "'failed','ats','failed:browser_unavailable')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        assert apply_home_main.quarantine_missing_infrastructure_evidence(conn)["candidates"] == 1
        out = apply_home_main.quarantine_missing_infrastructure_evidence(conn, execute=True)
        assert out == {"dry_run": False, "candidates": 1, "quarantined": 1}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, apply_status, apply_error FROM apply_queue WHERE url='infra-no-event'")
        row = cur.fetchone()
        assert (row["status"], row["apply_status"], row["apply_error"]) == (
            "blocked", "manual_review_required", "submission_uncertain:infrastructure_evidence_gap"
        )


def test_quarantine_residual_failures_audits_original_reason(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, company, title, score, status, "
            "apply_status, lane, apply_error) VALUES "
            "('residual-reason','https://example.test/residual-reason','Acme','Analyst',9,'failed',"
            "'failed','ats','failed:validation_stuck')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        out = apply_home_main.quarantine_residual_failures(conn, execute=True)
        assert out == {"dry_run": False, "candidates": 1, "quarantined": 1}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, apply_error FROM apply_queue WHERE url='residual-reason'"
        )
        row = cur.fetchone()
        assert (row["status"], row["apply_error"]) == (
            "blocked", "manual_review:residual_failure"
        )
        cur.execute(
            "SELECT message FROM fleet_console_audit "
            "WHERE action='quarantine_residual_failure' AND target='residual-reason'"
        )
        assert "failed:validation_stuck" in cur.fetchone()["message"]


def test_print_status_includes_challenge_age_diagnosis(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, score, status, apply_status, lane, lease_owner, "
            "lease_expires_at, updated_at) "
            "VALUES ('parked1','https://example.test/parked1',9,'leased',"
            "'challenge_pending','ats','worker-1',now() + interval '3650 days',now())"
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, worker_id, kind, route, raised_at) "
            "VALUES ('https://example.test/parked1','worker-1','login_gate',"
            "'owner_inbox',now() - interval '2 days')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        apply_home_main._print_status(conn)

    out = capsys.readouterr().out
    assert "challenge_diagnosis" in out
    assert "old_open" in out
    assert "parked_without_open" in out
    assert "blocked_skipped" in out
    assert "active_pending" in out
    assert "challenge_kinds" in out


def test_print_status_keeps_linkedin_challenges_out_of_ats_count(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO linkedin_queue (url, application_url, score, status, lane, apply_status) "
            "VALUES ('https://www.linkedin.com/jobs/view/status-li', "
            "'https://www.linkedin.com/jobs/view/status-li', 8, 'leased', 'linkedin', "
            "'challenge_pending')"
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, worker_id, kind, route, raised_at) "
            "VALUES ('https://www.linkedin.com/jobs/view/status-li','li-1','visible_captcha',"
            "'owner_inbox',now() - interval '2 days')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        apply_home_main._print_status(conn)

    out = capsys.readouterr().out
    assert "'open_challenges': 0" in out
    assert "'linkedin_open_challenges': 1" in out


def test_print_status_does_not_attribute_ats_challenge_to_nonparked_linkedin_url(fleet_db, capsys):
    url = "https://shared.example/jobs/status-collision"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, score, status, apply_status, lane, lease_owner, lease_expires_at) "
            "VALUES (%s,%s,9,'leased','challenge_pending','ats','ats-worker',now()+interval '3650 days')",
            (url, url),
        )
        cur.execute(
            "INSERT INTO linkedin_queue (url, application_url, score, status, lane, apply_status) "
            "VALUES (%s,%s,9,'failed','linkedin','failed')",
            (url, url),
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, worker_id, kind, route) "
            "VALUES (%s,'ats-worker','login_gate','owner_inbox')",
            (url,),
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        apply_home_main._print_status(conn)
    out = capsys.readouterr().out
    assert "'open_challenges': 1" in out
    assert "'linkedin_open_challenges': 0" in out


def test_cleanup_terminal_challenges_is_dry_run_then_closes_only_terminal_ats_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, apply_status) "
            "VALUES ('terminal-challenge', 'https://example.test/terminal', 8, 'failed', 'ats', 'failed')"
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, kind, route, raised_at) VALUES "
            "('terminal-challenge', 'manual_auth', 'owner_inbox', now() - interval '3 days')"
        )
        conn.commit()

        dry = apply_home_main.cleanup_terminal_challenges(conn, older_days=1)
        assert dry == {"dry_run": True, "candidates": 1, "closed": 0}

        done = apply_home_main.cleanup_terminal_challenges(conn, older_days=1, execute=True)
        assert done == {"dry_run": False, "candidates": 1, "closed": 1}
        cur.execute("SELECT resolved_at, outcome FROM auth_challenge WHERE url='terminal-challenge'")
        row = cur.fetchone()
        assert row["resolved_at"] is not None
        assert row["outcome"] == "stale_terminal_queue"


def test_cleanup_terminal_challenges_preserves_cross_lane_active_url(fleet_db):
    url = "shared-active-challenge"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, apply_status) "
            "VALUES (%s, 'https://example.test/terminal', 8, 'failed', 'ats', 'failed')",
            (url,),
        )
        cur.execute(
            "INSERT INTO linkedin_queue (url, application_url, score, status, lane, apply_status) "
            "VALUES (%s, 'https://linkedin.com/jobs/view/shared', 8, 'leased', 'linkedin', 'challenge_pending')",
            (url,),
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, kind, route, raised_at) VALUES "
            "(%s, 'login_gate', 'owner_inbox', now() - interval '3 days')",
            (url,),
        )
        conn.commit()

        result = apply_home_main.cleanup_terminal_challenges(conn, older_days=1, execute=True)
        assert result == {"dry_run": False, "candidates": 0, "closed": 0}
        cur.execute("SELECT resolved_at FROM auth_challenge WHERE url=%s", (url,))
        assert cur.fetchone()["resolved_at"] is None


def test_retire_stale_parked_challenges_is_bounded_and_skips_without_retry(fleet_db):
    url = "stale-parked-challenge"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, score, status, apply_status, lane, worker_id, "
            "lease_owner, lease_expires_at, updated_at) VALUES "
            "(%s,%s,9,'leased','challenge_pending','ats','worker-1','worker-1',"
            "now()+interval '3650 days',now()-interval '8 days')",
            (url, "https://example.test/stale-parked"),
        )
        cur.execute(
            "INSERT INTO auth_challenge (url, worker_id, kind, route, raised_at) "
            "VALUES (%s,'worker-1','login_gate','owner_inbox',now()-interval '8 days')",
            (url,),
        )
        conn.commit()

        dry = apply_home_main.retire_stale_parked_challenges(
            conn, older_days=7, limit=10,
        )
        assert dry["dry_run"] is True
        assert dry["candidates"] == 1
        assert dry["retired"] == 0

        done = apply_home_main.retire_stale_parked_challenges(
            conn, older_days=7, limit=10, execute=True,
        )
        assert done["retired"] == 1
        cur.execute(
            "SELECT status, apply_status, apply_error, lease_owner, lease_expires_at "
            "FROM apply_queue WHERE url=%s",
            (url,),
        )
        row = cur.fetchone()
        assert row["status"] == "blocked"
        assert row["apply_status"] == "challenge_skipped"
        assert row["apply_error"] == "challenge_skipped"
        assert row["lease_owner"] is None
        assert row["lease_expires_at"] is None

def test_repair_orphan_challenges_is_dry_run_then_creates_owner_record(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, apply_status, "
            "lease_owner, lease_expires_at, updated_at) VALUES "
            "('orphan-challenge', 'https://example.test/apply', 8, 'leased', 'ats', "
            "'challenge_pending', 'worker-7', now() + interval '3650 days', now() - interval '3 days')"
        )
        conn.commit()

        dry = apply_home_main.repair_orphan_challenges(conn, older_days=1)
        assert dry == {"dry_run": True, "candidates": 1, "created": 0}

        done = apply_home_main.repair_orphan_challenges(conn, older_days=1, execute=True)
        assert done == {"dry_run": False, "candidates": 1, "created": 1}
        cur.execute("SELECT worker_id, kind, route FROM auth_challenge WHERE url='orphan-challenge'")
        assert dict(cur.fetchone()) == {
            "worker_id": "worker-7", "kind": "manual_auth", "route": "owner_inbox"
        }


def test_print_status_includes_crash_evidence_diagnosis(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, "
            "apply_error, dedup_key) VALUES "
            "('crash1','https://example.test/crash1',9,'crash_unconfirmed','ats',"
            "'failed:no_result_line','dk-crash1')"
        )
        cur.execute(
            "INSERT INTO apply_result_events (url, queue_name, application_tool_calls, status) "
            "VALUES ('crash1','apply_queue',0,'crash_unconfirmed')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        apply_home_main._print_status(conn)

    out = capsys.readouterr().out
    assert "crash_diagnosis" in out
    assert "safe_pre_touch_candidates" in out
    assert "zero_tool_calls" in out
    assert "manual_review_audited" in out
    assert "no_autotriage_audit" in out
    assert "lease_started_no_terminal" in out
    assert "older_7d" in out
    assert "oldest_crash_at" in out
    assert "liveness_dead" in out
    assert "liveness_live" in out
    assert "liveness_uncertain" in out
    assert "evidence_freshness" in out
    assert "worker_missing_evidence_24h" in out
    assert "otp_diagnosis" in out
    assert "pending_requests" in out


def test_retire_stale_unapproved_is_capped_and_never_leased(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, "
            "approved_batch, attempts, updated_at) VALUES "
            "('stale-unapproved','https://example.test/stale',8,'queued','ats',NULL,0,"
            "now() - interval '10 days'),"
            "('fresh-unapproved','https://example.test/fresh',8,'queued','ats',NULL,0,now()),"
            "('attempted-unapproved','https://example.test/attempted',8,'queued','ats',NULL,1,"
            "now() - interval '10 days')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        dry = apply_home_main.retire_stale_unapproved(conn, older_days=7, limit=10)
        assert dry == {"dry_run": True, "candidates": 1, "retired": 0}
        done = apply_home_main.retire_stale_unapproved(conn, older_days=7, limit=10, execute=True)
        assert done == {"dry_run": False, "candidates": 1, "retired": 1}
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, apply_status, apply_error FROM apply_queue "
                "WHERE url='stale-unapproved'"
            )
            assert dict(cur.fetchone()) == {
                "status": "failed", "apply_status": "expired", "apply_error": "stale_unapproved"
            }
            cur.execute(
                "SELECT action, target FROM fleet_console_audit "
                "WHERE action='retire_stale_unapproved'"
            )
            assert dict(cur.fetchone()) == {
                "action": "retire_stale_unapproved", "target": "1 rows"
            }


def test_quarantine_unsafe_approved_is_dry_run_then_blocks(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, "
            "approved_batch, apply_error) VALUES "
            "('unsafe-approved','https://example.test/unsafe',9,'queued','ats',"
            "'batch-1','requeued_by_remediator:usage_limit')"
        )
        conn.commit()

        dry = apply_home_main.quarantine_unsafe_approved(conn, limit=10)
        assert dry == {"dry_run": True, "candidates": 1, "blocked": 0}

        done = apply_home_main.quarantine_unsafe_approved(conn, limit=10, execute=True)
        assert done == {"dry_run": False, "candidates": 1, "blocked": 1}
        cur.execute(
            "SELECT status, apply_status, apply_error, synced_to_home_at "
            "FROM apply_queue WHERE url='unsafe-approved'"
        )
        assert dict(cur.fetchone()) == {
            "status": "blocked",
            "apply_status": "manual_review_required",
            "apply_error": "unsafe_prior_attempt",
            "synced_to_home_at": None,
        }
        cur.execute(
            "SELECT action, target FROM fleet_console_audit "
            "WHERE action='quarantine_unsafe_approved'"
        )
        assert dict(cur.fetchone()) == {
            "action": "quarantine_unsafe_approved", "target": "1 rows"
        }


def test_clear_blocked_leases_only_clears_lease_metadata(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, "
            "apply_status, apply_error, lease_owner, lease_expires_at) VALUES "
            "('blocked-lease','https://example.test/blocked-lease',8,'blocked','ats',"
            "'blocked:submission_uncertain','submission_uncertain:evidence_gap','w1',"
            "now()+interval '1 day')"
        )
        conn.commit()
        dry = apply_home_main.clear_blocked_leases(conn, limit=10)
        assert dry == {"dry_run": True, "candidates": 1, "cleared": 0}
        done = apply_home_main.clear_blocked_leases(conn, limit=10, execute=True)
        assert done == {"dry_run": False, "candidates": 1, "cleared": 1}
        cur.execute(
            "SELECT status, apply_status, apply_error, lease_owner, lease_expires_at "
            "FROM apply_queue WHERE url='blocked-lease'"
        )
        assert dict(cur.fetchone()) == {
            "status": "blocked", "apply_status": "blocked:submission_uncertain",
            "apply_error": "submission_uncertain:evidence_gap",
            "lease_owner": None, "lease_expires_at": None,
        }


def test_retire_stale_unapproved_can_include_zero_tool_pre_touch_requeues(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, "
            "approved_batch, attempts, last_attempted_at, apply_error, updated_at) VALUES "
            "('safe-requeue','https://example.test/safe-requeue',5.7,'queued','ats',NULL,0,"
            "now() - interval '10 days','requeued_by_autotriage:pre_touch_crash',"
            "now() - interval '10 days')"
        )
        cur.execute(
            "INSERT INTO apply_result_events (url, queue_name, status, apply_error, "
            "application_tool_calls, source) VALUES "
            "('safe-requeue','apply_queue','failed','failed:timeout',0,'worker')"
        )
        conn.commit()

        dry = apply_home_main.retire_stale_unapproved(
            conn, older_days=7, limit=10, include_safe_requeues=True
        )
        assert dry == {"dry_run": True, "candidates": 1, "retired": 0}
        done = apply_home_main.retire_stale_unapproved(
            conn, older_days=7, limit=10, include_safe_requeues=True, execute=True
        )
        assert done == {"dry_run": False, "candidates": 1, "retired": 1}


def test_canary_readiness_reports_gate_and_row_warnings(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, "
            "approved_batch, dedup_key, apply_error) VALUES "
            "('canary-ready','https://example.test/ready',8,'queued','ats','batch-1','dk-ready',NULL),"
            "('canary-warning','https://example.test/warning',7.5,'queued','ats','batch-1','dk-warning',"
            "'requeued_by_remediator:usage_limit')"
        )
        cur.execute(
            "INSERT INTO apply_result_events (url, queue_name, status, apply_error, "
            "application_tool_calls, source) VALUES "
            "('canary-warning','apply_queue','failed','requeued_by_remediator:usage_limit',4,'worker')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        report = apply_home_main.canary_readiness(conn)
    assert report["gate_ready"] is False
    assert report["pause_source"] is None
    assert report["candidate_count"] == 2
    assert report["next_action"] == "review ready rows, then arm a bounded canary"
    by_url = {row["url"]: row for row in report["rows"]}
    assert by_url["canary-ready"]["ready"] is True
    assert by_url["canary-warning"]["ready"] is False
    assert by_url["canary-warning"]["blockers"] == [
        "prior_requeue", "prior_browser_tool_calls"
    ]


def test_crash_liveness_review_stamps_metadata_without_changing_crash_state(fleet_db, monkeypatch):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, updated_at) "
            "VALUES ('dead-crash','https://example.test/dead',9,'crash_unconfirmed','ats',"
            "now() - interval '10 days'),"
            "('live-crash','https://example.test/live',8,'crash_unconfirmed','ats',"
            "now() - interval '10 days')"
        )
        conn.commit()
    monkeypatch.setattr(
        "applypilot.apply.liveness.probe_url",
        lambda url: ("dead", "http_404") if url.endswith("/dead") else ("live", "ok_200"),
    )
    with pgqueue.connect(fleet_db) as conn:
        report = apply_home_main.review_crash_liveness(
            conn, older_days=7, limit=10, execute=True, refresh_days=7
        )
        assert report["dead"] == 1 and report["live"] == 1
        with conn.cursor() as cur:
            cur.execute(
                "SELECT url, status, liveness_status, liveness_reason "
                "FROM apply_queue WHERE url IN ('dead-crash','live-crash') ORDER BY url"
            )
            rows = cur.fetchall()
    assert rows[0]["status"] == "crash_unconfirmed"
    assert rows[0]["liveness_status"] == "dead"
    assert rows[1]["liveness_status"] == "live"


def test_crash_review_lists_evidence_and_latest_audit(fleet_db, capsys):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, company, title, status, lane, "
            "apply_error, worker_id, updated_at) VALUES "
            "('crash-review-1','https://example.test/crash-review-1',8.4,'Acme','Analyst',"
            "'crash_unconfirmed','ats','failed:no_result_line','worker-1',now() - interval '3 days')"
        )
        cur.execute(
            "INSERT INTO apply_result_events (url, queue_name, application_tool_calls, source) "
            "VALUES ('crash-review-1','apply_queue',0,'worker')"
        )
        cur.execute(
            "INSERT INTO autotriage_actions (url, chosen_action, action_status, reason) "
            "VALUES ('crash-review-1','requeue_pre_touch_crash','rejected','test audit')"
        )
        cur.execute(
            "UPDATE apply_queue SET liveness_status='live', liveness_reason='ok_200', "
            "liveness_checked_at=now() WHERE url='crash-review-1'"
        )
        conn.commit()

    apply_home_main.main(["--dsn", fleet_db, "crash-review", "--limit", "1"])
    out = capsys.readouterr().out

    assert "crash-review-1" in out
    assert "application_tool_calls" in out
    assert "requeue_pre_touch_crash" in out
    assert "test audit" in out

    with pgqueue.connect(fleet_db) as conn:
        zero = apply_home_main.list_crash_reviews(conn, limit=5, evidence="zero_tool_calls")
        missing = apply_home_main.list_crash_reviews(conn, limit=5, evidence="no_result_event")
        priority_old = apply_home_main.list_crash_reviews(
            conn, limit=5, older_days=1, priority=True
        )
        live = apply_home_main.list_crash_reviews(conn, limit=5, liveness="live")
        unchecked = apply_home_main.list_crash_reviews(conn, limit=5, liveness="unchecked")
    assert [row["url"] for row in zero] == ["crash-review-1"]
    assert missing == []
    assert [row["url"] for row in priority_old] == ["crash-review-1"]
    assert [row["url"] for row in live] == ["crash-review-1"]
    assert unchecked == []
    with pgqueue.connect(fleet_db) as conn:
        assert apply_home_main.list_crash_reviews(conn, limit=5, unreviewed=True) == []


def test_resolve_crash_is_explicit_audited_and_idempotency_guarded(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, "
            "apply_error, attempts, lease_owner) VALUES "
            "('manual-crash-1','https://example.test/manual-crash-1',8.1,"
            "'crash_unconfirmed','ats','crash_unconfirmed',3,'worker-1')"
        )
        conn.commit()

        result = apply_home_main.resolve_crash(
            conn, "manual-crash-1", outcome="applied",
            reason="confirmation email matched the application", actor="operator-1",
        )
        assert result["status"] == "applied"

        cur.execute(
            "SELECT status, apply_status, apply_error, lease_owner, applied_at, "
            "synced_to_home_at FROM apply_queue WHERE url='manual-crash-1'"
        )
        row = cur.fetchone()
        assert row["status"] == "applied"
        assert row["apply_status"] == "applied"
        assert row["apply_error"] is None
        assert row["lease_owner"] is None
        assert row["applied_at"] is not None
        assert row["synced_to_home_at"] is None

        cur.execute(
            "SELECT source, final_result_source, result_metadata, result_line "
            "FROM apply_result_events WHERE url='manual-crash-1' ORDER BY id DESC LIMIT 1"
        )
        event = cur.fetchone()
        assert event["source"] == "operator"
        assert event["final_result_source"] == "operator_crash_resolution"
        assert event["result_metadata"]["resolution"] == "manual_crash_resolution"
        assert event["result_line"].startswith("manual_resolution:applied:")

        cur.execute(
            "SELECT actor, action, target, ok FROM fleet_console_audit "
            "WHERE action='resolve_crash' AND target='manual-crash-1' ORDER BY id DESC LIMIT 1"
        )
        audit = cur.fetchone()
        assert (audit["actor"], audit["action"], audit["target"], audit["ok"]) == (
            "operator-1", "resolve_crash", "manual-crash-1", True
        )

        with pytest.raises(ValueError, match="not crash_unconfirmed"):
            apply_home_main.resolve_crash(
                conn, "manual-crash-1", outcome="failed", reason="repeat attempt"
            )


def test_park_dead_crashes_requires_fresh_dead_evidence_and_preserves_uncertainty(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, dedup_key, score, status, lane, attempts, "
            "updated_at, liveness_status, liveness_reason, liveness_checked_at) VALUES "
            "('dead-park-1','https://example.test/dead-park-1','dead-dk-1',8.4,"
            "'crash_unconfirmed','ats',1,now()-interval '8 days','dead','http_404',"
            "now()-interval '1 day'),"
            "('dead-park-tool','https://example.test/dead-park-tool','dead-dk-tool',8.4,"
            "'crash_unconfirmed','ats',1,now()-interval '8 days','dead','http_404',"
            "now()-interval '1 day')"
        )
        cur.execute(
            "INSERT INTO apply_result_events "
            "(queue_name,url,status,apply_status,application_tool_calls) "
            "VALUES ('apply_queue','dead-park-tool','failed','failed',2)"
        )
        conn.commit()

        dry = apply_home_main.park_dead_crashes(conn, limit=10)
        assert dry["candidates"] == 1
        assert dry["parked"] == 0
        assert dry["guard_counts"]["dead_rows"] == 2
        assert dry["guard_counts"]["tool_touched"] == 1

        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, dedup_key, score, status, lane, attempts, "
            "updated_at, liveness_status, liveness_reason, liveness_checked_at) VALUES "
            "('dead-park-set','https://example.test/dead-park-set','dead-dk-set',8.4,"
            "'crash_unconfirmed','ats',1,now()-interval '8 days','dead','http_404',"
            "now()-interval '1 day')"
        )
        cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('dead-dk-set','Acme')")
        conn.commit()
        with_set = apply_home_main.park_dead_crashes(
            conn, limit=10, include_applied_set=True,
        )
        assert with_set["candidates"] == 2
        assert with_set["include_applied_set"] is True

        done = apply_home_main.park_dead_crashes(conn, limit=10, execute=True)
        assert done["candidates"] == 1
        assert done["parked"] == 1

        cur.execute(
            "SELECT status, apply_status, apply_error, lease_owner "
            "FROM apply_queue WHERE url='dead-park-1'"
        )
        row = cur.fetchone()
        assert (row["status"], row["apply_status"], row["apply_error"], row["lease_owner"]) == (
            "blocked", "blocked:submission_uncertain",
            "submission_uncertain:dead_posting", None,
        )
        cur.execute(
            "SELECT source, final_result_source, result_metadata "
            "FROM apply_result_events WHERE url='dead-park-1' ORDER BY id DESC LIMIT 1"
        )
        event = cur.fetchone()
        assert event["source"] == "operator"
        assert event["final_result_source"] == "operator_dead_posting_park"
        assert event["result_metadata"]["outcome_confirmed"] is False


def test_park_protected_crashes_reclassifies_only_unresolved_applied_set_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, dedup_key, score, status, lane, attempts, "
            "updated_at, liveness_status, apply_error) VALUES "
            "('protected-live','https://example.test/protected-live','protected-dk',8.4,"
            "'crash_unconfirmed','ats',99,now()-interval '8 days','live','crash_unconfirmed')"
        )
        cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('protected-dk','Acme')")
        conn.commit()

        dry = apply_home_main.park_protected_crashes(conn, limit=10)
        assert dry["candidates"] == 1
        assert dry["guard_counts"]["attempts_99"] == 1
        assert dry["parked"] == 0

        done = apply_home_main.park_protected_crashes(conn, limit=10, execute=True)
        assert done["parked"] == 1
        cur.execute(
            "SELECT status, apply_status, apply_error FROM apply_queue "
            "WHERE url='protected-live'"
        )
        row = cur.fetchone()
        assert (row["status"], row["apply_status"], row["apply_error"]) == (
            "blocked", "blocked:submission_uncertain",
            "submission_uncertain:applied_set_protected",
        )
        cur.execute("SELECT 1 FROM applied_set WHERE dedup_key='protected-dk'")
        assert cur.fetchone() is not None


def test_park_protected_crashes_can_explicitly_park_tool_touched_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, dedup_key, score, status, lane, attempts, "
            "updated_at, liveness_status, apply_error) VALUES "
            "('protected-touched','https://example.test/protected-touched','touched-dk',8.4,"
            "'crash_unconfirmed','ats',1,now()-interval '8 days','live','failed:timeout')"
        )
        cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('touched-dk','Acme')")
        cur.execute(
            "INSERT INTO apply_result_events "
            "(queue_name,url,status,apply_status,application_tool_calls) "
            "VALUES ('apply_queue','protected-touched','failed','failed',3)"
        )
        conn.commit()

        dry = apply_home_main.park_protected_crashes(conn, limit=10)
        assert dry["candidates"] == 0
        with_tool = apply_home_main.park_protected_crashes(
            conn, limit=10, include_tool_touched=True,
        )
        assert with_tool["candidates"] == 1
        assert with_tool["include_tool_touched"] is True
        done = apply_home_main.park_protected_crashes(
            conn, limit=10, execute=True, include_tool_touched=True,
        )
        assert done["parked"] == 1
        cur.execute("SELECT apply_error FROM apply_queue WHERE url='protected-touched'")
        assert cur.fetchone()["apply_error"] == "submission_uncertain:tool_touched"


def test_park_evidence_gap_crashes_handles_unprotected_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, score, status, lane, attempts, updated_at, apply_error) "
            "VALUES ('evidence-gap','https://example.test/evidence-gap',8.1,"
            "'crash_unconfirmed','ats',1,now()-interval '8 days','failed:no_result_line')"
        )
        conn.commit()
        dry = apply_home_main.park_protected_crashes(
            conn, older_days=0, limit=10, require_applied_set=False,
        )
        assert dry["candidates"] == 1
        assert dry["require_applied_set"] is False
        done = apply_home_main.park_protected_crashes(
            conn, older_days=0, limit=10, execute=True, require_applied_set=False,
        )
        assert done["parked"] == 1
        cur.execute("SELECT apply_error FROM apply_queue WHERE url='evidence-gap'")
        assert cur.fetchone()["apply_error"] == "submission_uncertain:evidence_gap"


def test_park_email_review_crashes_preserves_unconfirmed_outcome(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue "
            "(url, application_url, score, status, lane, attempts, updated_at, apply_error) "
            "VALUES ('email-review','https://example.test/email-review',8.1,"
            "'crash_unconfirmed','ats',1,now()-interval '8 days','email_reconcile_review_required')"
        )
        cur.execute(
            "INSERT INTO inbox_outcomes "
            "(message_id, job_url, company, title, stage, confidence) VALUES "
            "('email-review-msg','https://example.test/email-review','Acme',"
            "'Chief of Staff','acknowledged','high')"
        )
        conn.commit()
        dry = apply_home_main.park_protected_crashes(
            conn, older_days=0, limit=10, require_applied_set=False,
            include_tool_touched=True, include_inbox=True, require_inbox=True,
        )
        assert dry["candidates"] == 1
        done = apply_home_main.park_protected_crashes(
            conn, older_days=0, limit=10, execute=True, require_applied_set=False,
            include_tool_touched=True, include_inbox=True, require_inbox=True,
        )
        assert done["parked"] == 1
        cur.execute("SELECT status, applied_at FROM apply_queue WHERE url='email-review'")
        row = cur.fetchone()
        assert row["status"] == "blocked"
        assert row["applied_at"] is None
        cur.execute("SELECT apply_error FROM apply_queue WHERE url='email-review'")
        assert cur.fetchone()["apply_error"] == "submission_uncertain:email_match_review"
