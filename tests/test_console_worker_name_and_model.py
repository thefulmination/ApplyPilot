"""Tests for the console worker table visibility and payload in operator UX.

These checks validate:
  1) friendly machine labels are emitted in /api/status (/workers rows),
  2) model/agent + switching telemetry is present for apply workers,
  3) the web page includes the new table headings for those fields.
"""
from __future__ import annotations

import json
import threading
import urllib.request
import pytest
from http.server import ThreadingHTTPServer

from applypilot.apply import pgqueue
from applypilot.fleet import console_app, heartbeat


@pytest.fixture()
def live_server(monkeypatch):
    monkeypatch.setattr(console_app, "_CACHED_TOKEN", None, raising=False)
    monkeypatch.setenv("APPLYPILOT_CONSOLE_TOKEN", "tok-console-workers")

    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)


def test_build_status_includes_worker_machine_name_and_model_switching_data(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        heartbeat.beat(
            conn,
            "m4-0",
            machine_owner="m4",
            role="apply",
            state="idle",
            sw_version="0.3.0",
        )
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s, "
                "agent_chain=%s, last_agent_switch_at=now()-interval '8 minutes', "
                "last_agent_switch_reason=%s WHERE worker_id=%s",
                ("codex", "sonnet", "claude,codex", "canary rotation", "m2-0"),
            )
        conn.commit()

    payload = console_app.build_status()
    workers = {w["worker_id"]: w for w in payload["workers"]}

    m2 = workers["m2-0"]
    assert m2["machine"] == "tarpon"
    assert m2["current_agent"] == "codex"
    assert m2["current_model"] == "codex-default"
    assert m2["current_model_family"] == "codex-like"
    assert m2["current_model_vendor"] == "codex"
    assert m2["agent_chain"] == "claude,codex"
    assert m2["last_agent_switch_reason"] == "canary rotation"
    assert m2["last_agent_switch_at"] is not None

    m4 = workers["m4-0"]
    assert m4["machine"] == "gggtower"
    assert "by_lane" in payload["queue"]["apply"]
    assert payload["queue"]["apply"]["by_lane"]["ats"]["queued"] == 0
    assert payload["queue"]["apply"]["by_lane"]["compute"]["queued"] == 0


def test_build_status_includes_fast_agent_freshness_and_recommendation(
    fleet_db, monkeypatch
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)

    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        heartbeat.beat(conn, "m4-0", machine_owner="m4", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s, "
                "agent_chain=%s, last_agent_switch_at=now()-interval '4 minutes', "
                "last_agent_switch_reason=%s WHERE worker_id=%s",
                ("codex", "gpt-5", "claude,codex", "fallback success", "m2-0"),
            )
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s, "
                "agent_chain=%s WHERE worker_id=%s",
                ("claude", "claude-sonnet-4", "claude,codex", "m4-0"),
            )
            cur.execute(
                "UPDATE fleet_config SET deadman_alert=%s, deadman_alert_at=now() WHERE id=1",
                ("silent_death: no apply worker heartbeat",),
            )
        conn.commit()

    payload = console_app.build_status()

    agents = payload["agents"]
    assert agents["workers"] == 2
    assert agents["dynamic_workers"] == 2
    assert agents["switched_workers"] == 1
    assert agents["model_usage"] == {"claude-sonnet-4": 1, "gpt-5": 1}
    assert agents["model_family_usage"] == {"claude": 1, "codex-like": 1}
    assert agents["model_count"] == 2
    assert agents["model_missing_workers"] == 0

    freshness = payload["freshness"]
    assert freshness["endpoint"] == "status"
    assert freshness["generated_at"] is not None
    assert freshness["last_worker_beat_at"] is not None
    assert freshness["ages"]["last_worker_beat_seconds"] is not None

    recommendation = payload["recommendation"]
    assert recommendation["title"] == "Investigate DeadMan alert"
    assert recommendation["severity"] == "severe"
    assert "No apply worker heartbeat" in recommendation["reason"]


