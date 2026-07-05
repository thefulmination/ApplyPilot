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
             "dedup_key": "k-stripe-analyst", "updated_at": "2026-06-29T12:00:00+00:00"}]
    jobs = er.load_crash_jobs(_FakeConn(rows))
    assert jobs[0]["site"] == "boards.greenhouse.io"       # apply_domain -> site
    assert jobs[0]["company"] == "Stripe" and jobs[0]["url"] == "https://stripe.com/jobs/1"
    assert jobs[0]["dedup_key"] == "k-stripe-analyst"
    assert jobs[0]["guard_after"] == "2026-06-29T12:00:00+00:00"   # updated_at -> guard_after


def test_load_crash_jobs_includes_all_crash_unconfirmed_buckets():
    fc = _FakeConn([])
    er.load_crash_jobs(fc)
    sql = fc._cur.executed[0][0].replace(" ", "")
    assert "status='crash_unconfirmed'" in sql
    assert "failed:no_result_line" not in sql


def test_load_crash_jobs_accepts_limit():
    fc = _FakeConn([])
    er.load_crash_jobs(fc, limit=25)
    sql, params = fc._cur.executed[0]
    assert "LIMIT %s" in sql
    assert params == (25,)


# ---------------------------------------------------------------------------
# Task 4: reconcile()
# ---------------------------------------------------------------------------
def _email(**kw):
    base = dict(message_id="m", sender="", subject="", body="", company="", title="",
                job_url=None, stage="acknowledged", occurred_at="2026-06-29T10:00:00+00:00")
    base.update(kw)
    return er.OutcomeEmail(**base)


def test_reconcile_company_domain_is_probable():
    jobs = [{"url": "https://stripe.com/jobs/1", "application_url": "", "company": "Stripe",
             "title": "Analyst", "site": "stripe.com"}]
    emails = [_email(message_id="m1", sender="jobs@stripe.com", subject="Application received")]
    res = er.reconcile(emails, jobs)
    assert res.confirmed == []
    r = res.probable[0]
    assert r.job_url == "https://stripe.com/jobs/1" and r.method == "company_domain" and r.classification == "probable"


def test_reconcile_board_slug_is_confirmed():
    jobs = [{"url": "https://job-boards.greenhouse.io/stripe/jobs/1",
             "application_url": "https://job-boards.greenhouse.io/stripe/jobs/1",
             "company": "Stripe", "title": "Analyst", "site": "job-boards.greenhouse.io"}]
    emails = [_email(message_id="m1", sender="no-reply@greenhouse-mail.io",
                     subject="Application received",
                     body="Thanks for applying: https://job-boards.greenhouse.io/stripe/jobs/1")]
    res = er.reconcile(emails, jobs)
    assert len(res.confirmed) == 1
    assert res.confirmed[0].method == "board_slug"


def test_reconcile_no_overlap_is_unmatched():
    jobs = [{"url": "https://stripe.com/jobs/1", "application_url": "", "company": "Stripe",
             "title": "Analyst", "site": "stripe.com"}]
    emails = [_email(message_id="m9", sender="news@randombrand.com", subject="Weekly digest", body="sale")]
    res = er.reconcile(emails, jobs)
    assert res.confirmed == [] and res.probable == [] and res.unmatched_emails == 1


def test_reconcile_rejects_probable_when_email_company_disagrees_with_job_company():
    jobs = [{"url": "https://hiring.cafe/viewjob/warp", "application_url": "https://jobs.ashbyhq.com/warp/1",
             "company": "Warp", "title": "Business Operations", "site": "jobs.ashbyhq.com"}]
    emails = [_email(
        message_id="m-factory",
        sender="Factory Hiring Team <no-reply@ashbyhq.com>",
        subject="Factory Application - Jonathan Stallone",
        body="Thank you for applying to the Business Operations role at Factory.",
        company="Factory",
        title="Business Operations",
    )]

    res = er.reconcile(emails, jobs)

    assert res.confirmed == []
    assert res.probable == []
    assert res.unmatched_emails == 1


def test_reconcile_keeps_probable_when_email_company_agrees_but_title_differs():
    jobs = [{"url": "https://meta.example/jobs/1", "application_url": "https://www.metacareers.com/jobs/1",
             "company": "Meta", "title": "Strategic Partnerships Data Center Power Lead",
             "site": "www.metacareers.com"}]
    emails = [_email(
        message_id="m-meta",
        sender="careers@meta.com",
        subject="Your Application to Meta",
        body="We received your application for the Business Operations Manager role.",
        company="Meta",
        title="Business Operations Manager",
    )]

    res = er.reconcile(emails, jobs)

    assert res.confirmed == []
    assert len(res.probable) == 1
    assert res.probable[0].job_url == "https://meta.example/jobs/1"


