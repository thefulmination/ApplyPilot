"""Regression guard against DOUBLE SUBMITS.

A real run double-submitted to the same company because one posting existed as
SEVERAL job rows (e.g. two hiring.cafe listings + a LinkedIn row, all resolving to
the same ATS form), and acquire_job's dedup only excluded rows whose effective
apply-url matched an *applied* row. Applying one row left its siblings acquirable.

These tests pin the strengthened posting-level dedup: a candidate is excluded if the
same posting is already applied, currently in_progress, or was submitted-but-
unconfirmed (no_confirmation) -- matched by effective apply target (incl. the durable
applications ledger) OR by company+title.
"""
from __future__ import annotations

import pytest

from applypilot import database
from applypilot.apply import launcher as L


def _seed(conn, url, *, title="Chief of Staff", company="", application_url=None,
          apply_status=None, apply_error=None, audit=9.0):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, "
        "tailored_resume_path, fit_score, audit_score, apply_status, apply_error, applied_at) "
        "VALUES (?, ?, 'TestCo', ?, ?, 'x', 8, ?, ?, ?, ?)",
        (url, title, company, application_url, audit, apply_status, apply_error,
         "2026-06-24T00:00:00+00:00" if apply_status == "applied" else None),
    )
    conn.commit()


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(L, "get_connection", lambda: c)
    return c


def test_sibling_row_excluded_by_company_and_title(conn):
    # The exact real-world shape: Setpoint as an applied hiring.cafe row + a still-
    # pending LinkedIn row for the same company+role (different url, app_url unresolved).
    _seed(conn, "https://hiring.cafe/viewjob/applied1", company="Setpoint",
          application_url="https://boards.greenhouse.io/setpoint/jobs/1", apply_status="applied")
    _seed(conn, "https://www.linkedin.com/jobs/view/999", company="Setpoint",
          application_url=None, apply_status=None)
    # The LinkedIn sibling must NOT be acquired -- Setpoint was already applied.
    assert L.acquire_job(min_score=7) is None


def test_target_url_acquires_unattempted_null_status_job(conn):
    _seed(conn, "https://www.linkedin.com/jobs/view/4415558624", company="",
          application_url=None, apply_status=None)

    job = L.acquire_job(target_url="https://www.linkedin.com/jobs/view/4415558624", min_score=7)

    assert job is not None
    assert job["url"] == "https://www.linkedin.com/jobs/view/4415558624"


def test_sibling_row_excluded_by_effective_url(conn):
    # Two rows that resolve to the SAME ATS form but have no/empty company.
    _seed(conn, "https://hiring.cafe/viewjob/a", company="",
          application_url="https://jobs.ashbyhq.com/acme/x", apply_status="applied")
    _seed(conn, "https://www.linkedin.com/jobs/view/2", company="",
          application_url="https://jobs.ashbyhq.com/acme/x", apply_status=None)
    assert L.acquire_job(min_score=7) is None


def test_in_progress_blocks_sibling_concurrent_double_submit(conn):
    # A row in flight under worker A must block worker B from grabbing a different row
    # of the SAME posting (the parallel-worker double-submit race).
    _seed(conn, "https://hiring.cafe/viewjob/inflight", company="Picogrid",
          application_url="https://jobs.ashbyhq.com/picogrid/1", apply_status="in_progress")
    _seed(conn, "https://www.linkedin.com/jobs/view/3", company="Picogrid",
          application_url=None, apply_status=None)
    assert L.acquire_job(min_score=7) is None


def test_no_confirmation_blocks_sibling(conn):
    # no_confirmation means the agent DID click submit (just couldn't confirm). A
    # sibling row of that posting must not be applied -> would be a second submission.
    _seed(conn, "https://hiring.cafe/viewjob/unconf", company="Checkr",
          application_url="https://job-boards.greenhouse.io/checkr/1",
          apply_status="failed", apply_error="no_confirmation")
    _seed(conn, "https://www.linkedin.com/jobs/view/4", company="Checkr",
          application_url=None, apply_status=None)
    assert L.acquire_job(min_score=7) is None


def test_applications_ledger_blocks_relisted_after_status_lost(conn):
    # Durable cross-check: even if jobs.apply_status was LOST (e.g. a DB restore from a
    # stale backup), the separate applications ledger still records the apply -> a
    # re-listed row for the same target must not be re-applied.
    conn.execute(
        "INSERT INTO applications (job_url, application_url, status, channel, created_at, updated_at) "
        "VALUES ('https://old/url', 'https://jobs.lever.co/heyjane/1', 'applied', 'applypilot', "
        "'2026-06-24T00:00:00+00:00', '2026-06-24T00:00:00+00:00')"
    )
    _seed(conn, "https://www.linkedin.com/jobs/view/5", company="Hey Jane",
          application_url="https://jobs.lever.co/heyjane/1", apply_status=None)
    assert L.acquire_job(min_score=7) is None


