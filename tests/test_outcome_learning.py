import json

from applypilot import database
import applypilot.outcome_scan as S


def _seed(conn):
    for row in [
        ("https://acme/1", "Quant", "Acme", "greenhouse", "applied", "2026-06-01T00:00:00+00:00", 8),
        ("https://acme/2", "Ops", "Acme", "greenhouse", "applied", "2026-06-02T00:00:00+00:00", 6),
    ]:
        conn.execute(
            "INSERT INTO jobs (url, title, company, source_board, apply_status, applied_at, fit_score) "
            "VALUES (?,?,?,?,?,?,?)",
            row,
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
             title="Ops", company="Acme", match_method=None, match_score=None,
             confidence="medium", body_text="body", snippet="body", extracted_by="llm",
             scanned_at="2026-06-29T00:00:00+00:00", match_status="needs_review",
             match_reason="ambiguous_company"),
    ]:
        S.upsert_email_event(conn, row)
    from applypilot.outcome_review import record_review
    record_review(conn, "m1", resolution="trusted", reviewed_by="test")


def test_learning_export_excludes_needs_review_rows(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_learning import export_learning_bundle

    out = tmp_path / "learning"
    summary = export_learning_bundle(output_dir=out, conn=conn, now_iso="2026-06-20T00:00:00+00:00")
    timelines = [json.loads(line) for line in (out / "trusted_outcome_timelines.jsonl").read_text(encoding="utf-8").splitlines()]
    assert summary["trusted_rows"] == 2
    assert next(row for row in timelines if row["job_url"] == "https://acme/1")["current_stage"] == "interview"
    assert next(row for row in timelines if row["job_url"] == "https://acme/2")["current_stage"] == "applied"


def test_learning_export_writes_reports(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_learning import export_learning_bundle

    out = tmp_path / "learning"
    export_learning_bundle(output_dir=out, conn=conn, now_iso="2026-06-20T00:00:00+00:00")
    assert (out / "lane_report.json").exists()
    assert (out / "score_band_report.json").exists()
    assert (out / "latency_report.json").exists()
    assert (out / "recommendations.jsonl").exists()
