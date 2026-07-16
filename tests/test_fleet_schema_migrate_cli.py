from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
import tomllib

from applypilot.apply import pgqueue
from applypilot.fleet import schema as fleet_schema
from applypilot.fleet import schema_main


ROOT = Path(__file__).resolve().parents[1]


def test_migrate_schema_command_is_registered():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["scripts"]["applypilot-fleet-migrate-schema"] == (
        "applypilot.fleet.schema_main:main"
    )


def test_migrate_schema_requires_explicit_fleet_dsn(monkeypatch, capsys):
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    monkeypatch.delenv("APPLYPILOT_FLEET_DSN", raising=False)

    rc = schema_main.main([])

    captured = capsys.readouterr()
    assert rc != 0
    assert "No fleet Postgres DSN" in captured.err


def test_migrate_schema_uses_locked_schema_migrator(monkeypatch, capsys):
    conn = object()
    calls = []
    monkeypatch.setattr(pgqueue, "connect", lambda dsn: nullcontext(conn))
    monkeypatch.setattr(fleet_schema, "ensure_schema_v3", lambda value: calls.append(value))

    rc = schema_main.main(["--dsn", "postgresql://controller/test"])

    assert rc == 0
    assert calls == [conn]
    assert "migration complete" in capsys.readouterr().out


def test_migrate_schema_returns_nonzero_on_migration_failure(monkeypatch, capsys):
    monkeypatch.setattr(pgqueue, "connect", lambda _dsn: nullcontext(object()))
    monkeypatch.setattr(
        fleet_schema,
        "ensure_schema_v3",
        lambda _conn: (_ for _ in ()).throw(RuntimeError("migration rejected")),
    )

    rc = schema_main.main(["--dsn", "postgresql://controller/test"])

    assert rc != 0
    assert "migration rejected" in capsys.readouterr().err
