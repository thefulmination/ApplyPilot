from __future__ import annotations

import sqlite3

from applypilot.apply import pgqueue
from applypilot.fleet.historical_apply_reaudit import reaudit_applied_outcomes


def _seed_applied(dsn: str, *, url: str, log_path: str | None, event_id_status: str = "applied") -> int:
    with pgqueue.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, application_url, score, status, apply_status, applied_at) "
                "VALUES (%s,%s,9.0,'applied','applied',now())",
                (url, url),
            )
            cur.execute(
                "INSERT INTO apply_result_events "
                "(url, status, apply_status, job_log_path, result_line, source) "
                "VALUES (%s,%s,%s,%s,'RESULT:APPLIED','worker') RETURNING id",
                (url, event_id_status, event_id_status, log_path),
            )
            event_id = cur.fetchone()["id"]
        conn.commit()
    return event_id


def _home_db(path, confirmed_url: str | None = None):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE email_events (message_id TEXT, job_url TEXT, stage TEXT, match_status TEXT)"
    )
    if confirmed_url:
        conn.execute(
            "INSERT INTO email_events VALUES ('m1', ?, 'acknowledged', 'attributed')",
            (confirmed_url,),
        )
    conn.commit()
    conn.close()


def test_dry_run_reports_contradicted_applied_without_mutation(fleet_db, tmp_path):
    url = "https://example.com/jobs/false-applied"
    event_id = _seed_applied(fleet_db, url=url, log_path="remote.log")
    home = tmp_path / "brain.db"
    _home_db(home)

    report = reaudit_applied_outcomes(
        pgqueue.connect(fleet_db),
        home_db_path=str(home),
        transcript_loader=lambda _: "Never claim RESULT:APPLIED in prose.\nRESULT:FAILED:budget_exhausted\n",
    )

    assert report["dry_run"] is True
    assert report["counts"]["correction_candidate"] == 1
    assert report["rows"][0]["event_id"] == event_id
    assert report["rows"][0]["parsed_status"] == "failed:budget_exhausted"
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status FROM apply_queue WHERE url=%s", (url,))
            assert cur.fetchone()["status"] == "applied"


def test_strict_log_or_email_confirmation_preserves_applied(fleet_db, tmp_path):
    log_url = "https://example.com/jobs/log-confirmed"
    email_url = "https://example.com/jobs/email-confirmed"
    _seed_applied(fleet_db, url=log_url, log_path="applied.log")
    _seed_applied(fleet_db, url=email_url, log_path="failed.log")
    home = tmp_path / "brain.db"
    _home_db(home, email_url)

    transcripts = {
        "applied.log": "RESULT:APPLIED\n",
        "failed.log": "RESULT:FAILED:no_confirmation\n",
    }
    with pgqueue.connect(fleet_db) as conn:
        report = reaudit_applied_outcomes(
            conn,
            home_db_path=str(home),
            transcript_loader=transcripts.__getitem__,
        )

    by_url = {row["url"]: row for row in report["rows"]}
    assert by_url[log_url]["classification"] == "verified_log"
    assert by_url[email_url]["classification"] == "verified_email"
    assert by_url[email_url]["evidence_conflict"] is True


def test_apply_demotes_but_retains_duplicate_guard_and_is_idempotent(fleet_db, tmp_path):
    url = "https://example.com/jobs/repair"
    event_id = _seed_applied(fleet_db, url=url, log_path="failed.log")
    home = tmp_path / "brain.db"
    _home_db(home)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company, applied_url) VALUES ('repair-key','Acme',%s)",
                (url,),
            )
        conn.commit()

        report = reaudit_applied_outcomes(
            conn,
            home_db_path=str(home),
            transcript_loader=lambda _: "RESULT:FAILED:budget_exhausted",
            apply=True,
        )
        assert report["counts"]["corrected"] == 1

        with conn.cursor() as cur:
            cur.execute("SELECT status, apply_status, apply_error, applied_at FROM apply_queue WHERE url=%s", (url,))
            row = cur.fetchone()
            assert row["status"] == "crash_unconfirmed"
            assert row["apply_status"] == "crash_unconfirmed"
            assert row["apply_error"] == "historical_reaudit:failed:budget_exhausted"
            assert row["applied_at"] is None
            cur.execute("SELECT count(*) FROM applied_set WHERE applied_url=%s", (url,))
            assert cur.fetchone()["count"] == 1
            cur.execute(
                "SELECT count(*) FROM apply_result_events WHERE source=%s",
                (f"historical_reaudit:{event_id}",),
            )
            assert cur.fetchone()["count"] == 1

        again = reaudit_applied_outcomes(
            conn,
            home_db_path=str(home),
            transcript_loader=lambda _: "RESULT:FAILED:budget_exhausted",
            apply=True,
        )
        assert again["counts"].get("corrected", 0) == 0


def test_missing_log_is_review_only(fleet_db, tmp_path):
    url = "https://example.com/jobs/missing"
    _seed_applied(fleet_db, url=url, log_path=None)
    home = tmp_path / "brain.db"
    _home_db(home)
    with pgqueue.connect(fleet_db) as conn:
        report = reaudit_applied_outcomes(conn, home_db_path=str(home), apply=True)
    assert report["rows"][0]["classification"] == "review_missing_evidence"
    assert report["counts"].get("corrected", 0) == 0


def test_digest_mismatch_cannot_drive_correction(fleet_db, tmp_path):
    url = "https://example.com/jobs/wrong-machine-log"
    event_id = _seed_applied(fleet_db, url=url, log_path="colliding.log")
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_result_events SET transcript_digest='sha256:not-the-log' WHERE id=%s",
                (event_id,),
            )
        conn.commit()
        report = reaudit_applied_outcomes(
            conn,
            home_db_path=str(tmp_path / "missing.db"),
            transcript_loader=lambda _: "RESULT:FAILED:budget_exhausted",
            apply=True,
        )
    assert report["rows"][0]["classification"] == "review_missing_evidence"
    assert report["rows"][0]["log_error"] == "transcript_digest_mismatch"
    assert report["counts"].get("corrected", 0) == 0
