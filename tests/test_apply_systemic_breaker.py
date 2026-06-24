"""Regression guard for the global systemic-failure circuit breaker.

A mid-run auth/API/CDP outage makes EVERY job fail with no_result_line or timeout,
both of which are PERMANENT failures. Without a global brake an unattended continuous
run would mark the whole applyable queue permanently failed before anyone noticed.
These tests pin the breaker's three properties:
  1. Classification: only no_result_line/timeout count as systemic (job-specific
     failures like captcha/salary are proof-of-life and must NOT count).
  2. Streak logic: only an UNBROKEN run of systemic failures trips; any proof-of-life
     outcome resets the counter.
  3. Un-burn: tripping the breaker resets the burned streak back to retryable, so a
     transient outage does not permanently destroy good jobs.
"""
from __future__ import annotations

import pytest

from applypilot import database
from applypilot.apply import launcher as L


@pytest.fixture(autouse=True)
def _reset_breaker():
    """Each test starts with a clean breaker (module-level state persists)."""
    with L._systemic_fail_lock:
        L._systemic_fail_count = 0
        L._systemic_recent.clear()
    yield
    with L._systemic_fail_lock:
        L._systemic_fail_count = 0
        L._systemic_recent.clear()


def test_classification_systemic_vs_job_specific():
    # Systemic: the agent never proved it drove the browser (env outage signature).
    assert L._is_systemic_failure("no_result_line") is True
    assert L._is_systemic_failure("timeout") is True
    assert L._is_systemic_failure("failed:no_result_line") is True  # tolerate prefix
    assert L._is_systemic_failure("failed:timeout") is True
    # Job-specific: proof the agent reached the page -> NOT systemic.
    for r in ("captcha", "not_eligible_salary", "auth_required", "already_applied",
              "no_confirmation", "page_error", "login_issue"):
        assert L._is_systemic_failure(r) is False, r
    assert L._is_systemic_failure(None) is False
    assert L._is_systemic_failure("") is False


def test_unbroken_streak_trips_at_threshold(monkeypatch):
    monkeypatch.setattr(L, "SYSTEMIC_FAIL_BREAKER", 5)
    # Four in a row: no trip yet.
    for i in range(4):
        assert L._note_systemic_failure(f"https://x/{i}") is False
    # Fifth consecutive: trips.
    assert L._note_systemic_failure("https://x/4") is True


def test_proof_of_life_resets_streak(monkeypatch):
    monkeypatch.setattr(L, "SYSTEMIC_FAIL_BREAKER", 5)
    for i in range(4):
        assert L._note_systemic_failure(f"https://x/{i}") is False
    # A healthy outcome (applied, or a job-specific failure) resets the counter...
    L._note_healthy_outcome()
    # ...so the next four systemic failures still do NOT trip (would need 5 fresh).
    for i in range(4):
        assert L._note_systemic_failure(f"https://y/{i}") is False
    assert L._note_systemic_failure("https://y/4") is True


def test_disabled_breaker_never_trips_and_bounds_memory(monkeypatch):
    monkeypatch.setattr(L, "SYSTEMIC_FAIL_BREAKER", 0)
    for i in range(50):
        assert L._note_systemic_failure(f"https://z/{i}") is False
    # The recent-URL list must stay bounded even with the breaker disabled (no leak
    # over a long run).
    with L._systemic_fail_lock:
        assert len(L._systemic_recent) <= 2


def test_trip_unburns_the_streak(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(L, "get_connection", lambda: conn)
    monkeypatch.setattr(L, "SYSTEMIC_FAIL_BREAKER", 3)

    # Three good jobs that a systemic outage just burned to permanent (attempts=99).
    urls = [f"https://boards.greenhouse.io/acme/jobs/{i}" for i in range(3)]
    for u in urls:
        conn.execute(
            "INSERT INTO jobs (url, title, site, apply_status, apply_error, apply_attempts) "
            "VALUES (?, 'CoS', 'TestCo', 'failed', 'no_result_line', 99)",
            (u,),
        )
    conn.commit()

    # Simulate the streak that tripped the breaker, then trip it.
    for u in urls:
        L._note_systemic_failure(u)
    L._trip_systemic_breaker(worker_id=0)

    # The run is halted...
    assert L._stop_event.is_set()
    L._stop_event.clear()  # don't leak the set event into other tests

    # ...and every burned job is back to retryable (status cleared, attempts reset),
    # so the next run picks them up once the environment recovers.
    rows = conn.execute(
        "SELECT apply_status, apply_attempts, apply_error FROM jobs ORDER BY url"
    ).fetchall()
    assert len(rows) == 3
    for status, attempts, err in rows:
        assert status is None
        assert attempts == 0
        assert err == "systemic_halt"


def test_five_consecutive_no_result_halts_and_keeps_jobs_retryable(tmp_path, monkeypatch):
    """The exact systemic-outage scenario: 5 consecutive no_result_line failures (auth/
    API/CDP dead) must HALT the run and leave those good jobs RETRYABLE -- NOT permanently
    burned to attempts=99 -- so they're picked up again once the environment recovers."""
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(L, "get_connection", lambda: conn)
    monkeypatch.setattr(L, "SYSTEMIC_FAIL_BREAKER", 5)
    L._stop_event.clear()

    urls = [f"https://job-boards.greenhouse.io/co/jobs/{i}" for i in range(5)]
    # no_result_line is in PERMANENT_FAILURES, so the outage marked each attempts=99.
    for u in urls:
        conn.execute("INSERT INTO jobs (url, title, site, apply_status, apply_error, apply_attempts) "
                     "VALUES (?, 'CoS', 'X', 'failed', 'no_result_line', 99)", (u,))
    conn.commit()

    tripped_on = None
    for i, u in enumerate(urls):
        assert L._is_systemic_failure("failed:no_result_line") is True  # the run_job code
        if L._note_systemic_failure(u):
            tripped_on = i
            L._trip_systemic_breaker(worker_id=0)
            break
    assert tripped_on == 4          # trips on the 5th consecutive, not before
    assert L._stop_event.is_set()   # run halted
    L._stop_event.clear()

    # Every job is back to retryable (NOT permanent) -> the outage cost zero good jobs.
    for status, attempts in conn.execute("SELECT apply_status, apply_attempts FROM jobs"):
        assert status is None and attempts == 0


def test_browser_failures_are_systemic_and_env_alias_documented():
    # browser_crashed / browser_unavailable (seen live) count toward the breaker so a
    # broken Chrome halts a long run; a one-off resets on the next success.
    assert L._is_systemic_failure("browser_crashed") is True
    assert L._is_systemic_failure("failed:browser_unavailable") is True
    # job-specific outcomes still reset the streak (not systemic).
    assert L._is_systemic_failure("captcha") is False
    assert L._is_systemic_failure("not_eligible_location") is False
