from typer.testing import CliRunner

import applypilot.cli as cli
from applypilot import database
import applypilot.outcome_scan as S

runner = CliRunner()


def _seed(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, source_board, location, salary, "
        "fit_score, apply_status, applied_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("https://acme/1", "Senior Quant Analyst", "Acme", "greenhouse", "Remote",
         "$210,000", 8, "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.commit()
    S.upsert_email_event(
        conn,
        dict(message_id="m1", thread_id="t1", job_url=None,
             occurred_at="2026-06-12T00:00:00+00:00", sender="talent@acme.com",
             sender_domain="acme.com", subject="Quick intro", stage="screen",
             outcome=None, reason=None, title="Senior Quant Analyst", company="Acme",
             match_method=None, match_score=None, confidence="medium",
             body_text="screen request", snippet="screen", extracted_by="llm",
             scanned_at="2026-06-29T00:00:00+00:00", match_status="needs_review",
             match_reason="ambiguous_company"),
    )


def test_outcomes_review_queue_renders_rows(monkeypatch, tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(database, "get_connection", lambda *a, **k: conn)

    result = runner.invoke(cli.app, ["outcomes-review", "queue"])
    assert result.exit_code == 0
    assert "m1" in result.stdout
    assert "needs_review" in result.stdout


def test_outcomes_review_resolve_trusted(monkeypatch, tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(database, "get_connection", lambda *a, **k: conn)

    result = runner.invoke(
        cli.app,
        [
            "outcomes-review", "resolve", "m1", "--resolution", "trusted",
            "--job-url", "https://acme/1",
        ],
    )
    assert result.exit_code == 0

    from applypilot.outcome_review import build_effective_events

    row = next(r for r in build_effective_events(conn) if r["message_id"] == "m1")
    assert row["trust_state"] == "trusted"
    outcome = conn.execute(
        "SELECT job_url, review_status FROM reviewed_outcomes WHERE event_id = 'm1'"
    ).fetchone()
    assert tuple(outcome) == ("https://acme/1", "accepted")


def test_outcomes_review_resolve_ignored(monkeypatch, tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(database, "get_connection", lambda *a, **k: conn)

    result = runner.invoke(
        cli.app,
        ["outcomes-review", "resolve", "m1", "--resolution", "ignored"],
    )
    assert result.exit_code == 0

    from applypilot.outcome_review import list_review_queue

    assert list_review_queue(conn) == []
