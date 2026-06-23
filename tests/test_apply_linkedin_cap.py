"""Regression guard for the workers=2 default.

Parallel apply workers are only account-safe because the LinkedIn daily cap is
DERIVED FROM THE DB (process-global), not held in per-worker memory: every worker
reads the same rolling-24h count, so once the cap is reached they all exclude the
LinkedIn lane and the real ceiling cannot be exceeded by running N workers. These
tests pin that property. If a future refactor makes the count per-worker, the
workers=2 default must not ship.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from applypilot import database


def _seed(conn, url, title, *, application_url=None, audit=9.0,
          applied=False, applied_at=None):
    conn.execute(
        "INSERT INTO jobs (url, title, site, application_url, tailored_resume_path, "
        "fit_score, audit_score, apply_status, applied_at) "
        "VALUES (?, ?, 'TestCo', ?, 'x', 8, ?, ?, ?)",
        (url, title, application_url, audit,
         "applied" if applied else None, applied_at),
    )
    conn.commit()


def test_linkedin_cap_is_db_derived_and_excludes_lane(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    from applypilot.apply import launcher as L
    monkeypatch.setattr(L, "get_connection", lambda: conn)

    # Top-ranked LinkedIn Easy-Apply candidate + a lower-ranked offsite ATS candidate.
    _seed(conn, "https://www.linkedin.com/jobs/view/111", "CoS LinkedIn", audit=9.5)
    _seed(conn, "https://boards.greenhouse.io/acme/jobs/222", "CoS Offsite",
          application_url="https://boards.greenhouse.io/acme/jobs/222", audit=8.0)

    # Simulate the rolling-24h LinkedIn cap already being reached (3 recent applies).
    now = datetime.now(timezone.utc).isoformat()
    for i in range(3):
        _seed(conn, f"https://www.linkedin.com/jobs/view/applied{i}", f"old{i}",
              applied=True, applied_at=now)

    # Process-global: the count comes from the DB, so two workers see the same value.
    assert L._linkedin_today(conn) == 3

    # Cap reached -> LinkedIn lane excluded -> the OFFSITE job is acquired, even
    # though the LinkedIn one outranks it. This is exactly what prevents N parallel
    # workers from each applying their own quota past the shared ceiling.
    job = L.acquire_job(min_score=7, exclude_linkedin=True)
    assert job is not None
    eff = job["application_url"] or job["url"]
    assert "greenhouse" in eff and "linkedin.com" not in eff

    # Control: without exclusion, the higher-ranked LinkedIn job is picked.
    conn.execute("UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url LIKE '%greenhouse%'")
    conn.commit()
    job2 = L.acquire_job(min_score=7, exclude_linkedin=False)
    assert job2 is not None and "linkedin.com" in job2["url"]


def test_linkedin_halt_is_db_shared_across_processes(tmp_path, monkeypatch):
    """A LinkedIn challenge/rate-limit recorded by ONE process must halt the lane for
    ALL processes — so the halt is derived from the DB, not in-memory per-process."""
    conn = database.init_db(tmp_path / "applypilot.db")
    from applypilot.apply import launcher as L

    now = datetime.now(timezone.utc).isoformat()
    assert L._linkedin_halt_active(conn) is False  # nothing failed yet

    # Process A records a LinkedIn challenge failure (mark_result persists apply_error).
    conn.execute(
        "INSERT INTO jobs (url, title, site, apply_status, apply_error, last_attempted_at) "
        "VALUES ('https://www.linkedin.com/jobs/view/777', 'x', 'TestCo', 'failed', 'linkedin_challenge', ?)",
        (now,),
    )
    conn.commit()

    # Process B (same DB) now sees the halt — no shared memory needed.
    assert L._linkedin_halt_active(conn) is True

    # A non-halt failure (e.g. salary) does NOT trip it.
    conn.execute("DELETE FROM jobs")
    conn.execute(
        "INSERT INTO jobs (url, title, site, apply_status, apply_error, last_attempted_at) "
        "VALUES ('https://www.linkedin.com/jobs/view/888', 'x', 'TestCo', 'failed', 'not_eligible_salary', ?)",
        (now,),
    )
    conn.commit()
    assert L._linkedin_halt_active(conn) is False


def test_linkedin_gap_wait_paces_against_other_processes(tmp_path, monkeypatch):
    """The cross-process gap waits relative to the most recent OTHER LinkedIn attempt,
    excluding the current job's own row."""
    conn = database.init_db(tmp_path / "applypilot.db")
    from applypilot.apply import launcher as L
    monkeypatch.setattr(L, "GAP_JITTER_HI", 1.0)
    monkeypatch.setattr(L, "GAP_JITTER_LO", 1.0)  # disable jitter for a deterministic assert
    monkeypatch.setattr(L, "LINKEDIN_HOST_GAP", 120.0)

    now = datetime.now(timezone.utc).isoformat()
    # Another worker/process just touched LinkedIn 'now'.
    conn.execute(
        "INSERT INTO jobs (url, title, site, last_attempted_at) "
        "VALUES ('https://www.linkedin.com/jobs/view/other', 'x', 'TestCo', ?)",
        (now,),
    )
    conn.commit()
    wait = L._linkedin_gap_wait(conn, "https://www.linkedin.com/jobs/view/mine")
    assert 100.0 <= wait <= 120.0  # ~full gap, since the other attempt was just now

    # Excluding the current job's own row: only that row exists -> no wait.
    conn.execute("DELETE FROM jobs")
    conn.execute(
        "INSERT INTO jobs (url, title, site, last_attempted_at) "
        "VALUES ('https://www.linkedin.com/jobs/view/mine', 'x', 'TestCo', ?)",
        (now,),
    )
    conn.commit()
    assert L._linkedin_gap_wait(conn, "https://www.linkedin.com/jobs/view/mine") == 0.0


def test_linkedin_today_counts_only_linkedin_lane_applies(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    from applypilot.apply import launcher as L

    now = datetime.now(timezone.utc).isoformat()
    # An applied LinkedIn job counts; an applied OFFSITE job (greenhouse) does not.
    _seed(conn, "https://www.linkedin.com/jobs/view/a", "li", applied=True, applied_at=now)
    _seed(conn, "https://boards.greenhouse.io/x/jobs/b", "gh",
          application_url="https://boards.greenhouse.io/x/jobs/b", applied=True, applied_at=now)
    # A LinkedIn-sourced job that REDIRECTS offsite (application_url is the ATS link)
    # is on the offsite lane -> must NOT count against the LinkedIn cap.
    _seed(conn, "https://www.linkedin.com/jobs/view/c", "li-offsite",
          application_url="https://jobs.lever.co/x/c", applied=True, applied_at=now)

    assert L._linkedin_today(conn) == 1
