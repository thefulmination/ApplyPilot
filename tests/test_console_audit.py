from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_app


def _latest_audit_row(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action, ok, message, lane, target "
                "FROM fleet_console_audit ORDER BY id DESC LIMIT 1"
            )
            return cur.fetchone()


def test_console_action_audit_records_success_without_secrets(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        ok, msg = console_app.run_action({"action": "pause"})
        assert ok is True
        with conn.cursor() as cur:
            cur.execute("SELECT action, ok, message FROM fleet_console_audit ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()

    assert row["action"] == "pause"
    assert row["ok"] is True
    assert "dsn" not in (row["message"] or "").lower()
    assert "token" not in (row["message"] or "").lower()


def test_console_action_audit_records_unknown_action(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    ok, msg = console_app.run_action({"action": "does_not_exist"})
    assert ok is False
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT action, ok, message FROM fleet_console_audit ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()

    assert row["action"] == "does_not_exist"
    assert row["ok"] is False


def test_console_action_audit_scrubs_and_bounds_lane_and_target(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    def fake_success(conn, body):
        return "done"

    monkeypatch.setitem(console_app._ACTIONS, "fake_success", fake_success)
    long_lane = "ats password=topsecret token=sk-test " + ("lane" * 60)
    long_target = "https://example.test/apply?password=topsecret&token=sk-test&" + ("target" * 80)

    ok, msg = console_app.run_action({
        "action": "fake_success",
        "lane": long_lane,
        "url": long_target,
    })

    assert ok is True
    row = _latest_audit_row(fleet_db)
    assert row["action"] == "fake_success"
    assert row["ok"] is True
    assert row["lane"] is not None
    assert row["target"] is not None
    assert len(row["lane"]) <= 120
    assert len(row["target"]) <= 300
    combined = f"{row['lane']} {row['target']}".lower()
    assert "topsecret" not in combined
    assert "sk-test" not in combined


def test_console_action_audit_records_known_action_exception(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    def fake_raises(conn, body):
        raise ValueError("password=topsecret token sk-test")

    monkeypatch.setitem(console_app._ACTIONS, "fake_raises", fake_raises)

    with pytest.raises(ValueError, match="password=topsecret token sk-test"):
        console_app.run_action({"action": "fake_raises", "lane": "ats"})

    row = _latest_audit_row(fleet_db)
    assert row is not None
    assert row["action"] == "fake_raises"
    assert row["ok"] is False
    assert row["message"] is not None
    assert "topsecret" not in row["message"].lower()
    assert "sk-test" not in row["message"].lower()


def test_console_action_audit_records_tuple_failure(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    def fake_failure(conn, body):
        return False, "unknown lane"

    monkeypatch.setitem(console_app._ACTIONS, "fake_failure", fake_failure)

    ok, msg = console_app.run_action({"action": "fake_failure"})

    assert ok is False
    assert msg == "unknown lane"
    row = _latest_audit_row(fleet_db)
    assert row["action"] == "fake_failure"
    assert row["ok"] is False
    assert row["message"] == "unknown lane"
