"""Tests for outcome_dashboard — data layer (Task 6) + stdlib server (Task 7)."""

import csv
import io
import json
import threading
import urllib.request
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer

from applypilot import database
import applypilot.outcome_scan as S
import applypilot.outcome_dashboard as D


def _seed(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, source_board, location, salary, "
        "fit_score, apply_status, applied_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("https://acme/1", "Senior Quant Analyst", "Acme", "greenhouse", "Remote",
         "$210,000", 8, "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.commit()
    for row in [
        dict(message_id="m1", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-02T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Thanks for applying", stage="acknowledged",
             outcome=None, reason=None, title="Senior Quant Analyst", company="Acme",
             match_method="company_name", match_score=0.9, confidence="high",
             body_text="b", snippet="b", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
        dict(message_id="m2", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-10T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Next steps", stage="interview",
             outcome=None, reason=None, title="Senior Quant Analyst", company="Acme",
             match_method="company_name", match_score=0.9, confidence="high",
             body_text="b", snippet="b", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
    ]:
        S.upsert_email_event(conn, row)


def test_universe_includes_applied_jobs(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)
    uni = D.get_tracked_universe(conn)
    assert [u["url"] for u in uni] == ["https://acme/1"]


def test_application_rows_compute_timeline(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)
    rows = D.build_application_rows(conn, now_iso="2026-06-20T00:00:00+00:00")
    r = rows[0]
    assert r["responded"] is True
    assert r["current_stage"] == "interview"
    assert r["first_response_days"] == 9
    assert r["segments"]["score_band"] == "8+"
    assert len(r["events"]) == 2


def test_build_csv_has_header_and_row(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)
    rows = D.build_application_rows(conn, now_iso="2026-06-20T00:00:00+00:00")
    text = D.build_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed[0]["company"] == "Acme"
    assert "first_response_days" in parsed[0]


def test_serve_refuses_unspecified_host():
    import pytest
    import applypilot.outcome_dashboard as D
    with pytest.raises(ValueError):
        D.serve(host="0.0.0.0", port=0)


def test_server_serves_json_and_csv(tmp_path):
    db = tmp_path / "applypilot.db"
    conn = database.init_db(db)
    _seed(conn)
    conn.commit()

    handler = D._make_handler(str(db))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/data") as resp:
            data = json.loads(resp.read())
        assert data["rows"][0]["company"] == "Acme"
        assert "insights" in data
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/export.csv") as resp:
            body = resp.read().decode()
        assert "company" in body.splitlines()[0]
    finally:
        server.shutdown()
        server.server_close()


def test_server_api_includes_review_and_action_queues(tmp_path):
    db = tmp_path / "applypilot.db"
    conn = database.init_db(db)
    _seed(conn)

    handler = D._make_handler(str(db))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/data") as resp:
            data = json.loads(resp.read())
        assert "review_queue" in data
        assert "action_queue" in data
        assert "alerts" in data
    finally:
        server.shutdown()
        server.server_close()


def test_server_post_review_persists_resolution(tmp_path):
    db = tmp_path / "applypilot.db"
    conn = database.init_db(db)
    _seed(conn)

    handler = D._make_handler(str(db))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        body = json.dumps({"message_id": "m2", "resolution": "ignored"}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/review",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read())
        assert result["resolution"] == "ignored"
    finally:
        server.shutdown()
        server.server_close()


def test_dashboard_uses_effective_events_after_ignore(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    from applypilot.outcome_review import record_review

    record_review(conn, "m2", resolution="ignored")
    rows = D.build_application_rows(conn, now_iso="2026-06-20T00:00:00+00:00")

    assert rows[0]["current_stage"] == "acknowledged"
    assert rows[0]["outcome"] is None
    assert [e["message_id"] for e in rows[0]["events"]] == ["m1"]


def test_dashboard_exposes_actionable_trust_state(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)

    rows = D.build_application_rows(conn, now_iso="2026-06-20T00:00:00+00:00")

    assert rows[0]["trust_state"] == "trusted"
    assert rows[0]["needs_action"] is True
