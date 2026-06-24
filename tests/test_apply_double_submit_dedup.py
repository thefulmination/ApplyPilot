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
    # A DIFFERENT role at the same company is still acquirable (distinct title), and an
    # empty-company row is never excluded by the company+title arm.
    _seed(conn, "https://hiring.cafe/viewjob/cos", company="BigCo",
          title="Chief of Staff", application_url="https://boards.greenhouse.io/bigco/1",
          apply_status="applied")
    _seed(conn, "https://hiring.cafe/viewjob/cos-ceo", company="BigCo",
          title="Chief of Staff to the CEO",
          application_url="https://boards.greenhouse.io/bigco/2", apply_status=None)
    job = L.acquire_job(min_score=7)
    assert job is not None and job["url"].endswith("/cos-ceo")
