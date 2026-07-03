from applypilot import database


def test_email_events_table_created(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(email_events)").fetchall()}
    assert cols == {
        "message_id", "thread_id", "job_url", "occurred_at", "sender",
        "sender_domain", "subject", "stage", "outcome", "reason", "title",
        "company", "match_method", "match_score", "confidence", "body_text",
        "snippet", "extracted_by", "scanned_at",
        "match_status", "match_reason", "prev_job_url",
    }


def test_email_events_message_id_is_primary_key(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    pk = [row[1] for row in conn.execute("PRAGMA table_info(email_events)").fetchall() if row[5]]
    assert pk == ["message_id"]


def test_ensure_outcome_tables_is_idempotent(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    # Second call must not raise.
    database.ensure_outcome_tables(conn)
    idx = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='email_events'"
    ).fetchall()}
    assert {"idx_email_events_job", "idx_email_events_occurred", "idx_email_events_stage"} <= idx
