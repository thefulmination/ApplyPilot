import json
import sqlite3

from applypilot.apply import pgqueue
from applypilot.fleet import repair_report, repair_report_main


def _seed_apply_row(
    cur,
    *,
    url,
    status="crash_unconfirmed",
    company="Acme",
    title="Analyst",
    dedup_key="dk",
    apply_domain="acme.com",
    apply_error=None,
    worker_id="w1",
    agent_model="sonnet",
    duration_ms=1234,
    approved=True,
    application_url=None,
):
    cur.execute(
        "INSERT INTO apply_queue "
        "(url, application_url, score, status, lane, apply_domain, target_host, "
        "dedup_key, approved_batch, company, title, apply_error, worker_id, "
        "agent_model, apply_duration_ms, updated_at) "
        "VALUES (%s,%s,9,%s,'ats',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        "'2026-06-30T00:00:00+00:00'::timestamptz)",
        (
            url,
            application_url or f"https://{apply_domain}/jobs/{url}",
            status,
            apply_domain,
            apply_domain,
            dedup_key,
            "batch-1" if approved else None,
            company,
            title,
            apply_error,
            worker_id,
            agent_model,
            duration_ms,
        ),
    )


def _mk_home_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE email_events (message_id TEXT PRIMARY KEY, sender TEXT, subject TEXT, "
        "body_text TEXT, company TEXT, title TEXT, job_url TEXT, stage TEXT NOT NULL, "
        "occurred_at TEXT)"
    )
    conn.execute(
        "INSERT INTO email_events VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "m-stripe",
            "jobs@stripe.com",
            "Application received",
            "Thanks for applying: https://job-boards.greenhouse.io/stripe/jobs/stripe-crash",
            "Stripe",
            "Analyst",
            None,
            "acknowledged",
            "2026-07-01T00:00:00+00:00",
        ),
    )
    conn.commit()
    conn.close()


def test_repair_report_summarizes_crashes_email_queue_and_recommendations(fleet_db, tmp_path):
    home_db = tmp_path / "home.db"
    _mk_home_db(home_db)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_apply_row(
            cur,
            url="stripe-crash",
            company="Stripe",
            title="Analyst",
            dedup_key="dk-stripe",
            apply_domain="job-boards.greenhouse.io",
            application_url="https://job-boards.greenhouse.io/stripe/jobs/stripe-crash",
            apply_error="failed:no_result_line",
            worker_id="w-stripe",
            agent_model="claude-sonnet-4",
            duration_ms=4321,
        )
        _seed_apply_row(cur, url="timeout-crash", apply_error="failed:timeout", worker_id="w-timeout")
        _seed_apply_row(cur, url="watchdog-crash", apply_error="crash_unconfirmed", worker_id="w-watchdog")
        _seed_apply_row(
            cur,
            url="blocked-queued",
            status="queued",
            company="HiringCafe",
            dedup_key="dk-hiringcafe",
            apply_error=None,
            worker_id=None,
        )
        cur.execute("INSERT INTO applied_set (dedup_key, company) VALUES ('dk-hiringcafe','HiringCafe')")
        cur.execute(
            "INSERT INTO worker_heartbeat (worker_id, role, state, recent_log) "
            "VALUES ('w-timeout','apply','idle','noise\nRESULT:FAILED:timeout\nmore')"
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        report = repair_report.build_report(conn, home_db_path=str(home_db), sample_limit=3)

    assert report["queue"]["status_counts"]["crash_unconfirmed"] == 3
    assert report["crash"]["total"] == 3
    assert report["crash"]["buckets"]["no_result_line"]["count"] == 1
    assert report["crash"]["buckets"]["timeout"]["count"] == 1
    assert report["crash"]["buckets"]["watchdog_reclaim"]["count"] == 1
    timeout_sample = report["crash"]["buckets"]["timeout"]["samples"][0]
    assert timeout_sample["worker_id"] == "w-timeout"
    assert timeout_sample["agent_model"] == "sonnet"
    assert timeout_sample["apply_duration_ms"] == 1234
    assert timeout_sample["last_result_line"] == "RESULT:FAILED:timeout"
    assert report["email_reconcile"]["confirmed"] == 1
    assert report["queue_diagnosis"]["queued"]["dedup_blocked"] == 1
    assert any("--apply --confirmed-only --max-flips 1" in r["command"] for r in report["recommendations"])
    assert any("applypilot-fleet-dedup-repair" in r["command"] for r in report["recommendations"])
    assert repair_report.classify_crash_error("email_reconcile_review_required") == "email_review_required"


class _Conn:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_repair_report_cli_prints_json(monkeypatch, capsys):
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn=None: _Conn())
    monkeypatch.setattr(
        repair_report,
        "build_report",
        lambda conn, **kw: {
            "queue": {"status_counts": {"crash_unconfirmed": 1}},
            "crash": {"total": 1, "buckets": {}},
            "email_reconcile": {"confirmed": 0, "probable": 0, "jobs_total": 1},
            "queue_diagnosis": {"queued": {"base_leaseable": 0}},
            "recommendations": [],
        },
    )

    rc = repair_report_main.main(["--dsn", "pg", "--home-db", "home.db", "--json"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["crash"]["total"] == 1
