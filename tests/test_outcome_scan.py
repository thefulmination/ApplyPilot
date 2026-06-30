# tests/test_outcome_scan.py
import applypilot.outcome_scan as S
from applypilot import database


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
