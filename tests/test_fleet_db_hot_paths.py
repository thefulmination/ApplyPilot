from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import machine_blackout
from applypilot.fleet import schema as fleet_schema


ROOT = Path(__file__).resolve().parents[1]


class _ReadOnlyConnection:
    def __init__(self) -> None:
        self.read_only = False


def test_blackout_poll_does_not_migrate_and_marks_connection_read_only(monkeypatch, capsys):
    conn = _ReadOnlyConnection()
    monkeypatch.setenv("FLEET_PG_DSN", "fleet-test-dsn")
    monkeypatch.delenv("APPLYPILOT_FLEET_DSN", raising=False)
    monkeypatch.setattr(pgqueue, "connect", lambda dsn: conn if dsn == "fleet-test-dsn" else None)
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


@pytest.mark.parametrize("variable", ["FLEET_PG_DSN", "APPLYPILOT_FLEET_DSN"])
def test_blackout_poll_accepts_each_fleet_dsn(variable, monkeypatch, capsys):
    conn = _ReadOnlyConnection()
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    monkeypatch.delenv("APPLYPILOT_FLEET_DSN", raising=False)
    monkeypatch.setenv(variable, "fleet-test-dsn")
    monkeypatch.setattr(pgqueue, "connect", lambda dsn: conn if dsn == "fleet-test-dsn" else None)
    monkeypatch.setattr(
        machine_blackout,
        "status_line",
        lambda status_conn, label, *, role: f"OK|{label}|{role}|||",
    )
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    assert capsys.readouterr().out.strip() == "OK|m4|compute|||"


def test_blackout_poll_rejects_database_url_without_fleet_dsn(monkeypatch, capsys):
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    monkeypatch.delenv("APPLYPILOT_FLEET_DSN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://wrong-database.invalid/app")
    monkeypatch.setattr(
        pgqueue,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("DATABASE_URL reached the fleet connector"),
    )
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    captured = capsys.readouterr()
    assert captured.out.strip() == "KEEP|m4|compute|||error"
    assert "No fleet Postgres DSN" in captured.err


def test_blackout_poll_rejects_conflicting_fleet_dsns(monkeypatch, capsys):
    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://fleet-one.invalid/app")
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", "postgresql://fleet-two.invalid/app")
    monkeypatch.setattr(
        pgqueue,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("conflicting fleet DSNs reached connector"),
    )
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    captured = capsys.readouterr()
    assert captured.out.strip() == "KEEP|m4|compute|||error"
    assert "Inconsistent fleet Postgres DSN references" in captured.err


def test_blackout_poll_returns_keep_when_status_query_fails(monkeypatch, capsys):
    conn = _ReadOnlyConnection()
    monkeypatch.setenv("FLEET_PG_DSN", "fleet-test-dsn")
    monkeypatch.delenv("APPLYPILOT_FLEET_DSN", raising=False)
    monkeypatch.setattr(pgqueue, "connect", lambda _dsn: conn)

    def fail_status_query(status_conn, _label, *, role):
        assert status_conn.read_only is True
        assert role == "compute"
        raise RuntimeError("status query failed")

    monkeypatch.setattr(machine_blackout, "status_line", fail_status_query)
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    captured = capsys.readouterr()
    assert captured.out.strip() == "KEEP|m4|compute|||error"
    assert "status query failed" in captured.err
