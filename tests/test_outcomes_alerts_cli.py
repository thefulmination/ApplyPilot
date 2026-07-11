from typer.testing import CliRunner

import applypilot.cli as cli
from applypilot import database
import applypilot.outcome_scan as S

runner = CliRunner()


def _seed(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, source_board, apply_status, applied_at) "
        "VALUES (?,?,?,?,?,?)",
        ("https://acme/1", "Quant", "Acme", "greenhouse", "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.commit()
    S.upsert_email_event(conn, dict(
        message_id="m1", thread_id="t1", job_url="https://acme/1",
        occurred_at="2026-06-10T00:00:00+00:00", sender="x@acme.com", sender_domain="acme.com",
        subject="Interview", stage="interview", outcome=None, reason=None,
        title="Quant", company="Acme", match_method="company_name", match_score=0.9,
        confidence="high", body_text="body", snippet="body", extracted_by="llm",
        scanned_at="2026-06-29T00:00:00+00:00"))


def test_outcomes_alerts_digest_writes_files(monkeypatch, tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(database, "get_connection", lambda *a, **k: conn)

    out = tmp_path / "digest"
    result = runner.invoke(cli.app, ["outcomes-alerts", "digest", "--output", str(out)])
    assert result.exit_code == 0
    assert (out / "outcome_digest.txt").exists()
