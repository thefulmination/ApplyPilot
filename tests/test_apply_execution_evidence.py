from __future__ import annotations

from applypilot.apply import launcher, pgqueue
from applypilot.fleet import queue


def test_execution_evidence_tracks_workday_step_submit_and_cost(monkeypatch):
    clock = iter((10.0, 10.5, 11.0, 11.5))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(clock))
    evidence = launcher._ExecutionEvidence(10.0)

    evidence.note_action(
        "browser_navigate",
        "navigate https://acme.wd5.myworkdayjobs.com/job/1",
    )
    evidence.note_action(
        "browser_click",
        "Submit application",
        '{"element":"Submit application"}',
    )
    result = evidence.snapshot(cost_usd=0.42, terminal_status="applied")

    assert result["workday_step"] == "submit"
    assert result["submit_clicked"] is True
    assert result["last_action"] == "Submit application"
    assert result["phase_costs_usd"] == {"agent_execution": 0.42}
    assert result["cost_allocation"] == "aggregate_only"
    assert result["confirmation_evidence"][0]["authoritative"] is False
    assert len(result["action_timeline"]) == 2


def test_result_metadata_is_persisted_on_immutable_event(fleet_db):
    url = "https://example.com/jobs/evidence"
    metadata = {
        "schema_version": 1,
        "last_action": "Submit application",
        "workday_step": "submit",
        "submit_clicked": True,
        "confirmation_evidence": [],
        "phase_costs_usd": {"agent_execution": 0.31},
    }
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, application_url, score, status, lease_owner, lease_expires_at) "
                "VALUES (%s,%s,9.0,'leased','w-evidence',now() + interval '5 minutes')",
                (url, url),
            )
        conn.commit()

        assert queue.write_apply_result(
            conn,
            "w-evidence",
            url,
            status="failed",
            apply_status="failed",
            apply_error="validation",
            target_host="example.com",
            home_ip="127.0.0.1",
            result_metadata=metadata,
        )
        with conn.cursor() as cur:
            cur.execute("SELECT result_metadata FROM apply_result_events WHERE url=%s", (url,))
            stored = cur.fetchone()["result_metadata"]
    assert stored == metadata
