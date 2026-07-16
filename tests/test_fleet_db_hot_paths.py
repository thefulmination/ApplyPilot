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


def test_blackout_poll_returns_keep_when_read_only_setup_fails(monkeypatch, capsys):
    class Connection:
        @property
        def read_only(self):
            return False

        @read_only.setter
        def read_only(self, _value):
            raise RuntimeError("read-only unavailable")

    monkeypatch.setenv("FLEET_PG_DSN", "fleet-test-dsn")
    monkeypatch.delenv("APPLYPILOT_FLEET_DSN", raising=False)
    monkeypatch.setattr(pgqueue, "connect", lambda _dsn: Connection())
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    captured = capsys.readouterr()
    assert captured.out.strip().startswith("BLOCKED|m4|compute|blackout-query-error||")
    assert "read-only unavailable" in captured.err


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
    assert captured.out.strip().startswith("BLOCKED|m4|compute|blackout-query-error||")
    assert "No fleet Postgres DSN" in captured.err


@pytest.mark.parametrize(
    "applypilot_fleet_dsn",
    [
        "postgresql://worker:other-secret@fleet-two.invalid:5432/applypilot",
        "postgresql://worker:other-secret@fleet-one.invalid:5433/applypilot",
        "postgresql://worker:other-secret@fleet-one.invalid:5432/other_database",
        "postgresql://other-user:other-secret@fleet-one.invalid:5432/applypilot",
        "postgresql://worker:different-secret@fleet-one.invalid:5432/applypilot",
    ],
)
def test_blackout_poll_rejects_conflicting_fleet_dsns(
    applypilot_fleet_dsn,
    monkeypatch,
    capsys,
):
    fleet_secret = "fleet-secret"
    monkeypatch.setenv(
        "FLEET_PG_DSN",
        f"postgresql://worker:{fleet_secret}@fleet-one.invalid:5432/applypilot",
    )
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", applypilot_fleet_dsn)
    monkeypatch.setattr(
        pgqueue,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("conflicting fleet DSNs reached connector"),
    )
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    captured = capsys.readouterr()
    assert captured.out.strip().startswith("BLOCKED|m4|compute|blackout-query-error||")
    assert "Inconsistent fleet Postgres DSN references" in captured.err
    assert fleet_secret not in captured.out
    assert fleet_secret not in captured.err
    assert "other-secret" not in captured.out
    assert "other-secret" not in captured.err
    assert "different-secret" not in captured.out
    assert "different-secret" not in captured.err


def test_blackout_poll_does_not_equate_omitted_host_with_localhost(monkeypatch, capsys):
    secret = "socket-secret"
    monkeypatch.delenv("PGHOST", raising=False)
    monkeypatch.setenv(
        "FLEET_PG_DSN",
        f"port=5432 dbname=applypilot user=worker password={secret}",
    )
    monkeypatch.setenv(
        "APPLYPILOT_FLEET_DSN",
        f"postgresql://worker:{secret}@localhost:5432/applypilot",
    )
    monkeypatch.setattr(
        pgqueue,
        "connect",
        lambda *_args, **_kwargs: pytest.fail("different destinations reached connector"),
    )
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    captured = capsys.readouterr()
    assert captured.out.strip().startswith("BLOCKED|m4|compute|blackout-query-error||")
    assert "Inconsistent fleet Postgres DSN references" in captured.err
    assert secret not in captured.out
    assert secret not in captured.err


def test_blackout_poll_query_runs_in_read_only_transaction(fleet_db, monkeypatch, capsys):
    transaction_modes: list[str] = []
    original = machine_blackout.status_line
    monkeypatch.setenv("FLEET_PG_DSN", fleet_db)
    monkeypatch.delenv("APPLYPILOT_FLEET_DSN", raising=False)

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


def test_control_status_query_runs_in_read_only_transaction_without_migration(
    fleet_db,
    monkeypatch,
    capsys,
):
    transaction_modes: list[str] = []
    original = machine_blackout.status_line
    monkeypatch.setattr(
        fleet_schema,
        "ensure_schema_v3",
        lambda *_args, **_kwargs: pytest.fail("control status attempted schema migration"),
    )

    def checked_status_line(conn, label, *, role):
        with conn.cursor() as cur:
            cur.execute("SHOW transaction_read_only")
            transaction_modes.append(cur.fetchone()["transaction_read_only"])
        return original(conn, label, role=role)

    monkeypatch.setattr(machine_blackout, "status_line", checked_status_line)

    rc = machine_blackout_main.main(["status", "--label", "m4", "--role", "compute"])

    assert rc == 0
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


