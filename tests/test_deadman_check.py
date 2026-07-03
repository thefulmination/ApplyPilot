"""Tests for the pure fleet dead-man detector (applypilot.fleet.deadman).

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema applied).
Every test seeds fleet_config / worker_heartbeat / apply_queue / llm_usage directly
via psycopg, then calls ``deadman_check`` with an INJECTED ``now`` and asserts the
exact set of alert ``kind``s returned (never real wall-clock time).
"""
from __future__ import annotations

import datetime as dt

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import deadman

NOW = dt.datetime(2026, 7, 3, 12, 0, 0, tzinfo=dt.timezone.utc)


def _arm(cur):
    cur.execute("UPDATE fleet_config SET paused=FALSE, ats_paused=FALSE WHERE id=1;")


def _heartbeat(cur, worker_id, last_beat, state="idle"):
    cur.execute(
        "INSERT INTO worker_heartbeat (worker_id, state, last_beat) VALUES (%s, %s, %s) "
        "ON CONFLICT (worker_id) DO UPDATE SET state=EXCLUDED.state, last_beat=EXCLUDED.last_beat;",
        (worker_id, state, last_beat),
    )


def _queue_row(cur, url, status, *, approved_batch=None, updated_at=None):
    updated_at = updated_at or NOW
    cur.execute(
        "INSERT INTO apply_queue (url, application_url, score, status, approved_batch, updated_at) "
        "VALUES (%s, %s, 1.0, %s, %s, %s) "
        "ON CONFLICT (url) DO UPDATE SET status=EXCLUDED.status, approved_batch=EXCLUDED.approved_batch, "
        "updated_at=EXCLUDED.updated_at;",
        (url, url, status, approved_batch, updated_at),
    )


def _llm_usage(cur, cost_usd, ts=None):
    ts = ts or NOW
    cur.execute(
        "INSERT INTO llm_usage (cost_usd, ts) VALUES (%s, %s);",
        (cost_usd, ts),
    )


def _doctor_pass(cur, ts):
    # The Fleet Doctor stamps fleet_config.doctor_last_pass_at each pass (its liveness signal).
    cur.execute("UPDATE fleet_config SET doctor_last_pass_at=%s WHERE id=1;", (ts,))


def _kinds(alerts):
    return {a.kind for a in alerts}


# ---------------------------------------------------------------------------
# all-healthy baseline
# ---------------------------------------------------------------------------

def test_all_healthy_returns_no_alerts_and_zero_streak(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            _doctor_pass(cur, NOW - dt.timedelta(minutes=5))
        conn.commit()

        alerts, streak = deadman.deadman_check(conn, now=NOW)

    assert alerts == []
    assert streak == 0


# ---------------------------------------------------------------------------
# silent_death
# ---------------------------------------------------------------------------

def test_silent_death_when_last_beat_stale(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=40))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "silent_death" in _kinds(alerts)


def test_silent_death_clears_when_beat_recent(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "silent_death" not in _kinds(alerts)


def test_silent_death_when_no_heartbeat_rows_at_all(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "silent_death" in _kinds(alerts)


def test_silent_death_not_armed_when_paused(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE, ats_paused=FALSE WHERE id=1;")
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=40))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "silent_death" not in _kinds(alerts)


def test_silent_death_not_armed_when_ats_paused(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=FALSE, ats_paused=TRUE WHERE id=1;")
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=40))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "silent_death" not in _kinds(alerts)


def test_silent_death_ignores_watchdog_and_linkedin_workers(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            # Only watchdog/linkedin workers are alive; no real apply worker beat.
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=1))
            _heartbeat(cur, "linkedin-worker-1", NOW - dt.timedelta(minutes=1))
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "silent_death" in _kinds(alerts)


# ---------------------------------------------------------------------------
# stalled_queue
# ---------------------------------------------------------------------------