def test_reconcile_promotes_exact_company_and_title_evidence_to_confirmed():
    jobs = [{"url": "https://www.indeed.com/viewjob?jk=anduril", "application_url": "https://grnh.se/anduril",
             "company": "Anduril", "title": "Talent Acquisition Operations Manager, Analytics",
             "site": "grnh.se"}]
    emails = [_email(
        message_id="m-anduril",
        sender="no-reply@anduril.com",
        subject="Your Application to Anduril",
        body="We received your application for the Talent Acquisition Operations Manager, Analytics role.",
        company="Anduril",
        title="Talent Acquisition Operations Manager, Analytics",
    )]

    res = er.reconcile(emails, jobs)

    assert len(res.confirmed) == 1
    assert res.confirmed[0].job_url == "https://www.indeed.com/viewjob?jk=anduril"
    assert res.probable == []


def test_reconcile_rejects_generic_company_token_only_overlap():
    jobs = [{"url": "https://hiring.cafe/viewjob/luma", "application_url": "https://jobs.gem.com/lumalabs-ai/1",
             "company": "Luma AI", "title": "Revenue Operations Lead", "site": "jobs.gem.com"}]
    emails = [_email(
        message_id="m-featherless",
        sender="Featherless AI Hiring Team <no-reply@ashbyhq.com>",
        subject="Thanks for applying to Featherless AI!",
        body="Thank you for applying for the Founding Business Development Rep role at Featherless AI.",
        company="Featherless AI",
        title="Founding Business Development Rep",
    )]

    res = er.reconcile(emails, jobs)

    assert res.confirmed == []
    assert res.probable == []
    assert res.unmatched_emails == 1


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
    assert len(res.probable) == 1


def test_reconcile_skips_consumed_message_ids():
    jobs = [{"url": "https://stripe.com/jobs/1", "application_url": "", "company": "Stripe",
             "title": "Analyst", "site": "stripe.com"}]
    emails = [_email(message_id="m-consumed", sender="jobs@stripe.com", subject="Application received")]
    res = er.reconcile(emails, jobs, consumed_message_ids={"m-consumed"})
    assert res.confirmed == []
    assert res.probable == []
    assert res.unmatched_emails == 0


def test_reconcile_uses_message_id_once_within_run():
    jobs = [{"url": "https://stripe.com/jobs/1", "application_url": "", "company": "Stripe",
             "title": "Analyst", "site": "stripe.com"}]
    emails = [
        _email(message_id="m-dupe", sender="jobs@stripe.com", subject="Application received"),
        _email(message_id="m-dupe", sender="jobs@stripe.com", subject="Application received"),
    ]
    res = er.reconcile(emails, jobs)
    assert len(res.probable) == 1


# ---------------------------------------------------------------------------
# Task 5: apply_resolutions
# ---------------------------------------------------------------------------
class _ScriptCursor:
    def __init__(self, rowcounts, rows=None):
        self._rc = list(rowcounts)
        self._rows = list(rows or [])
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

    def fetchall(self):
        return self._rows


class _ScriptConn:
    def __init__(self, rowcounts, rows=None):
        self._cur = _ScriptCursor(rowcounts, rows=rows)
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


def test_apply_resolutions_skips_consumed_message_id():
    conn = _ScriptConn(rowcounts=[1], rows=[{"message_id": "m"}])
    counts = er.apply_resolutions(conn, _res(confirmed=[_r("u1")]))
    assert counts == {"flipped": 0, "skipped": 1}
    assert not any(e[0].strip().upper().startswith("UPDATE APPLY_QUEUE") for e in conn._cur.executed)
    assert conn.commits == 0


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
    conn = _ScriptConn(rowcounts=[1, 1])
    counts = er.apply_resolutions(conn, _res(probable=[_r("u2", cls="probable")]), include_probable=True)
    assert counts == {"flipped": 1, "skipped": 0}


def test_apply_resolutions_stops_at_max_flips():
    conn = _ScriptConn(rowcounts=[1, 1])
    counts = er.apply_resolutions(
        conn,
        _res(confirmed=[_r("u1"), _r("u2")]),
        max_flips=1,
    )
    assert counts == {"flipped": 1, "skipped": 0}
    apply_updates = [
        e for e in conn._cur.executed
        if e[0].strip().upper().startswith("UPDATE APPLY_QUEUE")
    ]
    assert len(apply_updates) == 1


