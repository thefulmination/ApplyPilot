from applypilot.fleet import email_reconcile as er
import sqlite3


def _mk_home_db():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE email_events (message_id TEXT PRIMARY KEY, sender TEXT, subject TEXT,
                 body_text TEXT, company TEXT, title TEXT, job_url TEXT, stage TEXT NOT NULL,
                 occurred_at TEXT)""")
    rows = [
        ("m1", "jobs@stripe.com", "Application received", "thanks", "Stripe", "Analyst",
         "https://stripe.com/jobs/1", "acknowledged", "2026-06-29T10:00:00+00:00"),
        ("m2", "no-reply@x.com", "Unsubscribe", "promo", "", "", None, "other", "2026-06-29T11:00:00+00:00"),
        ("m3", "talent@acme.com", "Update on your application", "regret", "Acme", "Engineer",
         "https://acme.com/careers/9", "rejected", "2026-06-29T12:00:00+00:00"),
    ]
    c.executemany("INSERT INTO email_events VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return c


def test_load_outcome_emails_keeps_only_confirming_stages():
    emails = er.load_outcome_emails(_mk_home_db())
    ids = {e.message_id for e in emails}
    assert ids == {"m1", "m3"}          # "other" dropped
    m1 = next(e for e in emails if e.message_id == "m1")
    assert m1.company == "Stripe" and m1.stage == "acknowledged"


# ---------------------------------------------------------------------------
# Task 3: load_crash_jobs
# ---------------------------------------------------------------------------
class _FakeCursor:
    def __init__(self, rows): self._rows = rows; self.executed = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.executed.append((sql, params))
    def fetchall(self): return self._rows


class _FakeConn:
    def __init__(self, rows): self._cur = _FakeCursor(rows)
    def cursor(self): return self._cur


def test_load_crash_jobs_shapes_candidates_for_matcher():
    rows = [{"url": "https://stripe.com/jobs/1", "application_url": "https://boards.greenhouse.io/stripe/jobs/1",
             "company": "Stripe", "title": "Analyst", "apply_domain": "boards.greenhouse.io"}]
    jobs = er.load_crash_jobs(_FakeConn(rows))
    assert jobs[0]["site"] == "boards.greenhouse.io"       # apply_domain -> site
    assert jobs[0]["company"] == "Stripe" and jobs[0]["url"] == "https://stripe.com/jobs/1"


def test_load_crash_jobs_filters_no_result_line_bucket():
    fc = _FakeConn([])
    er.load_crash_jobs(fc)
    sql = fc._cur.executed[0][0].replace(" ", "")
    assert "status='crash_unconfirmed'" in sql
    assert "failed:no_result_line" in sql


def test_classify_strong_method_is_confirmed_regardless_of_score():
    assert er.classify_match("company_domain", 1.0) == "confirmed"
    assert er.classify_match("board_slug", 1.0) == "confirmed"
    assert er.classify_match("linkedin_job_id", 1.0) == "confirmed"


def test_classify_fuzzy_at_or_above_threshold_is_confirmed():
    assert er.classify_match("company_name", 0.6) == "confirmed"
    assert er.classify_match("title", 0.75) == "confirmed"


def test_classify_fuzzy_below_threshold_is_probable():
    assert er.classify_match("company_name", 0.59) == "probable"
    assert er.classify_match("ats_domain", 0.25) == "probable"


def test_classify_no_match_is_none():
    assert er.classify_match(None, None) is None
