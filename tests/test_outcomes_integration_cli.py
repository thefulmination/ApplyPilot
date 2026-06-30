# tests/test_outcomes_integration_cli.py
from typer.testing import CliRunner

import applypilot.cli as cli
from applypilot import database
import applypilot.outcome_scan as S

runner = CliRunner()


def _seed_rejected(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, source_board, apply_status, applied_at) "
        "VALUES (?,?,?,?,?,?)",
        ("https://acme/1", "Quant", "Acme", "greenhouse", "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.commit()
    S.upsert_email_event(conn, dict(
        message_id="m2", thread_id="t1", job_url="https://acme/1",
        occurred_at="2026-06-10T00:00:00+00:00", sender="x@acme.com", sender_domain="acme.com",
        subject="Decision", stage="rejected", outcome="rejected", reason="went another direction",
        title="Quant", company="Acme", match_method="company_name", match_score=0.9,
        confidence="high", body_text="b", snippet="b", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"))


def test_outcomes_promote_is_preview_only(monkeypatch, tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_rejected(conn)
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.outcome_dashboard._read_only_conn", lambda *a, **k: conn)
    result = runner.invoke(cli.app, ["outcomes-promote"])
    assert result.exit_code == 0
    assert "rejected" in result.stdout.lower()
    # The command must not have written to the applications tracker.
    assert conn.execute("SELECT COUNT(*) FROM applications").fetchone()[0] == 0
    # And there is no --apply option.
    help_res = runner.invoke(cli.app, ["outcomes-promote", "--help"])
    assert "--apply" not in help_res.stdout


def test_outcomes_lanes_runs(monkeypatch, tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_rejected(conn)
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr("applypilot.outcome_dashboard._read_only_conn", lambda *a, **k: conn)
    result = runner.invoke(cli.app, ["outcomes-lanes"])
    assert result.exit_code == 0
