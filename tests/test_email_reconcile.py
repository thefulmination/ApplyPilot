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
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, rows):
        self._cur = _FakeCursor(rows)

    def cursor(self):
        return self._cur


def test_load_crash_jobs_shapes_candidates_for_matcher():
    rows = [{"url": "https://stripe.com/jobs/1", "application_url": "https://boards.greenhouse.io/stripe/jobs/1",
             "company": "Stripe", "title": "Analyst", "apply_domain": "boards.greenhouse.io",
             "dedup_key": "k-stripe-analyst"}]
    jobs = er.load_crash_jobs(_FakeConn(rows))
    assert jobs[0]["site"] == "boards.greenhouse.io"       # apply_domain -> site
    assert jobs[0]["company"] == "Stripe" and jobs[0]["url"] == "https://stripe.com/jobs/1"
    assert jobs[0]["dedup_key"] == "k-stripe-analyst"


def test_load_crash_jobs_filters_no_result_line_bucket():
    fc = _FakeConn([])
    er.load_crash_jobs(fc)
    sql = fc._cur.executed[0][0].replace(" ", "")
    assert "status='crash_unconfirmed'" in sql
    assert "failed:no_result_line" in sql


# ---------------------------------------------------------------------------
# Task 4: reconcile()
# ---------------------------------------------------------------------------
def _email(**kw):
    base = dict(message_id="m", sender="", subject="", body="", company="", title="",
                job_url=None, stage="acknowledged", occurred_at="2026-06-29T10:00:00+00:00")
    base.update(kw)
    return er.OutcomeEmail(**base)


def test_reconcile_company_domain_is_confirmed():
    jobs = [{"url": "https://stripe.com/jobs/1", "application_url": "", "company": "Stripe",
             "title": "Analyst", "site": "stripe.com"}]
    emails = [_email(message_id="m1", sender="jobs@stripe.com", subject="Application received")]
    res = er.reconcile(emails, jobs)
    assert len(res.confirmed) == 1
    r = res.confirmed[0]
    assert r.job_url == "https://stripe.com/jobs/1" and r.method == "company_domain" and r.classification == "confirmed"


def test_reconcile_no_overlap_is_unmatched():
    jobs = [{"url": "https://stripe.com/jobs/1", "application_url": "", "company": "Stripe",
             "title": "Analyst", "site": "stripe.com"}]
    emails = [_email(message_id="m9", sender="news@randombrand.com", subject="Weekly digest", body="sale")]
    res = er.reconcile(emails, jobs)
    assert res.confirmed == [] and res.probable == [] and res.unmatched_emails == 1


def test_reconcile_resolves_each_job_once():
    # Two emails both match the same job; it must resolve to exactly one Resolution (dedupe).
    jobs = [{"url": "https://stripe.com/jobs/1", "application_url": "", "company": "Stripe",
             "title": "Analyst", "site": "stripe.com"}]
    emails = [
        _email(message_id="a", sender="jobs@stripe.com", subject="Application received"),
        _email(message_id="b", sender="careers@stripe.com", subject="We got your application"),
    ]
    res = er.reconcile(emails, jobs)
    all_urls = [r.job_url for r in res.confirmed + res.probable]
    assert all_urls.count("https://stripe.com/jobs/1") == 1   # deduped to one resolution
    assert len(res.confirmed) == 1


# ---------------------------------------------------------------------------
# Task 5: apply_resolutions
# ---------------------------------------------------------------------------
class _ScriptCursor:
    def __init__(self, rowcounts):
        self._rc = list(rowcounts)
        self.executed = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.strip().upper().startswith("UPDATE"):
            self.rowcount = self._rc.pop(0) if self._rc else 0


class _ScriptConn:
    def __init__(self, rowcounts):
        self._cur = _ScriptCursor(rowcounts)
        self.commits = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1


def _res(confirmed=(), probable=()):
    return er.ReconcileResult(confirmed=list(confirmed), probable=list(probable),
                              unmatched_emails=0, jobs_total=0)


def _r(url, cls="confirmed"):
    return er.Resolution(job_url=url, message_id="m", method="company_domain", score=1.0,
                         stage="acknowledged", occurred_at="2026-06-29T10:00:00+00:00", classification=cls)


def test_apply_resolutions_flips_confirmed_and_audits():
    conn = _ScriptConn(rowcounts=[1])               # UPDATE affects 1 row
    counts = er.apply_resolutions(conn, _res(confirmed=[_r("u1")]))
    assert counts == {"flipped": 1, "skipped": 0}
    updates = [e for e in conn._cur.executed if e[0].strip().upper().startswith("UPDATE apply_queue".upper())]
    inserts = [e for e in conn._cur.executed if "INSERT INTO email_reconcile_actions" in e[0]]
    assert len(updates) == 1 and len(inserts) == 1
    assert "status='applied'" in updates[0][0].replace(" ", "") or "status = 'applied'" in updates[0][0]
    assert "status='crash_unconfirmed'" in updates[0][0].replace(" ", "")   # guarded


def test_apply_resolutions_skips_when_row_already_moved():
    conn = _ScriptConn(rowcounts=[0])               # UPDATE affects 0 rows (already not crash)
    counts = er.apply_resolutions(conn, _res(confirmed=[_r("u1")]))
    assert counts == {"flipped": 0, "skipped": 1}
    assert not any("INSERT INTO email_reconcile_actions" in e[0] for e in conn._cur.executed)


def test_apply_resolutions_excludes_probable_by_default():
    conn = _ScriptConn(rowcounts=[1])
    counts = er.apply_resolutions(conn, _res(probable=[_r("u2", cls="probable")]))
    assert counts == {"flipped": 0, "skipped": 0}    # probable not applied unless included
    assert not any(e[0].strip().upper().startswith("UPDATE") for e in conn._cur.executed)


def test_apply_resolutions_includes_probable_when_opted_in():
    conn = _ScriptConn(rowcounts=[1])
    counts = er.apply_resolutions(conn, _res(probable=[_r("u2", cls="probable")]), include_probable=True)
    assert counts == {"flipped": 1, "skipped": 0}


# ---------------------------------------------------------------------------
# Task 6: format_report
# ---------------------------------------------------------------------------
def test_format_report_summarizes_counts():
    res = er.ReconcileResult(confirmed=[_r("u1")], probable=[_r("u2", cls="probable")],
                             unmatched_emails=5, jobs_total=480)
    text = er.format_report(res)
    assert "confirmed: 1" in text.lower()
    assert "probable: 1" in text.lower()
    assert "480" in text            # jobs_total surfaced


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


# ---------------------------------------------------------------------------
# Fix 3: end-to-end reconcile — fuzzy below threshold yields probable
# ---------------------------------------------------------------------------
def test_reconcile_fuzzy_below_threshold_is_probable():
    # Sender is on a generic domain so company_domain strong path does NOT fire.
    # Subject contains only "Acme" (1 of 3 tokens in "Acme Robotics Incorporated"),
    # giving company_name score 1/3 ≈ 0.33 — in [0.25, 0.6) → probable.
    jobs = [{"url": "https://x.io/1", "application_url": "",
             "company": "Acme Robotics Incorporated", "title": "SWE Senior Engineer", "site": "x.io"}]
    emails = [_email(message_id="m-fuzzy", sender="hr@somemail.com",
                     subject="Update on your application to Acme")]
    res = er.reconcile(emails, jobs)
    assert res.confirmed == [], "expected zero confirmed"
    assert len(res.probable) == 1, "expected exactly one probable"
    r = res.probable[0]
    assert r.method == "company_name"
    assert 0.25 <= r.score < 0.6
    assert r.classification == "probable"
