"""Tests for the persistence + delivery wrapper around the pure dead-man check
(applypilot.fleet.deadman.run_deadman).

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema applied)
plus ``tmp_path`` for the ALERT file. ``run_deadman`` is NOT pure -- it persists the
hot streak + alert flag to fleet_config, writes/removes the ALERT file, and
best-effort attempts a Windows toast (never raises).
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


def _doctor_pass(cur, ts):
    cur.execute("UPDATE fleet_config SET doctor_last_pass_at=%s WHERE id=1;", (ts,))


def _llm_usage(cur, cost_usd, ts=None):
    ts = ts or NOW
    cur.execute("INSERT INTO llm_usage (cost_usd, ts) VALUES (%s, %s);", (cost_usd, ts))


def _get_config_alert(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT deadman_alert, deadman_alert_at, deadman_hot_streak FROM fleet_config WHERE id=1;")
        return cur.fetchone()


# ---------------------------------------------------------------------------
# silent_death: alert set + file written, then heals + clears
# ---------------------------------------------------------------------------

def test_silent_death_sets_alert_and_writes_file_then_heals(fleet_db, tmp_path):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=40))  # stale
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            _doctor_pass(cur, NOW - dt.timedelta(minutes=5))
        conn.commit()

        alerts = deadman.run_deadman(conn, now=NOW, alert_dir=tmp_path)
        assert any(a.kind == "silent_death" for a in alerts)

        row = _get_config_alert(conn)
        assert row["deadman_alert"] is not None
        assert "silent_death" in row["deadman_alert"]
        assert row["deadman_alert_at"] is not None

        alert_file = tmp_path / "fleet-ALERT.txt"
        assert alert_file.exists()
        content = alert_file.read_text(encoding="utf-8")
        assert "silent_death" in content

        # Heal: fresh heartbeat, re-run -> alert clears + file removed.
        with conn.cursor() as cur:
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=1))
        conn.commit()

        alerts2 = deadman.run_deadman(conn, now=NOW, alert_dir=tmp_path)
        assert alerts2 == []

        row2 = _get_config_alert(conn)
        assert row2["deadman_alert"] is None
        assert row2["deadman_alert_at"] is None
        assert not alert_file.exists()


# ---------------------------------------------------------------------------
# running_hot: streak persists across separate run_deadman calls
# ---------------------------------------------------------------------------

def test_running_hot_streak_persists_across_calls(fleet_db, tmp_path):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            _doctor_pass(cur, NOW - dt.timedelta(minutes=5))
            cur.execute("UPDATE fleet_config SET cost_cap_daily_usd=100 WHERE id=1;")
            _llm_usage(cur, 96.0, NOW - dt.timedelta(hours=1))
        conn.commit()

        alerts1 = deadman.run_deadman(conn, now=NOW, alert_dir=tmp_path)
        assert alerts1 == []
        row1 = _get_config_alert(conn)
        assert row1["deadman_hot_streak"] == 1
        assert not (tmp_path / "fleet-ALERT.txt").exists()

        alerts2 = deadman.run_deadman(conn, now=NOW, alert_dir=tmp_path)
        assert any(a.kind == "running_hot" for a in alerts2)
        row2 = _get_config_alert(conn)
        assert row2["deadman_hot_streak"] == 2
        assert (tmp_path / "fleet-ALERT.txt").exists()


# ---------------------------------------------------------------------------
# toast delivery is best-effort and must never raise
# ---------------------------------------------------------------------------

def test_toast_failure_does_not_raise(fleet_db, tmp_path, monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("no powershell / BurntToast here")

    monkeypatch.setattr(deadman, "_send_toast", _boom)

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=40))  # stale -> alert
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            _doctor_pass(cur, NOW - dt.timedelta(minutes=5))
        conn.commit()

        # Must not raise even though _send_toast blows up.
        alerts = deadman.run_deadman(conn, now=NOW, alert_dir=tmp_path)

    assert any(a.kind == "silent_death" for a in alerts)


def test_healthy_run_never_calls_toast(fleet_db, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(deadman, "_send_toast", lambda *a, **k: calls.append((a, k)))

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _arm(cur)
            _heartbeat(cur, "apply-worker-1", NOW - dt.timedelta(minutes=5))
            _heartbeat(cur, "watchdog-1", NOW - dt.timedelta(minutes=5))
            _doctor_pass(cur, NOW - dt.timedelta(minutes=5))
        conn.commit()

        alerts = deadman.run_deadman(conn, now=NOW, alert_dir=tmp_path)

    assert alerts == []
    assert calls == []
