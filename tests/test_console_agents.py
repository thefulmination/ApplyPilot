from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_agents, console_app


def test_agent_summary_no_workers_returns_warn_unknown(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        result = console_agents.agent_summary(conn)

    assert result["verdict"]["code"] == "unknown"
    assert result["verdict"]["severity"] == "warn"
    assert result["verdict"]["reason"]


def test_agent_summary_reads_worker_heartbeat_blocks_and_spend(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, machine_owner, home_ip, role, state, last_beat, "
                "current_agent, current_model, agent_chain, last_agent_switch_reason) "
                "VALUES ('m4-0','m4','100.69.68.103','apply','idle',now(),"
                "'codex','sonnet','claude>codex','switch:claude->codex')"
            )
            cur.execute(
                "INSERT INTO agent_availability (agent, blocked_until, reason) "
                "VALUES ('claude', now() + interval '1 hour', 'usage_limit_wall')"
            )
            cur.execute(
                "INSERT INTO llm_usage (worker_id, task, provider, model, cost_usd, ts) "
                "VALUES ('m4-0','apply_agent','codex','sonnet',0.42,now()), "
                "('m4-0','apply_agent','codex','sonnet',0.08,now()), "
                "('m4-0','apply_agent','codex','sonnet',9.00,now() - interval '25 hours')"
            )
        conn.commit()

        result = console_agents.agent_summary(conn)

    assert result["workers"][0]["worker_id"] == "m4-0"
    assert result["workers"][0]["current_agent"] == "codex"
    assert result["workers"][0]["current_model"] == "sonnet"
    assert result["availability"]["claude"]["blocked"] is True
    assert result["availability"]["claude"]["reason"] == "usage_limit_wall"
    assert result["spend_24h"][0]["provider"] == "codex"
    assert result["spend_24h"][0]["count"] == 2
    assert result["spend_24h"][0]["cost_usd"] == 0.50
    assert result["verdict"]["code"] == "working"
    assert result["verdict"]["severity"] == "ok"
    assert result["verdict"]["reason"]


def test_agent_summary_detects_all_agents_blocked(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, role, state, last_beat, current_agent, current_model, agent_chain) "
                "VALUES ('m2-0','apply','idle',now(),'claude','sonnet','claude>codex')"
            )
            cur.execute(
                "INSERT INTO agent_availability (agent, blocked_until, reason) "
                "VALUES ('claude', now() + interval '1 hour', 'usage_limit_wall'), "
                "('codex', now() + interval '1 hour', 'predictive_spend')"
            )
        conn.commit()

        result = console_agents.agent_summary(conn)

    assert result["verdict"]["code"] == "all_agents_blocked"
    assert result["verdict"]["severity"] == "halted"
    assert result["verdict"]["reason"]


def test_agent_summary_downgrades_stale_switch_without_recent_fallback_spend(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, role, state, last_beat, current_agent, current_model, "
                "agent_chain, last_agent_switch_reason) "
                "VALUES ('m4-1','apply','idle',now(),'codex','sonnet',"
                "'claude>codex','switch:claude->codex')"
            )
            cur.execute(
                "INSERT INTO agent_availability (agent, blocked_until, reason) "
                "VALUES ('claude', now() + interval '1 hour', 'usage_limit_wall')"
            )
        conn.commit()

        result = console_agents.agent_summary(conn)

    assert result["spend_24h"] == []
    assert result["verdict"]["code"] == "partial"
    assert result["verdict"]["severity"] == "warn"
    assert result["verdict"]["reason"]


def test_agent_summary_ignores_other_worker_spend_for_switch_confirmation(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, role, state, last_beat, current_agent, current_model, "
                "agent_chain, last_agent_switch_at, last_agent_switch_reason) "
                "VALUES "
                "('m4-0','apply','idle',now(),'codex','sonnet','claude>codex',NULL,NULL), "
                "('m4-1','apply','idle',now(),'codex','sonnet','claude>codex',"
                "now() - interval '1 hour','switch:claude->codex')"
            )
            cur.execute(
                "INSERT INTO agent_availability (agent, blocked_until, reason) "
                "VALUES ('claude', now() + interval '1 hour', 'usage_limit_wall')"
            )
            cur.execute(
                "INSERT INTO llm_usage (worker_id, task, provider, model, cost_usd, ts) "
                "VALUES ('m4-0','apply_agent','codex','sonnet',0.25,now())"
            )
        conn.commit()

        result = console_agents.agent_summary(conn)

    assert result["spend_24h"][0]["provider"] == "codex"
    assert result["spend_24h"][0]["count"] == 1
    assert result["verdict"]["code"] == "partial"


def test_agent_summary_treats_current_agent_as_configured_when_chain_empty(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, role, state, last_beat, current_agent, current_model) "
                "VALUES ('m2-1','apply','idle',now(),'claude','sonnet')"
            )
            cur.execute(
                "INSERT INTO agent_availability (agent, blocked_until, reason) "
                "VALUES ('claude', now() + interval '1 hour', 'usage_limit_wall')"
            )
        conn.commit()

        result = console_agents.agent_summary(conn)

    assert result["workers"][0]["chain_agents"] == ["claude"]
    assert result["verdict"]["code"] == "all_agents_blocked"


def test_agents_route_scrubs_connect_errors(monkeypatch):
    def boom():
        raise RuntimeError("connect failed password=super-secret token=raw-token")

    monkeypatch.setattr(console_app.pgqueue, "connect", boom)
    server = ThreadingHTTPServer(("127.0.0.1", 0), console_app._Handler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/agents", method="GET")
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 500
        body = json.loads(exc_info.value.read())
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()

    assert "error" in body
    assert len(body["error"]) <= 500
    assert "super-secret" not in body["error"]
    assert "raw-token" not in body["error"]
