"""apply --auth-gated entry + same-day halt on challenge (auth-gated-tenant-lane
Task 5).

Supervised design note (owner decision 2026-07-03, amendment 0b2fead): there
is NO confirm-before-submit pause. `--auth-gated` just needs to: set
APPLYPILOT_AUTH_GATED_MODE=supervised, force headed, force home-box, scope to
--tenant, and pass supervised=True to run_job so record_tenant_outcome fires
on the real terminal status.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

import applypilot.cli as cli

runner = CliRunner()


def _setup(monkeypatch, tmp_path: Path):
    from applypilot import database

    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(database, "get_connection", lambda *a, **k: conn)
    # cli.py's --auth-gated path writes os.environ directly (real usage needs
    # a persistent process env, not a monkeypatch-scoped one) -- delenv here
    # so a prior test's mode/tenant-scope doesn't leak into the next test.
    monkeypatch.delenv("APPLYPILOT_AUTH_GATED_MODE", raising=False)
    monkeypatch.delenv("APPLYPILOT_AUTH_GATED_TENANT_HOST", raising=False)
    return conn


def test_auth_gated_no_enabled_tenants_prints_hint_and_exits_zero(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    # Guard: acquire/apply loop must never be entered when there are zero
    # supervised/trusted tenants -- stub it to blow up if called.
    def _boom(*a, **k):
        raise AssertionError("apply loop must not run with zero enabled tenants")

    monkeypatch.setattr("applypilot.apply.launcher.main", _boom)

    r = runner.invoke(cli.app, ["apply", "--auth-gated"])

    assert r.exit_code == 0, r.stdout
    assert "supervised" in r.stdout.lower() or "trusted" in r.stdout.lower()
    assert "tenants set" in r.stdout


def test_auth_gated_with_supervised_tenant_runs_and_sets_mode(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    from applypilot import tenants

    tenants.set_tenant(conn, "acme.myworkdayjobs.com", "supervised")

    calls = {}

    def _fake_main(**kwargs):
        import os
        calls["kwargs"] = kwargs
        calls["mode_env"] = os.environ.get("APPLYPILOT_AUTH_GATED_MODE")

    monkeypatch.setattr("applypilot.apply.launcher.main", _fake_main)
    # Skip the heavier preflight/tier/profile/resume/canary machinery -- not
    # this test's concern.
    import applypilot.config as config
    monkeypatch.setattr(config, "check_tier", lambda *a, **k: None)
    monkeypatch.setattr(type(config.PROFILE_PATH), "exists", lambda self: True, raising=False)
    monkeypatch.setattr(type(config.RESUME_PDF_PATH), "exists", lambda self: True, raising=False)

    import os
    try:
        r = runner.invoke(cli.app, [
            "apply", "--auth-gated", "--tenant", "acme.myworkdayjobs.com",
            "--base-resume", "--skip-preflight",
        ])
    finally:
        # cli.py's --auth-gated path writes os.environ directly (by design,
        # for a real long-running process) -- clean up so this test's mode/
        # tenant-scope never leaks into an unrelated test in the same session.
        os.environ.pop("APPLYPILOT_AUTH_GATED_MODE", None)
        os.environ.pop("APPLYPILOT_AUTH_GATED_TENANT_HOST", None)

    assert r.exit_code == 0, r.stdout
    assert calls.get("mode_env") == "supervised"
    assert calls["kwargs"].get("headless") is False
    assert calls["kwargs"].get("supervised") is True


# ---------------------------------------------------------------------------
# handle_auth_gated_result
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", ["captcha", "login_issue", "auth_required"])
def test_handle_auth_gated_result_halts_on_challenge(monkeypatch, status):
    from applypilot.apply import launcher as L

    calls = {}

    def _fake_halt(conn, host, until_iso):
        calls["host"] = host
        calls["until_iso"] = until_iso

    monkeypatch.setattr(L.tenants_mod, "halt_tenant", _fake_halt)

    halted = L.handle_auth_gated_result(object(), "acme.myworkdayjobs.com", status)

    assert halted is True
    assert calls["host"] == "acme.myworkdayjobs.com"
    assert calls["until_iso"]  # non-empty ISO string


@pytest.mark.parametrize("status", ["applied", "failed:other", "expired"])
def test_handle_auth_gated_result_no_halt_on_non_challenge(monkeypatch, status):
    from applypilot.apply import launcher as L

    calls = {}

    def _fake_halt(conn, host, until_iso):
        calls["called"] = True

    monkeypatch.setattr(L.tenants_mod, "halt_tenant", _fake_halt)

    halted = L.handle_auth_gated_result(object(), "acme.myworkdayjobs.com", status)

    assert halted is False
    assert "called" not in calls
