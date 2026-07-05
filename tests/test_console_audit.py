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


def test_console_audit_rows_are_read_only_bounded_and_scrubbed(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        for i in range(30):
            console_app._audit_action(
                conn,
                action=f"action-{i} token sk-test-{i}",
                ok=(i % 2 == 0),
                message="done password topsecret " + ("x" * 600),
                lane="ats secret lane-secret",
                target="https://example.test/apply?api_key=abc123&" + ("target" * 100),
            )

    payload = console_app.audit_rows()
    rows = payload["rows"]

    assert payload["schema_missing"] is False
    assert len(rows) == 25
    assert rows[0]["action"].startswith("action-29")
    assert set(rows[0]) == {"time", "action", "ok", "result", "message", "lane", "target"}
    assert rows[0]["result"] == "failed"
    assert len(rows[0]["action"]) <= 120
    assert len(rows[0]["message"]) <= 500
    assert len(rows[0]["lane"]) <= 120
    assert len(rows[0]["target"]) <= 300
    combined = " ".join(
        str(v).lower()
        for row in rows
        for v in row.values()
        if v is not None
    )
    assert "topsecret" not in combined
    assert "sk-test" not in combined
    assert "lane-secret" not in combined
    assert "abc123" not in combined


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


def test_console_action_audit_failure_does_not_mask_success(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    def fake_success(conn, body):
        return "done"

    def audit_fails(conn, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setitem(console_app._ACTIONS, "fake_success_no_audit", fake_success)
    monkeypatch.setattr(console_app, "_audit_action", audit_fails)

    assert console_app.run_action({"action": "fake_success_no_audit"}) == (True, "done")


def test_console_action_audit_failure_does_not_mask_tuple_failure(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    def fake_failure(conn, body):
        return False, "unknown lane"

    def audit_fails(conn, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setitem(console_app._ACTIONS, "fake_failure_no_audit", fake_failure)
    monkeypatch.setattr(console_app, "_audit_action", audit_fails)

    assert console_app.run_action({"action": "fake_failure_no_audit"}) == (False, "unknown lane")


def test_console_action_unknown_action_audit_failure_still_returns_unknown(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    def audit_fails(conn, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setattr(console_app, "_audit_action", audit_fails)

    assert console_app.run_action({"action": "does_not_exist"}) == (False, "unknown action")


def test_console_action_unknown_action_connect_failure_still_returns_unknown(monkeypatch):
    def connect_fails(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(console_app.pgqueue, "connect", connect_fails)

    assert console_app.run_action({"action": "does_not_exist"}) == (False, "unknown action")


def test_console_action_exception_audit_failure_reraises_original(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    def fake_raises(conn, body):
        raise ValueError("original failure")

    def audit_fails(conn, **kwargs):
        raise RuntimeError("audit unavailable")

    monkeypatch.setitem(console_app._ACTIONS, "fake_raises_no_audit", fake_raises)
    monkeypatch.setattr(console_app, "_audit_action", audit_fails)

    with pytest.raises(ValueError, match="original failure"):
        console_app.run_action({"action": "fake_raises_no_audit"})


def test_console_audit_rows_degrades_when_audit_table_missing(fleet_db, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("DROP TABLE fleet_console_audit")
        conn.commit()

    result = console_app.audit_rows()

    assert result == {
        "rows": [],
        "schema_missing": True,
        "reason": "Console audit table is not installed; apply the fleet v3 schema migration.",
    }
