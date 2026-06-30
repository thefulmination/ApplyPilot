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
