from __future__ import annotations

import pytest

from applypilot.apply import launcher, pgqueue
from applypilot.fleet import queue


def test_execution_evidence_tracks_workday_step_submit_and_cost(monkeypatch):
    clock = iter((10.0, 10.5, 11.0, 11.5))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(clock))
    markers = []
    evidence = launcher._ExecutionEvidence(10.0, interaction_marker=lambda: markers.append(True))

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
    assert markers == [True]


def test_execution_evidence_fails_closed_without_lease_marker():
    evidence = launcher._ExecutionEvidence(10.0)

    with pytest.raises(RuntimeError, match="not bound to the active lease"):
        evidence.prepare_tool("mcp__playwright__browser_click")


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("mcp__playwright__browser_click", "browser_click"),
        ("mcp__codex__browser_click", "browser_click"),
        ("mcp__codex_apps__browser_click", "browser_click"),
        ("playwright__browser_type", "browser_type"),
        ("playwright.browser_navigate", "browser_navigate"),
        ("browser_fill_form", "browser_fill_form"),
    ],
)
def test_browser_tool_names_are_normalized_across_agent_protocols(tool_name, expected):
    assert launcher._normalize_browser_tool_name(tool_name) == expected
    assert launcher._browser_tool_policy(tool_name) == "interaction"


@pytest.mark.parametrize(
    "tool_name",
    [
        "mcp__playwright__browser_snapshot",
        "browser_take_screenshot",
        "browser_console_messages",
        "browser_network_requests",
        "browser_wait_for",
    ],
)
def test_browser_read_tools_deliberately_do_not_cross_interaction_boundary(tool_name):
    markers = []
    evidence = launcher._ExecutionEvidence(10.0, interaction_marker=lambda: markers.append(True))

    evidence.prepare_tool(tool_name)

    assert launcher._browser_tool_policy(tool_name) == "read"
    assert markers == []


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
                "(url, application_url, score, status,apply_domain,target_host,dedup_key,approved_batch) "
                "VALUES (%s,%s,9.0,'queued','example.com','example.com','evidence-dedup','test')",
                (url, url),
            )
            cur.execute(
                "INSERT INTO fleet_decision_policies(policy_version,lane,status) "
                "VALUES('evidence-policy','ats','active')"
            )
            cur.execute("UPDATE fleet_config SET ats_policy_version='evidence-policy' WHERE id=1")
            cur.execute(
                "UPDATE apply_queue SET decision_id='evidence-decision',policy_version='evidence-policy',"
                "decision_action='apply',qualification_verdict='qualified',qualification_score=9,"
                "qualification_floor=7,preference_score=9,outcome_score=9,final_score=9,"
                "decision_confidence=.9,decision_created_at=now(),"
                "decision_expires_at=now()+interval '1 day',input_hash='evidence-hash' WHERE url=%s",
                (url,),
            )
        conn.commit()
        assert queue.lease_apply(conn, "w-evidence", home_ip="127.0.0.1") is not None

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
            cur.execute(
                "SELECT result_metadata FROM apply_result_events WHERE url=%s ORDER BY id DESC LIMIT 1",
                (url,),
            )
            stored = cur.fetchone()["result_metadata"]
    assert stored == metadata