def test_unresolved_aggregator_row_is_deferred(conn):
    # An aggregator row whose apply target is the aggregator's OWN page (the real ATS +
    # company are revealed only at runtime) is deferred at acquire -- never applied -- so
    # it can't double-submit a job already applied elsewhere. (This is the chiefofstaffjob
    # -> Picogrid-via-Ashby double-submit class.)
    _seed(conn, "https://www.chiefofstaffjob.com/jobs/aggx", company="ChiefOfStaffJob.com",
          application_url="https://www.chiefofstaffjob.com/jobs/aggx")
    assert L.acquire_job(min_score=7) is None
    row = conn.execute("SELECT apply_status, apply_error FROM jobs WHERE url LIKE '%/jobs/aggx'").fetchone()
    assert row[0] == "deferred" and row[1] == "aggregator_unresolved_target"


def test_resolved_aggregator_row_is_applyable(conn):
    # Same aggregator source, but enrichment RESOLVED application_url to the real ATS ->
    # effective host is greenhouse (not the aggregator), dedup works, so it's applyable.
    _seed(conn, "https://www.chiefofstaffjob.com/jobs/aggy", company="RealCo",
          application_url="https://boards.greenhouse.io/realco/1")
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"].endswith("/jobs/aggy")


def test_dedup_does_not_over_exclude(conn):
    # A genuinely DIFFERENT role at the same company is still acquirable (the user's
    # rule: different roles are not duplicates). "Chief of Staff" and "VP of Marketing"
    # share no role words -> low similarity -> not a near-dup.
    _seed(conn, "https://hiring.cafe/viewjob/cos", company="BigCo",
          title="Chief of Staff", application_url="https://boards.greenhouse.io/bigco/1",
          apply_status="applied")
    _seed(conn, "https://hiring.cafe/viewjob/vpmkt", company="BigCo",
          title="VP of Marketing",
          application_url="https://boards.greenhouse.io/bigco/2", apply_status=None)
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"].endswith("/vpmkt")


# --- Freshness filter (APPLYPILOT_MAX_JOB_AGE_DAYS) ---------------------------

def _seed_age(conn, url, days_old, *, live=None, company="Acme", title="Chief of Staff"):
    import datetime as _dt
    disc = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(days=days_old)).isoformat()
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, tailored_resume_path, fit_score, "
        "audit_score, discovered_at, liveness_status) VALUES (?, ?, 'X', ?, 'x', 8, 9.0, ?, ?)",
        (url, title, company, disc, live),
    )
    conn.commit()


def test_freshness_filter_off_by_default_keeps_stale(conn):
    # No env set -> filter off -> a 60-day-old posting is still acquirable (unchanged behavior).
    _seed_age(conn, "https://boards.greenhouse.io/a/jobs/1", 60)
    assert L.acquire_job(min_score=7) is not None


def test_freshness_filter_skips_stale_unverified(conn, monkeypatch):
    # With a 45-day bound, a stale posting with no liveness confirmation is skipped --
    # these have a much higher expired-on-visit rate, so we don't waste a launch.
    monkeypatch.setenv("APPLYPILOT_MAX_JOB_AGE_DAYS", "45")
    _seed_age(conn, "https://boards.greenhouse.io/a/jobs/2", 60)
    assert L.acquire_job(min_score=7) is None


def test_freshness_filter_keeps_stale_but_liveness_confirmed(conn, monkeypatch):
    # Age alone doesn't disqualify -- a liveness_status='live' row is kept even if old.
    monkeypatch.setenv("APPLYPILOT_MAX_JOB_AGE_DAYS", "45")
    _seed_age(conn, "https://boards.greenhouse.io/a/jobs/3", 60, live="live")
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"].endswith("/jobs/3")


