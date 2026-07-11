from applypilot import database
import applypilot.outcome_scan as S


def _seed(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, source_board, location, salary, "
        "fit_score, apply_status, applied_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("https://acme/1", "Senior Quant Analyst", "Acme", "greenhouse", "Remote",
         "$210,000", 8, "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.execute(
        "INSERT INTO jobs (url, title, company, source_board, location, salary, "
        "fit_score, apply_status, applied_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("https://acme/2", "Staff Analyst", "Acme", "greenhouse", "Remote",
         "$240,000", 9, "applied", "2026-06-03T00:00:00+00:00"),
    )
    conn.commit()
    for row in [
        dict(message_id="ack-1", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-02T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Thanks for applying", stage="acknowledged",
             outcome=None, reason=None, title="Senior Quant Analyst", company="Acme",
             match_method="company_name", match_score=0.9, confidence="high",
             body_text="ack", snippet="ack", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
        dict(message_id="iv-1", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-12T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Interview", stage="interview",
             outcome=None, reason=None, title="Senior Quant Analyst", company="Acme",
             match_method="company_name", match_score=0.9, confidence="high",
             body_text="interview", snippet="interview", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
        dict(message_id="screen-1", thread_id="t2", job_url=None,
             occurred_at="2026-06-13T00:00:00+00:00", sender="talent@acme.com",
             sender_domain="acme.com", subject="Quick intro", stage="screen",
             outcome=None, reason=None, title="Staff Analyst", company="Acme",
             match_method=None, match_score=None, confidence="medium",
             body_text="screen", snippet="screen", extracted_by="llm",
             scanned_at="2026-06-29T00:00:00+00:00", match_status="needs_review",
             match_reason="ambiguous_company"),
    ]:
        S.upsert_email_event(conn, row)


def test_build_operator_payload_splits_review_and_action_queues(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_operator import build_operator_payload

    payload = build_operator_payload(conn, now_iso="2026-06-20T00:00:00+00:00")
    assert [row["message_id"] for row in payload["review_queue"]] == ["screen-1"]
    assert [row["message_id"] for row in payload["action_queue"]] == ["iv-1", "screen-1"]
    acme1 = next(row for row in payload["rows"] if row["job_url"] == "https://acme/1")
    assert acme1["current_stage"] == "interview"


def test_mark_actioned_removes_item_from_action_queue(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_operator import build_operator_payload
    from applypilot.outcome_review import record_review

    record_review(conn, "iv-1", resolution="trusted", review_action="mark_actioned")
    payload = build_operator_payload(conn, now_iso="2026-06-20T00:00:00+00:00")
    assert [row["message_id"] for row in payload["action_queue"]] == ["screen-1"]


def test_trusted_application_rows_exclude_needs_review_events(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_operator import build_operator_payload

    payload = build_operator_payload(conn, now_iso="2026-06-20T00:00:00+00:00")
    acme2 = next(row for row in payload["rows"] if row["job_url"] == "https://acme/2")
    assert acme2["current_stage"] == "applied"
    assert acme2["needs_review_event_count"] == 0
