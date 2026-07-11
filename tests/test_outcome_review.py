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
         "$230,000", 9, "applied", "2026-06-03T00:00:00+00:00"),
    )
    conn.commit()
    for row in [
        dict(message_id="m1", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-02T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Thanks for applying", stage="acknowledged",
             outcome=None, reason=None, title="Senior Quant Analyst", company="Acme",
             match_method="company_name", match_score=0.9, confidence="high",
             body_text="hello", snippet="hello", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
        dict(message_id="m2", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-10T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Decision", stage="rejected",
             outcome="rejected", reason="went another direction", title="Senior Quant Analyst",
             company="Acme", match_method="company_name", match_score=0.9, confidence="high",
             body_text="long body", snippet="long", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
        dict(message_id="m3", thread_id="t2", job_url=None,
             occurred_at="2026-06-12T00:00:00+00:00", sender="talent@acme.com",
             sender_domain="acme.com", subject="Quick intro", stage="screen",
             outcome=None, reason=None, title="Staff Analyst", company="Acme",
             match_method=None, match_score=None, confidence="medium",
             body_text="screen request", snippet="screen", extracted_by="llm",
             scanned_at="2026-06-29T00:00:00+00:00", match_status="needs_review",
             match_reason="ambiguous_company"),
    ]:
        S.upsert_email_event(conn, row)


def test_init_db_creates_email_event_reviews(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(email_event_reviews)").fetchall()
    }
    assert {"message_id", "review_action", "resolution", "reviewed_at"} <= cols


def test_effective_events_apply_latest_review(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_review import record_review, build_effective_events

    record_review(conn, "m2", resolution="corrected", corrected_stage="screen", corrected_outcome=None)
    record_review(conn, "m2", resolution="trusted", corrected_stage="interview", corrected_outcome=None)

    rows = {row["message_id"]: row for row in build_effective_events(conn)}
    assert rows["m2"]["stage"] == "interview"
    assert rows["m2"]["outcome"] is None
    assert rows["m2"]["trust_state"] == "trusted"


def test_ignored_events_drop_out_of_effective_job_timeline(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_review import record_review, build_effective_events_for_job

    record_review(conn, "m2", resolution="ignored")
    rows = build_effective_events_for_job(conn, "https://acme/1")
    assert [row["message_id"] for row in rows] == ["m1"]


def test_reassign_job_moves_event_to_other_job(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_review import record_review, build_effective_events_for_job

    record_review(conn, "m3", resolution="corrected", corrected_job_url="https://acme/2")
    rows = build_effective_events_for_job(conn, "https://acme/2")
    assert [row["message_id"] for row in rows] == ["m3"]
    assert rows[0]["job_url"] == "https://acme/2"


def test_review_queue_includes_needs_review_rows(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_review import list_review_queue

    rows = list_review_queue(conn)
    assert [row["message_id"] for row in rows] == ["m1", "m2", "m3"]


def test_recommendation_mail_is_rejected_before_matching():
    from applypilot.outcome_review import classify_review_candidate

    assert classify_review_candidate(
        sender="Indeed <donotreply@match.indeed.com>", subject="5 new jobs"
    ) == "rejected"
    assert classify_review_candidate(
        sender="Recruiter <person@company.com>", subject="Interview availability"
    ) == "needs_review"