# ---------------------------------------------------------------------------
# Phase 2.3: apply_resolutions sets applied_set.got_response on a flip
# ---------------------------------------------------------------------------
def test_apply_resolutions_sets_got_response_on_flip():
    # rowcounts: [0]=apply_queue flip UPDATE hits 1 row, [1]=applied_set got_response UPDATE hits 1 row
    conn = _ScriptConn(rowcounts=[1, 1])
    counts = er.apply_resolutions(conn, _res(confirmed=[_r("u1")]))
    assert counts == {"flipped": 1, "skipped": 0}
    got_response_updates = [
        e for e in conn._cur.executed
        if e[0].strip().upper().startswith("UPDATE APPLIED_SET")
    ]
    assert len(got_response_updates) == 1, "apply_resolutions must UPDATE applied_set.got_response on a flip"
    sql, params = got_response_updates[0]
    assert "got_response" in sql.lower() and "true" in sql.lower()
    # keyed via apply_queue.url -> dedup_key (applied_set has no url column)
    assert "apply_queue" in sql.lower() and "dedup_key" in sql.lower()
    assert params is not None and "u1" in params


def test_apply_resolutions_skipped_row_does_not_touch_got_response():
    # UPDATE apply_queue hits 0 rows (already moved) -> skipped; got_response must NOT be touched.
    conn = _ScriptConn(rowcounts=[0])
    counts = er.apply_resolutions(conn, _res(confirmed=[_r("u1")]))
    assert counts == {"flipped": 0, "skipped": 1}
    assert not any(
        e[0].strip().upper().startswith("UPDATE APPLIED_SET") for e in conn._cur.executed
    ), "skipped rows must not touch applied_set.got_response"


def test_apply_resolutions_got_response_end_to_end_real_pg(fleet_db):
    """Real-Postgres check that the got_response SQL actually runs and is keyed
    correctly through apply_queue.dedup_key -> applied_set.dedup_key (catches SQL
    typos/join errors the fake-conn unit tests above can't catch)."""
    from applypilot.apply import pgqueue
    dk = "stripe::analyst"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain, dedup_key) "
            "VALUES ('u1','http://x','9','crash_unconfirmed','ats','x.com',%s)", (dk,),
        )
        cur.execute(
            "INSERT INTO applied_set (dedup_key, company, applied_url) VALUES (%s,'Stripe','u1')", (dk,),
        )
        # A second, unrelated row must NOT be touched (skip check).
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, apply_domain, dedup_key) "
            "VALUES ('u2','http://y','9','applied','ats','y.com','other::role')",
        )
        cur.execute(
            "INSERT INTO applied_set (dedup_key, company, applied_url) VALUES ('other::role','Other','u2')",
        )
        conn.commit()

    with pgqueue.connect(fleet_db) as conn:
        counts = er.apply_resolutions(conn, _res(confirmed=[_r("u1")]))
    assert counts == {"flipped": 1, "skipped": 0}

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT got_response FROM applied_set WHERE dedup_key=%s", (dk,))
        assert cur.fetchone()["got_response"] is True
        cur.execute("SELECT got_response FROM applied_set WHERE dedup_key='other::role'")
        assert cur.fetchone()["got_response"] is False


# ---------------------------------------------------------------------------
# Fix B: load_outcome_emails graceful missing table
# ---------------------------------------------------------------------------
def test_load_outcome_emails_missing_table_returns_empty():
    conn = sqlite3.connect(":memory:")  # no email_events table
    assert er.load_outcome_emails(conn) == []
    conn.close()


# ---------------------------------------------------------------------------
# Fix D: apply_resolutions rolls back on write failure
# ---------------------------------------------------------------------------
class _FailingCursor(_ScriptCursor):
    """Cursor that raises on INSERT (simulates a failed audit write)."""

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.strip().upper().startswith("UPDATE"):
            self.rowcount = self._rc.pop(0) if self._rc else 0
        elif "INSERT" in sql.upper():
            raise RuntimeError("simulated insert failure")


class _RollbackConn(_ScriptConn):
    def __init__(self, rowcounts):
        self._cur = _FailingCursor(rowcounts)
        self.commits = 0
        self.rolled_back = 0

    def cursor(self):
        return self._cur

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rolled_back += 1


