from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

import applypilot.cli as cli

runner = CliRunner()


def _setup(monkeypatch, tmp_path: Path):
    from applypilot import database

    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(database, "get_connection", lambda *a, **k: conn)
    return conn


def _insert_job(conn, url, *, application_url=None, applied_at=None,
                 title="Engineer", site="Co", full_description="desc"):
    conn.execute(
        "INSERT INTO jobs (url, application_url, title, site, full_description, applied_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (url, application_url, title, site, full_description, applied_at),
    )
    conn.commit()


def test_set_then_list_shows_tenant(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    r = runner.invoke(cli.app, ["tenants", "set", "foo.com", "supervised"])
    assert r.exit_code == 0, r.stdout

    r = runner.invoke(cli.app, ["tenants", "list"])
    assert r.exit_code == 0, r.stdout
    assert "foo.com" in r.stdout
    assert "supervised" in r.stdout


def test_bare_tenants_defaults_to_list(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    from applypilot import tenants
    tenants.set_tenant(conn, "bare.com", "supervised")

    r = runner.invoke(cli.app, ["tenants"])
    assert r.exit_code == 0, r.stdout
    assert "bare.com" in r.stdout


def test_set_trusted_without_evidence_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    r = runner.invoke(cli.app, ["tenants", "set", "foo.com", "trusted"])
    assert r.exit_code == 1
    assert "clean submits" in r.stdout or "force" in r.stdout


def test_set_trusted_with_force_succeeds(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    r = runner.invoke(cli.app, ["tenants", "set", "foo.com", "trusted", "--force"])
    assert r.exit_code == 0, r.stdout

    r = runner.invoke(cli.app, ["tenants", "list"])
    assert "trusted" in r.stdout


def test_set_invalid_status_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    r = runner.invoke(cli.app, ["tenants", "set", "foo.com", "bogus"])
    assert r.exit_code == 1
    assert "invalid status" in r.stdout


def test_halt_sets_halted_until_and_list_shows_it(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)

    r = runner.invoke(cli.app, ["tenants", "halt", "foo.com"])
    assert r.exit_code == 0, r.stdout

    from applypilot import tenants
    assert tenants.is_halted(conn, "foo.com", "2000-01-01T00:00:00+00:00")

    r = runner.invoke(cli.app, ["tenants", "list"])
    assert r.exit_code == 0
    assert "foo.com" in r.stdout
    assert "yes" in r.stdout.lower()


def test_list_eligible_job_count(monkeypatch, tmp_path):
    conn = _setup(monkeypatch, tmp_path)
    from applypilot import tenants
    tenants.set_tenant(conn, "foo.com", "supervised")

    _insert_job(conn, "https://foo.com/j1")
    _insert_job(conn, "https://foo.com/j2", applied_at="2026-06-01T00:00:00+00:00")
    _insert_job(conn, "https://other.com/j3", application_url="https://foo.com/apply/3")

    r = runner.invoke(cli.app, ["tenants", "list"])
    assert r.exit_code == 0, r.stdout
    # Two eligible: j1 (url host match, not applied) and j3 (application_url host
    # match, not applied). j2 is applied and excluded.
    assert "foo.com" in r.stdout
