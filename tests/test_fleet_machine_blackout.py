from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue  # noqa: E402
from applypilot.fleet import machine_blackout, machine_blackout_main  # noqa: E402


def test_blackout_blocks_non_home_non_mac_until_expiry(fleet_db):
    now = datetime(2026, 7, 6, 16, 30, tzinfo=timezone.utc)
    until = now + timedelta(minutes=30)

    with pgqueue.connect(fleet_db) as conn:
        machine_blackout.create_blackout(
            conn,
            name="today-mac-only",
            expires_at=until,
            allow_patterns=["home", "mac", "mac-*"],
            block_patterns=["*"],
            reason="non-mac machines in use until 5pm",
            now=now,
        )

        assert machine_blackout.is_machine_allowed(conn, "home", now=now).allowed is True
        assert machine_blackout.is_machine_allowed(conn, "mac", now=now).allowed is True
        assert machine_blackout.is_machine_allowed(conn, "mac-0", now=now).allowed is True

        blocked = machine_blackout.is_machine_allowed(conn, "m4", now=now)
        assert blocked.allowed is False
        assert blocked.policy_name == "today-mac-only"
        assert blocked.reason == "non-mac machines in use until 5pm"


def test_blackout_expires_without_manual_clear(fleet_db):
    now = datetime(2026, 7, 6, 16, 30, tzinfo=timezone.utc)
    until = now + timedelta(minutes=30)

    with pgqueue.connect(fleet_db) as conn:
        machine_blackout.create_blackout(
            conn,
            name="today-mac-only",
            expires_at=until,
            allow_patterns=["home", "mac-*"],
            block_patterns=["*"],
            reason="temporary",
            now=now,
        )

        assert machine_blackout.is_machine_allowed(conn, "m2", now=now).allowed is False
        assert machine_blackout.is_machine_allowed(conn, "m2", now=until + timedelta(seconds=1)).allowed is True


def test_status_line_is_powershell_friendly(fleet_db):
    now = datetime(2026, 7, 6, 16, 30, tzinfo=timezone.utc)
    with pgqueue.connect(fleet_db) as conn:
        machine_blackout.create_blackout(
            conn,
            name="today-mac-only",
            expires_at=now + timedelta(minutes=30),
            allow_patterns=["home"],
            block_patterns=["*"],
            reason="blocked for workday",
            now=now,
        )
        line = machine_blackout.status_line(conn, "m4", role="compute", now=now)

    assert line.startswith("BLOCKED|m4|compute|today-mac-only|")
    assert line.endswith("|blocked for workday")


def test_control_cli_defaults_allow_home_and_mac_without_duplicates(fleet_db, monkeypatch, capsys):
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    controlled_now = datetime(2035, 7, 6, 16, 0, tzinfo=timezone.utc)
    until = controlled_now + timedelta(hours=1)

    rc = machine_blackout_main.main([
        "blackout",
        "--name",
        "default-policy",
        "--until",
        until.isoformat(),
        "--reason",
        "default allow list",
    ], now_fn=lambda: controlled_now)
    assert rc == 0

    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT allow_patterns, block_patterns FROM fleet_machine_blackout WHERE name='default-policy'")
            row = cur.fetchone()

    assert row["allow_patterns"] == ["home", "mac", "mac-*"]
    assert row["block_patterns"] == ["*"]
    assert "created|" in capsys.readouterr().out
