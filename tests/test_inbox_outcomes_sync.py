import sqlite3

import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import sync


def _brain(tmp_path):
    sq = sqlite3.connect(tmp_path / "brain.db")
    sq.row_factory = sqlite3.Row
    sq.execute("""CREATE TABLE email_events(message_id TEXT PRIMARY KEY, thread_id TEXT, job_url TEXT,
        occurred_at TEXT, sender TEXT, sender_domain TEXT, subject TEXT, stage TEXT, outcome TEXT,
        reason TEXT, title TEXT, company TEXT, match_method TEXT, match_score REAL, confidence TEXT,
        body_text TEXT, snippet TEXT, extracted_by TEXT, scanned_at TEXT)""")
    sq.execute("INSERT INTO email_events(message_id, job_url, company, title, stage, outcome, "
               "sender_domain, confidence, occurred_at) VALUES(?,?,?,?,?,?,?,?,?)",
               ("m1", "https://acme/1", "Acme", "Quant", "rejected", "rejected", "acme.com", "high",
                "2026-06-10T00:00:00+00:00"))
    sq.commit()
    return sq


def test_push_inbox_outcomes_upserts_idempotently(fleet_db, tmp_path):
    sq = _brain(tmp_path)
    pg = pgqueue.connect(fleet_db)
    try:
        n1 = sync.push_inbox_outcomes(sqlite_conn=sq, pg_conn=pg)
        assert n1 == 1
        with pg.cursor() as cur:
            cur.execute("SELECT message_id, company, outcome, dedup_key FROM inbox_outcomes")
            rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["company"] == "Acme" and rows[0]["outcome"] == "rejected"
        assert rows[0]["dedup_key"]  # derived, non-empty
        # Re-run: idempotent, still one row.
        n2 = sync.push_inbox_outcomes(sqlite_conn=sq, pg_conn=pg)
        with pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM inbox_outcomes")
            assert cur.fetchone()["c"] == 1
        assert n2 == 1  # upsert touched the row again
    finally:
        sq.close(); pg.close()
