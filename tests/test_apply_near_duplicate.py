"""Regression guard for the near-duplicate reposting guard + the duplicate monitor.

Real incident: Amae Health posted one role twice -- "Founder Associate, Growth &
Partnership Operations" (greenhouse job 4259921009) and "Business Development Associate,
Growth & Partnership Operations" (greenhouse job 4288094009), both on
greenhouse.io/amaehealth -> the applicant got two confirmation emails. Exact
(company,title)+url dedup misses it (both the title and the job ID differ). The reliable
shared signal is the employer ATS board slug, which both share and which generic
aggregators (chiefofstaffjob.com) do NOT encode.
"""
from __future__ import annotations

import pytest

from applypilot import database
from applypilot.apply import launcher as L


def test_employer_board_slug():
    assert L._employer_board_slug("https://job-boards.greenhouse.io/amaehealth/jobs/4259921009") == "greenhouse.io/amaehealth"
    assert L._employer_board_slug("https://jobs.ashbyhq.com/picogrid/abc") == "ashbyhq.com/picogrid"
    # generic boards do NOT encode the employer -> None (so distinct companies aren't merged)
    assert L._employer_board_slug("https://www.linkedin.com/jobs/view/123") is None
    assert L._employer_board_slug("https://www.chiefofstaffjob.com/jobs/x") is None
    assert L._employer_board_slug("https://hiring.cafe/viewjob/x") is None


def test_slug_none_for_subdomain_and_ambiguous_ats():
    # These previously produced BOGUS employer slugs (workable.com/view, bamboohr.com/123,
    # the workday site segment) that could falsely merge two different companies. Now None,
    # so those employers are matched by the company field instead -- never by a bad slug.
    assert L._employer_board_slug("https://jobs.workable.com/view/xyz") is None
    assert L._employer_board_slug("https://co.bamboohr.com/careers/123") is None
    assert L._employer_board_slug("https://equinix.wd1.myworkdayjobs.com/External/job/x") is None
    assert L._employer_board_slug("https://acme.recruitee.com/o/role") is None


def test_same_employer_by_company_or_slug_no_false_merge():
    # Same real company -> same employer even across boards (robust, no URL parsing).
    assert L._same_employer("https://www.linkedin.com/jobs/view/1", "Amae Health",
                            "https://job-boards.greenhouse.io/amaehealth/jobs/2", "Amae Health") is True
    # Same greenhouse board with no company -> same employer via the reliable slug.
    assert L._same_employer("https://job-boards.greenhouse.io/amaehealth/jobs/1", "",
                            "https://job-boards.greenhouse.io/amaehealth/jobs/2", "") is True
    # DIFFERENT companies with no reliable slug must NOT be merged (the false-positive
    # the slug bug would have caused).
    assert L._same_employer("https://jobs.workable.com/view/a", "Acme",
                            "https://jobs.workable.com/view/b", "Globex") is False
    # The aggregator pseudo-company is not an employer -> never merges its listings.
    assert L._same_employer("https://chiefofstaffjob.com/jobs/a", "ChiefOfStaffJob.com",
                            "https://chiefofstaffjob.com/jobs/b", "ChiefOfStaffJob.com") is False


def test_near_dup_cross_board_same_company(conn):
    # Amae applied via greenhouse; a LinkedIn re-list of the same role (no resolved ATS
    # url, but company='Amae Health') must still be caught via the company signal.
    _ins(conn, "https://hiring.cafe/v/gh", "Founder Associate, Growth & Partnership Operations",
         company="Amae Health", app_url="https://job-boards.greenhouse.io/amaehealth/jobs/1", status="applied")
    dup = L._find_near_duplicate_applied(
        conn, "https://www.linkedin.com/jobs/view/999",
        "Founder Associate, Growth & Partnership Operations", "Amae Health")
    assert dup is not None


def test_sig_title_tokens_drops_boilerplate():
    toks = L._sig_title_tokens("Chief of Staff On-Site Full Time United States 26")
    assert "chief" in toks and "staff" in toks
    assert "of" not in toks and "full" not in toks and "time" not in toks
    assert "united" not in toks and "states" not in toks and "26" not in toks


@pytest.fixture
def conn(tmp_path, monkeypatch):
    c = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(L, "get_connection", lambda: c)
    return c


def _ins(conn, url, title, *, company="", app_url=None, status=None):
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, tailored_resume_path, "
        "fit_score, audit_score, apply_status, applied_at) VALUES (?, ?, 'X', ?, ?, 'x', 8, 9.0, ?, ?)",
        (url, title, company, app_url, status, "2026-06-24T10:00:00+00:00" if status == "applied" else None),
    )
    conn.commit()


def test_near_duplicate_detected_same_board(conn):
    _ins(conn, "https://hiring.cafe/v/1", "Business Development Associate, Growth & Partnership Operations",
         company="Amae Health", app_url="https://job-boards.greenhouse.io/amaehealth/jobs/4288094009", status="applied")
    dup = L._find_near_duplicate_applied(
        conn, "https://job-boards.greenhouse.io/amaehealth/jobs/4259921009",
        "Founder Associate, Growth & Partnership Operations")
    assert dup is not None  # same employer board + 'associate/growth/partnership/operations' overlap