def test_stalled_queue_when_queued_approved_but_no_recent_applies(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            _queue_row(cur, "https://boards.greenhouse.io/acme/jobs/1", "queued", approved_batch="b1")
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "stalled_queue" in _kinds(alerts)


def test_stalled_queue_clears_with_recent_applied_row(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            _queue_row(cur, "https://boards.greenhouse.io/acme/jobs/1", "queued", approved_batch="b1")
            _queue_row(cur, "https://boards.greenhouse.io/acme/jobs/2", "applied",
                       updated_at=NOW - dt.timedelta(hours=1))
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "stalled_queue" not in _kinds(alerts)


def test_stalled_queue_not_armed_when_paused(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE, ats_paused=FALSE WHERE id=1;")
            _queue_row(cur, "https://boards.greenhouse.io/acme/jobs/1", "queued", approved_batch="b1")
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "stalled_queue" not in _kinds(alerts)


def test_stalled_queue_not_triggered_without_approved_queued_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            # queued but NOT approved -> not eligible to stall on.
            _queue_row(cur, "https://boards.greenhouse.io/acme/jobs/1", "queued", approved_batch=None)
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "stalled_queue" not in _kinds(alerts)


# ---------------------------------------------------------------------------
# selfheal_dead
# ---------------------------------------------------------------------------

def test_selfheal_dead_when_no_watchdog_beat(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "selfheal_dead" in _kinds(alerts)


def test_selfheal_dead_when_watchdog_beat_stale(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=40))
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "selfheal_dead" in _kinds(alerts)


def test_selfheal_dead_clears_when_both_watchdog_and_doctor_fresh(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            _doctor_pass(cur, NOW - dt.timedelta(minutes=5))
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "selfheal_dead" not in _kinds(alerts)


def test_selfheal_dead_when_doctor_pass_stale_even_if_watchdog_alive(fleet_db):
    # The Fleet Doctor is the PRIMARY self-healer (5-min cadence). A fresh watchdog must
    # not mask a dead Doctor -- selfheal_dead fires when doctor_last_pass_at is stale/NULL.
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))  # watchdog alive
            _doctor_pass(cur, NOW - dt.timedelta(minutes=40))             # doctor stale
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "selfheal_dead" in _kinds(alerts)


def test_naive_now_is_coerced_not_crashed(fleet_db):
    # A naive `now` (datetime.now() vs datetime.now(timezone.utc)) must NOT crash the
    # monitor with a bare TypeError deep in an aware-vs-naive comparison. Seed a real
    # silent_death and pass a NAIVE now: the check still computes correctly.
    naive_now = dt.datetime(2026, 7, 3, 12, 0, 0)  # no tzinfo
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=40))  # stale
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            _doctor_pass(cur, NOW - dt.timedelta(minutes=5))
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=naive_now)  # must not raise

    assert "silent_death" in _kinds(alerts)


def test_selfheal_dead_fires_even_when_paused(fleet_db):
    # selfheal_dead has no `armed` gate in the brief -- the watchdog itself being
    # down matters regardless of whether the fleet is currently paused.
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE, ats_paused=FALSE WHERE id=1;")
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    assert "selfheal_dead" in _kinds(alerts)


# ---------------------------------------------------------------------------
# running_hot (streak semantics)
# ---------------------------------------------------------------------------

def test_running_hot_requires_two_consecutive_calls(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            cur.execute("UPDATE fleet_config SET cost_cap_daily_usd=100 WHERE id=1;")
            _llm_usage(cur, 96.0, NOW - dt.timedelta(hours=1))
        conn.commit()

        alerts1, streak1 = deadman.deadman_check(conn, now=NOW, prev_hot_streak=0)
        assert "running_hot" not in _kinds(alerts1)
        assert streak1 == 1

        alerts2, streak2 = deadman.deadman_check(conn, now=NOW, prev_hot_streak=streak1)
        assert "running_hot" in _kinds(alerts2)
        assert streak2 == 2


def test_running_hot_resets_streak_when_below_threshold(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            cur.execute("UPDATE fleet_config SET cost_cap_daily_usd=100 WHERE id=1;")
            _llm_usage(cur, 10.0, NOW - dt.timedelta(hours=1))
        conn.commit()

        alerts, streak = deadman.deadman_check(conn, now=NOW, prev_hot_streak=1)

    assert "running_hot" not in _kinds(alerts)
    assert streak == 0


def test_running_hot_ignores_usage_outside_rolling_24h_window(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            cur.execute("UPDATE fleet_config SET cost_cap_daily_usd=100 WHERE id=1;")
            _llm_usage(cur, 96.0, NOW - dt.timedelta(hours=25))  # outside window
        conn.commit()

        alerts, streak = deadman.deadman_check(conn, now=NOW, prev_hot_streak=1)

    assert "running_hot" not in _kinds(alerts)
    assert streak == 0


def test_running_hot_disabled_when_daily_cap_zero(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            cur.execute("UPDATE fleet_config SET cost_cap_daily_usd=0 WHERE id=1;")
            _llm_usage(cur, 9999.0, NOW - dt.timedelta(hours=1))
        conn.commit()

        alerts, streak = deadman.deadman_check(conn, now=NOW, prev_hot_streak=1)

    assert "running_hot" not in _kinds(alerts)
    assert streak == 0


# ---------------------------------------------------------------------------
# read-only guarantee + combined conditions
# ---------------------------------------------------------------------------

def test_deadman_check_does_not_write_anything(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
        conn.commit()

        deadman.deadman_check(conn, now=NOW)

        # A read-only function must not leave an open write transaction; a fresh
        # cursor read should see the exact same armed state we seeded.
        with conn.cursor() as cur:
            cur.execute("SELECT paused, ats_paused FROM fleet_config WHERE id=1;")
            row = cur.fetchone()
    assert row["paused"] is False
    assert row["ats_paused"] is False


def test_multiple_conditions_can_fire_together(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            # No heartbeats at all -> silent_death AND selfheal_dead both fire.
            _queue_row(cur, "https://boards.greenhouse.io/acme/jobs/1", "queued", approved_batch="b1")
        conn.commit()

        alerts, _ = deadman.deadman_check(conn, now=NOW)

    kinds = _kinds(alerts)
    assert "silent_death" in kinds
    assert "selfheal_dead" in kinds
    assert "stalled_queue" in kinds
    assert all(isinstance(a.kind, str) and isinstance(a.severity, str) and isinstance(a.detail, str)
               for a in alerts)
