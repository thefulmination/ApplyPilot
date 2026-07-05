from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import console_agents


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
                "VALUES ('m4-0','apply_agent','codex','sonnet',0.42,now())"
            )
        conn.commit()

        result = console_agents.agent_summary(conn)

    assert result["workers"][0]["worker_id"] == "m4-0"
    assert result["workers"][0]["current_agent"] == "codex"
    assert result["workers"][0]["current_model"] == "sonnet"
    assert result["availability"]["claude"]["blocked"] is True
    assert result["availability"]["claude"]["reason"] == "usage_limit_wall"
    assert result["spend_24h"][0]["provider"] == "codex"
    assert result["spend_24h"][0]["cost_usd"] == 0.42
    assert result["verdict"]["code"] == "working"


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
