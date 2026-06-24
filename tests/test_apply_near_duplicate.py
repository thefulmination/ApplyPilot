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


def test_short_title_never_near_dup(conn):
    # 'Chief of Staff' (2 significant tokens) must never trigger a near-dup skip.
    _ins(conn, "https://hiring.cafe/v/3", "Chief of Staff", company="Acme",
         app_url="https://job-boards.greenhouse.io/acme/jobs/1", status="applied")
    assert L._find_near_duplicate_applied(
        conn, "https://job-boards.greenhouse.io/acme/jobs/2", "Chief of Staff") is None


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
