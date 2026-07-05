from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_app


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
