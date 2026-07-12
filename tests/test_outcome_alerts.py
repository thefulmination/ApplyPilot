import json

from applypilot import database
import applypilot.outcome_scan as S


def _seed(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, source_board, apply_status, applied_at) "
        "VALUES (?,?,?,?,?,?)",
        ("https://acme/1", "Quant", "Acme", "greenhouse", "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.commit()
    for row in [
        dict(message_id="m1", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-10T00:00:00+00:00", sender="x@acme.com", sender_domain="acme.com",
             subject="Interview", stage="interview", outcome=None, reason=None,
             title="Quant", company="Acme", match_method="company_name", match_score=0.9,
             confidence="high", body_text="body", snippet="body", extracted_by="llm",
             scanned_at="2026-06-29T00:00:00+00:00"),
        dict(message_id="m2", thread_id="t2", job_url=None,
             occurred_at="2026-06-11T00:00:00+00:00", sender="x@acme.com", sender_domain="acme.com",
             subject="Quick intro", stage="screen", outcome=None, reason=None,
             title="Quant", company="Acme", match_method=None, match_score=None,
             confidence="medium", body_text="body", snippet="body", extracted_by="llm",
             scanned_at="2026-06-29T00:00:00+00:00", match_status="needs_review",
             match_reason="ambiguous_company"),
        dict(message_id="m3", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-12T00:00:00+00:00", sender="x@acme.com", sender_domain="acme.com",
             subject="Interview reminder", stage="interview", outcome=None, reason=None,
             title="Quant", company="Acme", match_method="company_name", match_score=0.9,
             confidence="high", body_text="body", snippet="body", extracted_by="llm",
             scanned_at="2026-06-29T00:00:00+00:00"),
    ]:
        S.upsert_email_event(conn, row)
    from applypilot.outcome_review import record_review
    record_review(conn, "m1", resolution="trusted", reviewed_by="test")
    record_review(conn, "m3", resolution="trusted", reviewed_by="test")


def test_alert_builder_classifies_and_collapses_threads(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_alerts import build_alerts

    alerts = build_alerts(conn, now_iso="2026-06-20T00:00:00+00:00")
    assert len(alerts) == 2
    assert alerts[0]["severity"] == "critical"
    assert alerts[0]["message_id"] == "m3"
    assert alerts[1]["severity"] == "warning"


def test_write_digest_outputs_text_and_json(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_alerts import write_digest

    out = tmp_path / "digest"
    result = write_digest(conn, output_dir=out, now_iso="2026-06-20T00:00:00+00:00")
    assert (out / "outcome_digest.txt").exists()
    summary = json.loads((out / "outcome_digest.json").read_text(encoding="utf-8"))
    assert summary["critical_count"] == 1
    assert summary["warning_count"] == 1
    assert result["digest_dir"] == str(out)