def test_build_status_discovery_workers_include_machine_names(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-d0", machine_owner="m2", role="discovery", state="applying")
        conn.commit()

    payload = console_app.build_status()
    discovery = payload["discovery"]
    assert discovery is not None
    assert discovery["workers"][0]["machine"] == "tarpon"
    assert discovery["workers"][0]["machine_owner"] == "m2"


def test_machine_names_can_be_overridden_by_env(fleet_db, monkeypatch) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    monkeypatch.setenv("APPLYPILOT_MACHINE_NAMES", "{\"m2\":\"tarpon-home\",\"m4\":\"gggtower-node\"}")

    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        conn.commit()

    payload = console_app.build_status()
    assert payload["workers"][0]["machine"] == "tarpon-home"


def test_workers_table_includes_model_and_machine_columns_in_html(live_server) -> None:
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")
    assert "<th>Machine</th>" in html
    assert "<th>Agent</th>" in html
    assert "<th>Model family</th>" in html
    assert "<th>Model</th>" in html
    assert "<th>Model vendor</th>" in html
    assert "<th>Last switch</th>" in html
    assert 'class="table-scroll"' in html
    assert "id=\"workers\"" in html
    assert "id=\"aPrimaryModel\"" in html
    assert "id=\"aSwitchStatus\"" in html
    assert "<th>Discovery worker</th>" in html
    assert "<th>Found 24h</th>" in html
    assert "id=\"discWorkers\"" in html
    assert "class=\"family-legend\"" in html
    assert "codex-like" in html
    assert "id=\"aModelByMachine\"" in html
    assert "aDynamicWorked" in html
    assert "aLatestSwitch" in html
    assert "id=\"aModelTrend\"" in html
    assert "aModelTrendHint" in html
    assert "id=\"cQueueFlow\"" in html
    assert "id=\"cQueueFlowHint\"" in html
    assert "id=\"cLaneFlowAts\"" in html
    assert "id=\"cLaneFlowAtsHint\"" in html
    assert "id=\"cLaneFlowCompute\"" in html
    assert "id=\"cLaneFlowComputeHint\"" in html
    assert "id=\"cLaneFlowOther\"" in html
    assert "id=\"cLaneFlowOtherHint\"" in html
    assert "id=\"cQueuedAts\"" in html
    assert "id=\"cQueuedCompute\"" in html
    assert "id=\"cQueuedOther\"" in html
    assert 'id="applyStateCard"' in html
    assert 'class="card apply-state' in html
    assert "class=\"verdict-chip" in html
    assert "v-working" in html
    assert "v-blocked" in html
    assert "vendor-chip" in html
    assert "s.recommendation || {}" in html
    assert "stateRecommendation" in html


def test_queue_flow_js_samples_lane_payloads_instead_of_history_array(live_server) -> None:
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")
    assert "function _queueFlowForLane(flowSamples, laneData, laneText)" in html
    assert "const queue = laneData || {};" in html
    assert "return _queueFlowForLane(laneFlowSnapshots[key], laneData, key.toUpperCase());" in html


def test_agents_api_includes_switch_verdicts_and_spend(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying", sw_version="0.2.0")
        heartbeat.beat(conn, "m4-0", machine_owner="m4", role="apply", state="idle", sw_version="0.2.0")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s, "
                "agent_chain=%s, last_agent_switch_at=now()-interval '15 minutes', "
                "last_agent_switch_reason=%s WHERE worker_id=%s",
                ("codex", "haiku", "codex,claude", "model blocked", "m2-0"),
            )
            cur.execute(
                "INSERT INTO llm_usage (provider, model, task, cost_usd, ts) "
                "VALUES (%s, %s, %s, %s, now() - interval '2 minutes')",
                ("codex", "haiku", "apply", 1.23),
            )
            cur.execute(
                "INSERT INTO agent_availability (agent, blocked_until, reason) "
                "VALUES (%s, now() + interval '10 minutes', %s)",
                ("codex", "usage_limit_wall"),
            )
        conn.commit()

    payload = console_app._agents_state()
    workers = {w["worker_id"]: w for w in payload["workers"]}
    assert workers["m2-0"]["machine"] == "tarpon"
    assert workers["m2-0"]["current_agent"] == "codex"
    assert workers["m2-0"]["current_model"] == "codex-default"
    assert workers["m2-0"]["switch_verdict"] == "partial"
    assert workers["m2-0"]["active_blocked_agents"] == ["codex"]
    assert payload["agent_blocks"]["codex"]["reason"] == "usage_limit_wall"
    assert payload["agent_spend"]["codex"]["models"]["haiku"] == 1
    assert payload["agent_spend"]["codex"]["spend_usd"] == 1.23
    summary = payload["summary"]
    assert summary["dynamic_workers"] == 1
    assert summary["switched_workers"] == 0
    assert summary["partial_workers"] == 1
    assert summary["blocked_workers"] == 0
    assert summary["model_usage"]["codex-default"] == 1
    assert summary["model_count"] == 1
    assert summary["model_missing_workers"] == 0


def test_agents_api_is_read_only_and_html_wires_call(live_server, fleet_db) -> None:
    with urllib.request.urlopen(f"{live_server}/") as resp:
        html = resp.read().decode("utf-8")
    assert 'id="agents"' in html
    assert "Agents and models" in html
    assert "/api/agents" in html

    with urllib.request.urlopen(f"{live_server}/api/agents") as resp:
        assert resp.status == 200
        body = json.loads(resp.read().decode("utf-8"))
        assert set(body.keys()) == {"workers", "agent_blocks", "agent_spend", "summary"}


def test_agents_api_workers_include_machine_and_model_family(monkeypatch, live_server, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s, agent_chain=%s "
                "WHERE worker_id=%s",
                ("codex", "gpt-5", "codex,claude", "m2-0"),
            )
        conn.commit()

    with urllib.request.urlopen(f"{live_server}/api/agents") as resp:
        body = json.loads(resp.read().decode("utf-8"))

    rows = body["workers"]
    assert len(rows) == 1
    row = rows[0]
    assert row["machine"] == "tarpon"
    assert row["current_model_family"] == "codex-like"
    assert row["current_model_vendor"] == "codex"
    assert row["current_model"] == "gpt-5"
    assert row["current_agent"] == "codex"
    assert row["agent_chain"] == ["codex", "claude"]


def test_agents_summary_tracks_models_and_switching(live_server, monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        heartbeat.beat(conn, "m4-0", machine_owner="m4", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
            "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s, "
            "agent_chain=%s WHERE worker_id=%s",
                ("codex", "claude-sonnet-4", "claude,codex", "m2-0"),
            )
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent=%s, current_model=%s, "
                "agent_chain=%s WHERE worker_id=%s",
                ("codex", "gpt-5", "codex", "m4-0"),
            )
        conn.commit()

    with urllib.request.urlopen(f"{live_server}/api/agents") as resp:
        payload = json.loads(resp.read().decode("utf-8"))

    summary = payload["summary"]
    assert summary["dynamic_workers"] == 1
    assert summary["switched_workers"] == 1
    assert summary["model_usage"]["claude-sonnet-4"] == 1
    assert summary["model_usage"]["gpt-5"] == 1
    assert summary["model_family_usage"]["claude"] == 1
    assert summary["model_family_usage"]["codex-like"] == 1


def test_build_status_uses_llm_usage_fallback_for_missing_worker_model(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO llm_usage (worker_id, machine_owner, provider, model, task, cost_usd, ts) "
                "VALUES (%s, %s, %s, %s, %s, %s, now() - interval '1 minute')",
                ("m2-0", "m2", "codex", "gpt-5", "apply", 0.31),
            )
        conn.commit()

    payload = console_app.build_status()
    workers = {w["worker_id"]: w for w in payload["workers"]}
    agents = payload["agents"]

    assert workers["m2-0"]["current_agent"] == "codex"
    assert workers["m2-0"]["current_model"] == "gpt-5"
    assert workers["m2-0"]["current_model_family"] == "codex-like"
    assert workers["m2-0"]["current_model_vendor"] == "codex"
    assert agents["model_usage"] == {"gpt-5": 1}
    assert agents["model_family_usage"] == {"codex-like": 1}


def test_build_status_uses_zero_cost_result_evidence_for_missing_worker_model(
    monkeypatch, fleet_db
) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "home-apply-0", machine_owner="home", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_result_events (queue_name, url, worker_id, machine_owner, "
                "status, agent, agent_model, est_cost_usd) "
                "VALUES ('apply_queue', 'apply-zero', 'home-apply-0', 'home', "
                "'applied', 'codex', 'codex-default', 0)"
            )
        conn.commit()

    payload = console_app.build_status()
    worker = {w["worker_id"]: w for w in payload["workers"]}["home-apply-0"]

    assert worker["current_agent"] == "codex"
    assert worker["current_model"] == "codex-default"
    assert worker["current_model_family"] == "codex-like"
    assert worker["current_model_vendor"] == "codex"


def test_build_status_tolerates_pre_migration_result_event_schema(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "legacy-apply-0", machine_owner="home", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE apply_result_events DROP COLUMN machine_owner")
            cur.execute(
                "INSERT INTO apply_result_events (queue_name, url, worker_id, status, agent, agent_model) "
                "VALUES ('apply_queue', 'legacy-zero', 'legacy-apply-0', "
                "'applied', 'codex', 'codex-default')"
            )
        conn.commit()

    payload = console_app.build_status()
    worker = {w["worker_id"]: w for w in payload["workers"]}["legacy-apply-0"]
    assert worker["current_model"] == "codex-default"


def test_build_status_normalizes_legacy_codex_sonnet_telemetry(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "legacy-codex-0", machine_owner="home", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent='codex', current_model=NULL "
                "WHERE worker_id='legacy-codex-0'"
            )
            cur.execute(
                "INSERT INTO llm_usage (worker_id, machine_owner, provider, model, task, cost_usd) "
                "VALUES ('legacy-codex-0', 'home', 'codex', 'sonnet', 'apply_agent', 0)"
            )
        conn.commit()

    worker = {
        w["worker_id"]: w for w in console_app.build_status()["workers"]
    }["legacy-codex-0"]
    assert worker["current_agent"] == "codex"
    assert worker["current_model"] == "codex-default"
    assert worker["current_model_vendor"] == "codex"


def test_build_status_does_not_mix_model_from_different_agent(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "switched-0", machine_owner="home", role="apply", state="idle")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent='claude', current_model=NULL "
                "WHERE worker_id='switched-0'"
            )
            cur.execute(
                "INSERT INTO llm_usage (worker_id, machine_owner, provider, model, task, cost_usd) "
                "VALUES ('switched-0', 'home', 'codex', 'gpt-5', 'apply_agent', 0)"
            )
        conn.commit()

    worker = {w["worker_id"]: w for w in console_app.build_status()["workers"]}["switched-0"]
    assert worker["current_agent"] == "claude"
    assert worker["current_model"] is None


def test_build_status_labels_legacy_codex_without_usage_as_cli_default(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "codex-no-usage", machine_owner="m2", role="apply", state="paused")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE worker_heartbeat SET current_agent='codex', current_model=NULL "
                "WHERE worker_id='codex-no-usage'"
            )
        conn.commit()

    worker = {
        w["worker_id"]: w for w in console_app.build_status()["workers"]
    }["codex-no-usage"]
    assert worker["current_model"] == "codex-default"


def test_agents_summary_counts_apply_agent_usage_rows(monkeypatch, fleet_db) -> None:
    monkeypatch.setenv("APPLYPILOT_FLEET_DSN", fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO llm_usage (worker_id, machine_owner, provider, model, task, cost_usd, ts) "
                "VALUES (%s, %s, %s, %s, %s, %s, now() - interval '1 minute')",
                ("m2-0", "m2", "codex", "gpt-5", "apply_agent", 0.44),
            )
        conn.commit()

    payload = console_app._agents_state()
    assert payload["agent_spend"]["codex"]["models"]["gpt-5"] == 1
    assert payload["agent_spend"]["codex"]["spend_usd"] == 0.44
