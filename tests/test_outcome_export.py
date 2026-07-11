import json

from applypilot import database
import applypilot.outcome_scan as S
from applypilot.outcome_export import export_outcome_events


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
             body_text="hello", snippet="hello", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
        dict(message_id="m2", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-10T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Decision", stage="rejected",
             outcome="rejected", reason="went another direction", title="Senior Quant Analyst",
             company="Acme", match_method="company_name", match_score=0.9, confidence="high",
             body_text="long body", snippet="long", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
    ]:
        S.upsert_email_event(conn, row)


def test_export_writes_both_jsonl_with_enrichment(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)
    from applypilot.outcome_review import record_review

    record_review(conn, "m2", resolution="ignored")
    out = tmp_path / "exp"
    summary = export_outcome_events(output_dir=out, conn=conn)

    events = [json.loads(l) for l in (out / "email_events.jsonl").read_text(encoding="utf-8").splitlines()]
    timelines = [json.loads(l) for l in (out / "outcome_timelines.jsonl").read_text(encoding="utf-8").splitlines()]

    assert summary["email_events_exported"] == 2
    assert summary["outcome_timelines_exported"] == 1
    assert {e["message_id"] for e in events} == {"m1", "m2"}

    tl = timelines[0]
    assert tl["job_url"] == "https://acme/1"
    assert tl["outcome"] is None
    assert tl["trust_state"] == "trusted"
    assert tl["trusted_event_count"] == 1
    assert tl["excluded_event_count"] == 1
    assert tl["implied_status"] is None
    assert "outcome_signal" in tl                                 # #5 field, advisory
    # body_text stripped from timeline events (lean); snippet kept
    assert "body_text" not in tl["events"][0]
    assert tl["events"][0]["snippet"] == "hello"
    assert tl["events"][0]["trust_state"] == "trusted"
    # raw archive carries body_text (lossless)
    assert next(e for e in events if e["message_id"] == "m1")["body_text"] == "hello"
    # summary sidecar is written to disk
    summary_on_disk = json.loads((out / "outcomes_summary.json").read_text(encoding="utf-8"))
    assert summary_on_disk["email_events_exported"] == 2
    assert summary_on_disk["trusted_event_count"] == 1
    assert summary_on_disk["excluded_event_count"] == 1