@pytest.mark.parametrize(
    ("fleet_dsn", "applypilot_fleet_dsn"),
    [
        (
            "host=fleet.example port=5432 dbname=applypilot user=worker password=secret connect_timeout=5",
            "connect_timeout=5 password=secret user=worker dbname=applypilot port=5432 host=fleet.example",
        ),
        (
            "postgresql://worker:secret@fleet.example:5432/applypilot?connect_timeout=5",
            "host=fleet.example port=5432 dbname=applypilot user=worker password=secret connect_timeout=5",
        ),
        (
            "host=fleet.example dbname=applypilot user=worker password=secret",
            "postgresql://worker:secret@fleet.example:5432/applypilot",
        ),
        (
            "host=fleet.example dbname=applypilot user=worker password=secret",
            (
                "host=fleet.example port=5432 dbname=applypilot user=worker password=secret "
                "sslmode=prefer target_session_attrs=any"
            ),
        ),
        (
            "host=fleet.example user=worker password=secret",
            "host=fleet.example user=worker password=secret dbname=worker",
        ),
        (
            "postgresql://worker:secret@fleet.example",
            "postgresql://worker:secret@fleet.example/worker",
        ),
    ],
)
def test_blackout_poll_accepts_semantically_equivalent_fleet_dsns(
    fleet_dsn,
    applypilot_fleet_dsn,
    monkeypatch,
    capsys,
):
    conn = _ReadOnlyConnection()
    for variable in ("PGPORT", "PGSSLMODE", "PGTARGETSESSIONATTRS"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("FLEET_PG_DSN", fleet_dsn)
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", applypilot_fleet_dsn)
    monkeypatch.setattr(pgqueue, "connect", lambda dsn: conn if dsn == fleet_dsn else None)
    monkeypatch.setattr(
        machine_blackout,
        "status_line",
        lambda status_conn, label, *, role: f"OK|{label}|{role}|||",
    )
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    captured = capsys.readouterr()
    assert captured.out.strip() == "OK|m4|compute|||"
    assert captured.err == ""


@pytest.mark.parametrize("failure_point", ["connect", "query", "schema"])
def test_blackout_poll_returns_sanitized_blocked_when_database_access_fails(
    failure_point,
    monkeypatch,
    capsys,
):
    conn = _ReadOnlyConnection()
    secret = "super-secret-password"
    dsn = f"postgresql://worker:{secret}@fleet.invalid/applypilot"
    monkeypatch.setenv("FLEET_PG_DSN", dsn)
    monkeypatch.delenv("APPLYPILOT_FLEET_DSN", raising=False)

    def connect(_dsn):
        if failure_point == "connect":
            raise RuntimeError(f"could not connect using {dsn} password={secret}")
        return conn

    monkeypatch.setattr(pgqueue, "connect", connect)

    def fail_status_query(status_conn, _label, *, role):
        assert status_conn.read_only is True
        assert role == "compute"
        if failure_point in {"query", "schema"}:
            raise RuntimeError(f"{failure_point} access failed for password={secret}")
        return "OK|m4|compute|||"

    monkeypatch.setattr(machine_blackout, "status_line", fail_status_query)
    monkeypatch.setattr(sys, "argv", ["fleet-blackout-query.py", "m4", "compute"])

    runpy.run_path(str(ROOT / "fleet-blackout-query.py"), run_name="__main__")

    captured = capsys.readouterr()
    assert captured.out.strip().startswith("BLOCKED|m4|compute|blackout-query-error||")
    assert "RuntimeError" in captured.err
    assert "***" in captured.err
    assert secret not in captured.out
    assert secret not in captured.err


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


def test_desired_state_poll_returns_stop_when_query_fails(monkeypatch, capsys):
    class Cursor:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args):
            assert self.conn.read_only is True
            raise RuntimeError("desired-state query failed")

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

    captured = capsys.readouterr()
    assert captured.out.strip() == "STOP|||"
    assert "control state unavailable" in captured.err
    assert "desired-state query failed" not in captured.err
