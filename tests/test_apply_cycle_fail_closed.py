from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest
from typer.testing import CliRunner


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("readiness_exit", [1, 2, 3, 127])
def test_apply_cycle_never_falls_through_after_nonzero_readiness(tmp_path, readiness_exit):
    register = ROOT / "register-apply-cycle.ps1"
    wrapper = tmp_path / "generated-apply-cycle.ps1"
    readiness = tmp_path / "readiness.ps1"
    later_stage = tmp_path / "later-stage.ps1"
    sentinel = tmp_path / "later-stage-ran.txt"
    readiness.write_text("exit [int]$env:INJECTED_READINESS_EXIT\n", encoding="utf-8")
    escaped_sentinel = str(sentinel).replace("'", "''")
    later_stage.write_text(
        f"Set-Content -LiteralPath '{escaped_sentinel}' -Value ran\n",
        encoding="utf-8",
    )

    rendered = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(register),
            "-RenderApplyCycleWrapper",
            str(wrapper),
            "-ReadinessCommand",
            str(readiness),
            "-PostReadinessCommand",
            str(later_stage),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert rendered.returncode == 0, rendered.stderr

    env = os.environ.copy()
    env["INJECTED_READINESS_EXIT"] = str(readiness_exit)
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-File", str(wrapper)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == readiness_exit
    assert not sentinel.exists()
    assert f"readiness denied with exit {readiness_exit}" in result.stderr


def test_local_apply_url_is_rejected_while_emergency_hold_is_active(monkeypatch):
    from applypilot import cli

    bootstrap_calls = []
    monkeypatch.setattr(cli, "_bootstrap", lambda: bootstrap_calls.append(True))

    result = CliRunner().invoke(cli.app, ["apply", "--url", "https://example.invalid/job"])

    assert result.exit_code == 78
    assert "APPLYPILOT_ADMISSION_DENIED:EMERGENCY_HOLD" in result.output
    assert "emergency acquisition hold" in result.output.lower()
    assert bootstrap_calls == []


def test_direct_launcher_entry_is_denied_before_queue_mutation(monkeypatch):
    from applypilot.apply import launcher

    monkeypatch.setenv(
        "FLEET_PG_DSN",
        "postgresql://fleet_worker:wrong@127.0.0.1:1/unavailable?connect_timeout=1",
    )

    with pytest.raises(SystemExit) as exc_info:
        launcher.main(target_url="https://example.invalid/job")

    assert "control database unavailable" in str(exc_info.value)


def test_database_url_is_not_a_fleet_dsn_fallback(monkeypatch):
    from applypilot.apply import pgqueue

    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    monkeypatch.delenv("APPLYPILOT_FLEET_DSN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://ambiguous.invalid/db")

    with pytest.raises(RuntimeError, match="FLEET_PG_DSN"):
        pgqueue.get_dsn()


def test_applypilot_fleet_dsn_alias_is_not_accepted(monkeypatch):
    from applypilot.apply import pgqueue

    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", "postgresql://retired.invalid/control")

    with pytest.raises(RuntimeError, match="FLEET_PG_DSN") as exc_info:
        pgqueue.get_dsn()

    assert "APPLYPILOT_FLEET_DSN" not in str(exc_info.value)


def test_explicit_positional_fleet_dsn_wins_without_reading_ambiguous_environment(monkeypatch):
    from applypilot.apply import pgqueue

    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://fleet.invalid/control")
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", "postgresql://retired.invalid/control")

    assert pgqueue.get_dsn("host=localhost dbname=fleet") == "host=localhost dbname=fleet"
