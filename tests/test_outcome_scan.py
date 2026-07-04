# tests/test_outcome_scan.py
import applypilot.outcome_scan as S
from applypilot import database
from applypilot.mail_source import MailMessage


class FakeClient:
    def __init__(self, reply): self._reply = reply
    def chat(self, messages, **kw): return self._reply


def _seed_applied_job(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, site, application_url, apply_status, applied_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("https://boards.greenhouse.io/acme/jobs/1", "Quant Analyst", "Acme", "Acme",
         "https://boards.greenhouse.io/acme/jobs/1", "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.commit()


def test_build_email_event_matches_job_and_extracts(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_applied_job(conn)
    applied = [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()]
    msg = {
        "message_id": "m1", "thread_id": "t1",
        "subject": "Update on your application to Acme",
        "sender": "Acme Careers <careers@acme.com>",
        "date": "Wed, 03 Jun 2026 10:00:00 +0000",
        "body": "We went with another candidate.",
    }
    reply = '{"stage":"rejected","outcome":"rejected","reason":"chose another candidate","title":"Quant Analyst","company":"Acme","confidence":"high"}'
    row = S.build_email_event(msg, applied, client=FakeClient(reply))
    assert row["message_id"] == "m1"
    assert row["stage"] == "rejected"
    assert row["outcome"] == "rejected"
    assert row["reason"] == "chose another candidate"
    assert row["sender_domain"] == "acme.com"
    assert row["occurred_at"].startswith("2026-06-03")
    assert row["job_url"] == "https://boards.greenhouse.io/acme/jobs/1"


def test_upsert_is_idempotent(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    row = {
        "message_id": "m1", "thread_id": "t1", "job_url": None,
        "occurred_at": "2026-06-03T10:00:00+00:00", "sender": "x@y.com",
        "sender_domain": "y.com", "subject": "s", "stage": "acknowledged",
        "outcome": None, "reason": None, "title": None, "company": None,
        "match_method": None, "match_score": None, "confidence": "low",
        "body_text": "b", "snippet": "b", "extracted_by": "llm",
        "scanned_at": "2026-06-29T00:00:00+00:00",
    }
    assert S.upsert_email_event(conn, row) == "inserted"
    assert S.upsert_email_event(conn, row) == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM email_events").fetchone()[0] == 1
    assert S.upsert_email_event(conn, {**row, "stage": "offer"}, reextract=True) == "updated"
    assert conn.execute("SELECT stage FROM email_events WHERE message_id='m1'").fetchone()[0] == "offer"


def test_scan_outcomes_uses_injected_fetch(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_applied_job(conn)
    monkeypatch.setattr(S, "get_connection", lambda: conn)
    messages = [{
        "message_id": "m1", "thread_id": "t1",
        "subject": "Interview invitation — Quant Analyst at Acme",
        "sender": "careers@acme.com",
        "date": "Wed, 03 Jun 2026 10:00:00 +0000",
        "body": "Please pick a time on the calendly link.",
    }]
    reply = '{"stage":"interview","outcome":null,"reason":null,"title":"Quant Analyst","company":"Acme","confidence":"high"}'
    counts = S.scan_outcomes(client=FakeClient(reply), fetch_messages=lambda: messages, conn=conn)
    assert counts["inserted"] == 1
    counts2 = S.scan_outcomes(client=FakeClient(reply), fetch_messages=lambda: messages, conn=conn)
    assert counts2["skipped"] == 1
    assert counts2["inserted"] == 0


def test_scan_outcomes_concurrent_inserts_all_and_is_idempotent(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_applied_job(conn)
    messages = [{
        "message_id": f"m{i}", "thread_id": "t1",
        "subject": "Update on your application to Acme",
        "sender": "careers@acme.com",
        "date": "Wed, 03 Jun 2026 10:00:00 +0000",
        "body": "We went with another candidate.",
    } for i in range(6)]
    reply = '{"stage":"rejected","outcome":"rejected","reason":"chose another","title":"Quant Analyst","company":"Acme","confidence":"high"}'
    counts = S.scan_outcomes(client=FakeClient(reply), fetch_messages=lambda: messages, conn=conn, concurrency=4)
    assert counts["inserted"] == 6
    assert counts["errors"] == 0
    assert conn.execute("SELECT COUNT(*) FROM email_events").fetchone()[0] == 6
    # Re-run under concurrency: idempotent, nothing duplicated.
    counts2 = S.scan_outcomes(client=FakeClient(reply), fetch_messages=lambda: messages, conn=conn, concurrency=4)
    assert counts2["skipped"] == 6
    assert counts2["inserted"] == 0


def test_scan_persists_quarantine_columns(tmp_path):
    from applypilot import database
    from applypilot.outcome_scan import scan_outcomes
    conn = database.init_db(tmp_path / "brain.db")
    conn.execute(
        "INSERT INTO jobs (url, title, site, apply_status, applied_at) VALUES (?,?,?,?,?)",
        ("https://boards.greenhouse.io/checkr/jobs/1", "Analyst", "Checkr",
         "applied", "2026-06-28T12:00:00+00:00"))
    conn.commit()
    msg = {"message_id": "m1", "thread_id": "t1",
           "subject": "Your application to Checkr",
           "sender": "no-reply@us.greenhouse-mail.io",
           "date": "Sat, 20 Jun 2026 12:00:00 +0000",   # predates the apply
           "body": "Thank you for applying to Checkr."}
    counts = scan_outcomes(conn=conn, fetch_messages=lambda: [msg], client=None, concurrency=1)
    assert counts["needs_review"] == 1
    row = conn.execute("SELECT job_url, match_status, match_reason FROM email_events WHERE message_id='m1'").fetchone()
    assert row["job_url"] is None
    assert row["match_status"] == "needs_review"
    assert row["match_reason"] == "predates_application"


class FakeMailSource:
    """Stand-in for mail_source.MailSource: returns a fixed list of MailMessage."""
    def __init__(self, messages): self._messages = messages
    def fetch(self, *, since_days, max_messages): return self._messages


def test_gmail_fetch_default_maps_mail_source_messages(monkeypatch):
    """_gmail_fetch's default thunk routes through get_mail_source() and maps
    MailMessage -> the {message_id,thread_id,subject,sender,date,body} dict shape."""
    mail_msg = MailMessage(
        id="m1", thread_id="t1",
        subject="Update on your application to Acme",
        sender="Acme Careers <careers@acme.com>",
        date="Wed, 03 Jun 2026 10:00:00 +0000",
        body="We went with another candidate.",
    )
    fake_source = FakeMailSource([mail_msg])
    monkeypatch.setattr(
        "applypilot.mail_source.get_mail_source", lambda: fake_source
    )

    fetch = S._gmail_fetch(30, None, max_messages=200)
    out = fetch()

    assert out == [{
        "message_id": "m1",
        "thread_id": "t1",
        "subject": "Update on your application to Acme",
        "sender": "Acme Careers <careers@acme.com>",
        "date": "Wed, 03 Jun 2026 10:00:00 +0000",
        "body": "We went with another candidate.",
    }]


def test_scan_outcomes_default_fetch_routes_through_mail_source(tmp_path, monkeypatch):
    """End-to-end: scan_outcomes with NO fetch_messages injection (the default
    path) still classifies + persists a message coming from get_mail_source()."""
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_applied_job(conn)
    monkeypatch.setattr(S, "get_connection", lambda: conn)

    mail_msg = MailMessage(
        id="m-rej-1", thread_id="t-rej-1",
        subject="Update on your application to Acme",
        sender="Acme Careers <careers@acme.com>",
        date="Wed, 03 Jun 2026 10:00:00 +0000",
        body="We went with another candidate.",
    )
    fake_source = FakeMailSource([mail_msg])
    monkeypatch.setattr(
        "applypilot.mail_source.get_mail_source", lambda: fake_source
    )

    reply = '{"stage":"rejected","outcome":"rejected","reason":"chose another candidate","title":"Quant Analyst","company":"Acme","confidence":"high"}'
    counts = S.scan_outcomes(client=FakeClient(reply), conn=conn)

    assert counts["inserted"] == 1
    row = conn.execute(
        "SELECT stage, outcome, sender_domain FROM email_events WHERE message_id='m-rej-1'"
    ).fetchone()
    assert row["stage"] == "rejected"
    assert row["outcome"] == "rejected"
    assert row["sender_domain"] == "acme.com"


def test_position_filled_scan_stamps_liveness_on_attributed_match(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_applied_job(conn)
    row_msg = {
        "message_id": "filled-1", "thread_id": "t-filled",
        "subject": "Position no longer available",
        "sender": "careers@acme.com",
        "date": "Wed, 03 Jun 2026 10:00:00 +0000",
        "body": "This role has been filled.",
    }
    reply = '{"stage":"position_filled","outcome":"position_filled","reason":"role closed","title":"Quant Analyst","company":"Acme","confidence":"high"}'
    counts = S.scan_outcomes(client=FakeClient(reply), fetch_messages=lambda: [row_msg], conn=conn)
    assert counts["inserted"] == 1
    job = conn.execute("SELECT liveness_status, liveness_reason FROM jobs WHERE url='https://boards.greenhouse.io/acme/jobs/1'").fetchone()
    assert job["liveness_status"] == "dead"
    assert job["liveness_reason"] == "email_position_filled"


def test_position_filled_scan_does_not_stamp_if_unmatched_or_needs_review(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_applied_job(conn)
    mismatch_msg = {
        "message_id": "filled-2", "thread_id": "t-filled",
        "subject": "Update on your application",
        "sender": "other@other.com",
        "date": "Wed, 03 Jun 2026 10:00:00 +0000",
        "body": "This position has been filled.",
    }
    reply = '{"stage":"position_filled","outcome":"position_filled","reason":"role closed","title":null,"company":null,"confidence":"high"}'
    # needs_review path from temporal guard:
    stale_msg = {
        "message_id": "filled-3", "thread_id": "t-filled",
        "subject": "Update on your application to Acme",
        "sender": "careers@acme.com",
        "date": "Sat, 20 Jun 2026 12:00:00 +0000",
        "body": "Position no longer available.",
    }
    conn.execute(
        "UPDATE jobs SET applied_at='2026-06-28T12:00:00+00:00' WHERE url='https://boards.greenhouse.io/acme/jobs/1'"
    )
    conn.commit()
    counts = S.scan_outcomes(client=FakeClient(reply), fetch_messages=lambda: [mismatch_msg, stale_msg], conn=conn, concurrency=1)
    assert counts["inserted"] == 2
    assert counts["needs_review"] == 1
    job = conn.execute("SELECT liveness_status, liveness_reason FROM jobs WHERE url='https://boards.greenhouse.io/acme/jobs/1'").fetchone()
    assert job["liveness_status"] is None
    assert job["liveness_reason"] is None