def test_apply_resolutions_rolls_back_on_write_failure():
    import pytest
    conn = _RollbackConn(rowcounts=[1])  # UPDATE hits 1 row, INSERT will raise
    with pytest.raises(RuntimeError, match="simulated insert failure"):
        er.apply_resolutions(conn, _res(confirmed=[_r("u1")]))
    assert conn.rolled_back == 1
    assert conn.commits == 0  # no commit should have happened


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
    assert er.classify_match("board_slug", 1.0) == "confirmed"
    assert er.classify_match("linkedin_job_id", 1.0) == "confirmed"


def test_classify_company_domain_is_review_only():
    assert er.classify_match("company_domain", 1.0) == "probable"


def test_classify_ats_domain_at_or_above_threshold_is_confirmed():
    assert er.classify_match("ats_domain", 0.6) == "confirmed"
    assert er.classify_match("ats_domain", 0.75) == "confirmed"


def test_classify_company_name_and_title_are_always_probable():
    # too collision-prone to auto-flip, regardless of score (review-only via --apply-probable)
    assert er.classify_match("company_name", 1.0) == "probable"
    assert er.classify_match("title", 1.0) == "probable"
    assert er.classify_match("company_name", 0.6) == "probable"


def test_classify_fuzzy_below_threshold_is_probable():
    assert er.classify_match("ats_domain", 0.59) == "probable"
    assert er.classify_match("ats_domain", 0.25) == "probable"


def test_classify_no_match_is_none():
    assert er.classify_match(None, None) is None


# ---------------------------------------------------------------------------
# Fix 3: end-to-end reconcile — fuzzy below threshold yields probable
# ---------------------------------------------------------------------------
def test_email_predating_crash_attempt_cannot_confirm():
    from applypilot.fleet.email_reconcile import reconcile, OutcomeEmail
    emails = [OutcomeEmail(
        message_id="m1", sender="no-reply@us.greenhouse-mail.io",
        subject="Your application to Acme",
        body="Thank you for applying to Acme. See https://boards.greenhouse.io/acme/jobs/1",
        company="Acme", title="Analyst", job_url=None, stage="applied_confirmation",
        occurred_at="2026-06-01T00:00:00+00:00",           # BEFORE the crash attempt
    )]
    jobs = [{"url": "https://boards.greenhouse.io/acme/jobs/1",
             "application_url": "https://boards.greenhouse.io/acme/jobs/1",
             "company": "Acme", "title": "Analyst", "site": "boards.greenhouse.io",
             "dedup_key": "k1", "guard_after": "2026-06-29T12:00:00+00:00"}]
    result = reconcile(emails, jobs)
    assert result.confirmed == [] and result.probable == []
    assert result.unmatched_emails == 1


def test_reconcile_forwards_occurred_at_and_guard_fires_for_the_right_reason(monkeypatch):
    """Pins the Task-4 wiring itself: the black-box test above also passes when
    reconcile() DROPS occurred_at (the matcher then refuses via no_timestamp — same
    unmatched outcome, wrong path). Spy on the matcher to prove reconcile forwards the
    email's occurred_at and the refusal is the TEMPORAL guard, not the missing-timestamp
    fallback."""
    from applypilot.fleet import email_reconcile as mod
    from applypilot.gmail_outcomes import match_email_to_job as real_match

    seen: list[dict] = []

    def spy(sender, subject, body, jobs, **kw):
        r = real_match(sender, subject, body, jobs, **kw)
        seen.append({"occurred_at": kw.get("occurred_at"), "status": r.status, "reason": r.reason})
        return r

    monkeypatch.setattr(mod, "match_email_to_job", spy)
    emails = [mod.OutcomeEmail(
        message_id="m1", sender="no-reply@us.greenhouse-mail.io",
        subject="Your application to Acme",
        body="Thank you for applying to Acme. See https://boards.greenhouse.io/acme/jobs/1",
        company="Acme", title="Analyst", job_url=None, stage="applied_confirmation",
        occurred_at="2026-06-01T00:00:00+00:00",
    )]
    jobs = [{"url": "https://boards.greenhouse.io/acme/jobs/1",
             "application_url": "https://boards.greenhouse.io/acme/jobs/1",
             "company": "Acme", "title": "Analyst", "site": "boards.greenhouse.io",
             "dedup_key": "k1", "guard_after": "2026-06-29T12:00:00+00:00"}]

    result = mod.reconcile(emails, jobs)

    assert result.confirmed == [] and result.unmatched_emails == 1
    assert seen == [{
        "occurred_at": "2026-06-01T00:00:00+00:00",       # reconcile forwarded the email's timestamp
        "status": "needs_review",
        "reason": "predates_application",                  # temporal guard, NOT no_timestamp
    }]


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
