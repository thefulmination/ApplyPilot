from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue  # noqa: E402
from applypilot.fleet import machine_blackout, machine_blackout_main  # noqa: E402
from applypilot.fleet import schema as fleet_schema  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]


class _ReadOnlyConnection:
    def __init__(self) -> None:
        self.read_only = False

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def test_blackout_poll_does_not_migrate_and_marks_connection_read_only(monkeypatch, capsys):
    conn = _ReadOnlyConnection()
    monkeypatch.setenv("FLEET_PG_DSN", "fleet-test-dsn")
    monkeypatch.setattr(pgqueue, "connect", lambda _dsn: conn)
    monkeypatch.setattr(
        fleet_schema,
        "ensure_schema_v3",
        lambda *_args, **_kwargs: pytest.fail("blackout poll attempted schema migration"),
    )

    def status_line(status_conn, label, *, role):
        assert status_conn is conn
        assert status_conn.read_only is True
        return f"OK|{label}|{role}|||"

    monkeypatch.setattr(machine_blackout, "status_line", status_line)
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    assert capsys.readouterr().out.strip() == "OK|m4|compute|||"


def test_blackout_poll_returns_keep_when_read_only_setup_fails(monkeypatch, capsys):
    class Connection:
        @property
        def read_only(self):
            return False

        @read_only.setter
        def read_only(self, _value):
            raise RuntimeError("read-only unavailable")

    monkeypatch.setenv("FLEET_PG_DSN", "fleet-test-dsn")
    monkeypatch.setattr(pgqueue, "connect", lambda _dsn: Connection())
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    captured = capsys.readouterr()
    assert captured.out.strip() == "KEEP|m4|compute|||error"
    assert "read-only unavailable" in captured.err


def test_blackout_poll_query_runs_in_read_only_transaction(fleet_db, monkeypatch, capsys):
    transaction_modes: list[str] = []
    original = machine_blackout.status_line

    def checked_status_line(conn, label, *, role):
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            transaction_modes.append(cur.fetchone()["transaction_read_only"])
        return original(conn, label, role=role)

    monkeypatch.setattr(machine_blackout, "status_line", checked_status_line)
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    assert capsys.readouterr().out.strip() == "OK|m4|compute|||"
    assert transaction_modes == ["on"]


def test_control_status_does_not_migrate_and_marks_connection_read_only(monkeypatch, capsys):
    conn = _ReadOnlyConnection()
    monkeypatch.setattr(pgqueue, "connect", lambda: conn)
    monkeypatch.setattr(
        fleet_schema,
        "ensure_schema_v3",
        lambda *_args, **_kwargs: pytest.fail("status command attempted schema migration"),
    )
    monkeypatch.setattr(
        machine_blackout,
        "status_line",
        lambda status_conn, label, *, role: (
            f"OK|{label}|{role}|||" if status_conn.read_only else pytest.fail("status connection is writable")
        ),
    )

    rc = machine_blackout_main.main(["status", "--label", "m4", "--role", "compute"])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "OK|m4|compute|||"


def test_desired_state_poll_marks_connection_read_only(monkeypatch, capsys):
    class Cursor:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            assert self.conn.read_only is True

        def fetchone(self):
            return None

    class Connection:
        def __init__(self):
            self.read_only = False

        def cursor(self):
            return Cursor(self)

    monkeypatch.setenv("FLEET_PG_DSN", "fleet-test-dsn")
    monkeypatch.delenv("APPLYPILOT_FLEET_DSN", raising=False)
    monkeypatch.setattr(pgqueue, "connect", lambda _dsn: Connection())
    monkeypatch.setattr(sys, "argv", ["fleet-agent-query.py", "m4"])

    runpy.run_path(str(ROOT / "fleet-agent-query.py"), run_name="__main__")

    assert capsys.readouterr().out.strip() == "STOP|||"