def test_freshness_filter_keeps_fresh(conn, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_MAX_JOB_AGE_DAYS", "45")
    _seed_age(conn, "https://boards.greenhouse.io/a/jobs/4", 5)
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"].endswith("/jobs/4")


# --- Crash-stranded lease handling (anti double-submit) -----------------------

def test_reclaim_parks_hardkill_strand_not_retryable(conn):
    # A job left 'in_progress' past the stale-lease TTL = a hard-killed worker that may
    # have already submitted. It must be PARKED (crash_unconfirmed, attempts=99), not
    # silently re-offered -- re-applying would risk a double.
    import datetime as _dt
    old = (_dt.datetime.now(_dt.timezone.utc)
           - _dt.timedelta(seconds=L.STALE_LEASE_SECONDS + 60)).isoformat()
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score, apply_status, last_attempted_at) VALUES "
        "('https://hiring.cafe/strand', 'Chief of Staff', 'X', 'Acme', "
        "'https://job-boards.greenhouse.io/acme/jobs/1', 'x', 8, 9.0, 'in_progress', ?)", (old,))
    conn.commit()
    assert L.reclaim_stale_leases() == 1
    r = conn.execute("SELECT apply_status, apply_error, apply_attempts FROM jobs "
                     "WHERE url LIKE '%/strand'").fetchone()
    assert r[0] == "failed" and r[1] == "crash_unconfirmed" and r[2] == 99
    assert L.acquire_job(min_score=7) is None  # not re-acquired


def test_crash_unconfirmed_blocks_sibling(conn):
    # A crash_unconfirmed job (may have submitted) blocks a SIBLING listing of the same
    # role -- so a re-listed posting isn't applied when the original might have gone out.
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, apply_status, "
        "apply_error, apply_attempts) VALUES ('https://hiring.cafe/cu', 'Chief of Staff', "
        "'X', 'Acme', 'https://job-boards.greenhouse.io/acme/jobs/1', 'failed', "
        "'crash_unconfirmed', 99)")
    conn.commit()
    _seed(conn, "https://www.linkedin.com/jobs/view/9", company="Acme", title="Chief of Staff")
    assert L.acquire_job(min_score=7) is None


# --- reset_failed must NOT un-park possibly-submitted jobs (anti double-submit) ---
#
# reclaim_stale_leases parks a hard-killed (maybe-already-submitted) lease as
# failed/crash_unconfirmed; the agent records a submitted-but-unconfirmed apply as
# failed/no_confirmation (no_confirmation = it DID click submit). Both carry
# apply_status='failed', so the --reset-failed utility (reset_failed) used to match
# them and clear apply_status/apply_error/attempts -> the row became re-acquirable ->
# a SECOND application under the user's name. reset_failed must leave them parked,
# mirroring acquire_job's own dedup exclusion of these error codes.

def test_reset_failed_does_not_unpark_crash_unconfirmed(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score, apply_status, apply_error, apply_attempts) VALUES "
        "('https://hiring.cafe/cu', 'Chief of Staff', 'X', 'Acme', "
        "'https://job-boards.greenhouse.io/acme/jobs/1', 'x', 8, 9.0, 'failed', "
        "'crash_unconfirmed', 99)")
    conn.commit()
    L.reset_failed()
    r = conn.execute("SELECT apply_status, apply_error, apply_attempts FROM jobs "
                     "WHERE url LIKE '%/cu'").fetchone()
    assert r[0] == "failed" and r[1] == "crash_unconfirmed" and r[2] == 99
    assert L.acquire_job(min_score=7) is None  # still not re-acquirable


def test_reset_failed_does_not_unpark_no_confirmation(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score, apply_status, apply_error, apply_attempts) VALUES "
        "('https://hiring.cafe/nc', 'Chief of Staff', 'X', 'Acme', "
        "'https://job-boards.greenhouse.io/acme/jobs/2', 'x', 8, 9.0, 'failed', "
        "'no_confirmation', 1)")
    conn.commit()
    L.reset_failed()
    r = conn.execute("SELECT apply_status, apply_error FROM jobs "
                     "WHERE url LIKE '%/nc'").fetchone()
    assert r[0] == "failed" and r[1] == "no_confirmation"
    assert L.acquire_job(min_score=7) is None  # still not re-acquirable


def test_reset_failed_resets_ordinary_failure(conn):
    # A genuine, safe-to-retry failure (page_error -- the agent never reached submit)
    # SHOULD still be reset by --reset-failed and become acquirable again.
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score, apply_status, apply_error, apply_attempts) VALUES "
        "('https://hiring.cafe/pe', 'Chief of Staff', 'X', 'Acme', "
        "'https://job-boards.greenhouse.io/acme/jobs/3', 'x', 8, 9.0, 'failed', "
        "'page_error', 1)")
    conn.commit()
    L.reset_failed()
    r = conn.execute("SELECT apply_status, apply_error, apply_attempts FROM jobs "
                     "WHERE url LIKE '%/pe'").fetchone()
    assert r[0] is None and r[1] is None and r[2] == 0
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"].endswith("/pe")