def test_distinct_role_same_board_not_flagged(conn):
    # A genuinely different role at the SAME employer board must still be applyable.
    _ins(conn, "https://hiring.cafe/v/2", "Founder Associate, Growth & Partnership Operations",
         company="Amae Health", app_url="https://job-boards.greenhouse.io/amaehealth/jobs/4259921009", status="applied")
    assert L._find_near_duplicate_applied(
        conn, "https://job-boards.greenhouse.io/amaehealth/jobs/9999", "Clinic Director") is None


def test_identical_title_same_employer_is_dup(conn):
    # Two IDENTICAL 'Chief of Staff' listings at the same company ARE a duplicate
    # (Jaccard 1.0) -- apply once.
    _ins(conn, "https://hiring.cafe/v/3", "Chief of Staff", company="Acme",
         app_url="https://job-boards.greenhouse.io/acme/jobs/1", status="applied")
    assert L._find_near_duplicate_applied(
        conn, "https://job-boards.greenhouse.io/acme/jobs/2", "Chief of Staff", "Acme") is not None


def test_different_roles_same_employer_released(conn):
    # The user's rule: "if they aren't identical roles then they aren't duplicates."
    # Different roles at one company share a word or two but stay below the ratio.
    _ins(conn, "https://hiring.cafe/v/g", "GTM Strategy and Operations", company="Acme",
         app_url="https://job-boards.greenhouse.io/acme/jobs/1", status="applied")
    _ins(conn, "https://hiring.cafe/v/bd", "Business Development Associate", company="Acme",
         app_url="https://job-boards.greenhouse.io/acme/jobs/2", status="applied")
    # "Pricing Strategy and Operations" vs "GTM Strategy and Operations" -> 0.50 < 0.55
    assert L._find_near_duplicate_applied(
        conn, "https://job-boards.greenhouse.io/acme/jobs/3", "Pricing Strategy and Operations", "Acme") is None
    # "Business Development Representative" vs "...Associate" -> 0.50 < 0.55 (different role)
    assert L._find_near_duplicate_applied(
        conn, "https://job-boards.greenhouse.io/acme/jobs/4", "Business Development Representative", "Acme") is None


def test_acquire_skips_near_duplicate(conn):
    # The Amae shape end-to-end: one applied, the re-post is the only pending candidate.
    _ins(conn, "https://hiring.cafe/v/applied", "Business Development Associate, Growth & Partnership Operations",
         company="Amae Health", app_url="https://job-boards.greenhouse.io/amaehealth/jobs/4288094009", status="applied")
    _ins(conn, "https://hiring.cafe/v/repost", "Founder Associate, Growth & Partnership Operations",
         company="Amae Health", app_url="https://job-boards.greenhouse.io/amaehealth/jobs/4259921009")
    assert L.acquire_job(min_score=7) is None  # the re-post is skipped, queue effectively empty
    row = conn.execute("SELECT apply_status, apply_error FROM jobs WHERE url LIKE '%/repost'").fetchone()
    assert row[0] == "deferred" and row[1] == "near_duplicate_role"


def test_audit_flags_amae_excludes_aggregator(conn):
    # Amae near-dup IS reported; the chiefofstaffjob aggregator pseudo-company is NOT
    # (different employers that merely share the aggregator company + boilerplate tokens).
    _ins(conn, "https://hiring.cafe/v/a", "Founder Associate, Growth & Partnership Operations",
         company="Amae Health", app_url="https://job-boards.greenhouse.io/amaehealth/jobs/1", status="applied")
    _ins(conn, "https://hiring.cafe/v/b", "Business Development Associate, Growth & Partnership Operations",
         company="Amae Health", app_url="https://job-boards.greenhouse.io/amaehealth/jobs/2", status="applied")
    # two DIFFERENT employers via the aggregator (same pseudo-company + boilerplate tokens)
    _ins(conn, "https://www.chiefofstaffjob.com/jobs/x", "Chief of Staff On-Site Full Time United States",
         company="ChiefOfStaffJob.com", app_url="https://www.chiefofstaffjob.com/jobs/x", status="applied")
    _ins(conn, "https://www.chiefofstaffjob.com/jobs/y", "Chief of Staff Remote Full Time United States",
         company="ChiefOfStaffJob.com", app_url="https://www.chiefofstaffjob.com/jobs/y", status="applied")
    dups = L.audit_duplicate_applications(conn)
    assert len(dups) == 1
    assert dups[0]["employer"] == "greenhouse.io/amaehealth"
    assert dups[0]["kind"] == "near-duplicate"


def test_audit_works_with_plain_connection(tmp_path):
    # audit_duplicate_applications must not assume the caller's connection has
    # row_factory=sqlite3.Row -- a plain sqlite3 connection used to crash with
    # "ValueError: dictionary update sequence element #0 has length N; 2 is required".
    import sqlite3
    database.init_db(tmp_path / "applypilot.db")
    plain = sqlite3.connect(str(tmp_path / "applypilot.db"))  # NO row_factory set
    plain.execute(
        "INSERT INTO jobs (url, title, site, company, application_url, apply_status, applied_at) "
        "VALUES ('https://hiring.cafe/x', 'Chief of Staff', 'X', 'Acme', "
        "'https://job-boards.greenhouse.io/acme/jobs/1', 'applied', '2026-06-24T00:00:00+00:00')"
    )
    plain.commit()
    result = L.audit_duplicate_applications(plain)  # must not raise
    assert isinstance(result, list)
