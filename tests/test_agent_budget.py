"""Fleet-wide agent availability + predictive spend soft-blocks (agent_budget).

Runs against the disposable test Postgres (fleet_db). Proves:
  - a block written by one worker is visible fleet-wide, and expires;
  - a later block never gets shortened by an earlier one (GREATEST);
  - rolling per-agent apply spend sums only in-window rows by provider;
  - evaluate_soft_blocks pre-emptively blocks an agent over its soft cap, and leaves
    an under-cap / uncapped agent alone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from applypilot.apply import pgqueue
from applypilot.fleet import agent_budget
from applypilot.fleet import queue


def _authorize_apply(conn, url: str, score: float = 9.0) -> None:
    policy = "test-ats-policy"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
            "VALUES (%s,'ats','active') ON CONFLICT (policy_version) DO UPDATE SET status='active'",
            (policy,),
        )
        cur.execute("UPDATE fleet_config SET ats_policy_version=%s WHERE id=1", (policy,))
        cur.execute(
            "UPDATE apply_queue SET decision_id=%s, policy_version=%s, decision_action='apply', "
            "qualification_verdict='qualified', qualification_score=9, qualification_floor=7, "
            "preference_score=8, outcome_score=8, final_score=%s, decision_confidence=.9, "
            "decision_created_at=now(), decision_expires_at=now()+interval '1 day', input_hash=%s "
            "WHERE url=%s",
            (f"decision-{url}", policy, score, f"hash-{url}", url),
        )
    conn.commit()


def _now_utc():
    # DB and test share UTC wall clock; generous margins make skew irrelevant.
    return datetime.now(timezone.utc)


def _seed_spend(conn, provider, cost, *, secs_ago):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO llm_usage (worker_id, task, provider, cost_usd, ts) "
            "VALUES ('w', 'apply_agent', %s, %s, now() - make_interval(secs => %s))",
            (provider, cost, secs_ago),
        )
    conn.commit()


def test_record_and_get_block_roundtrip(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        agent_budget.record_block(conn, "claude", _now_utc() + timedelta(hours=1), "usage_limit_wall")
    with pgqueue.connect(fleet_db) as conn:
        blocks = agent_budget.get_blocks(conn)
    assert "claude" in blocks
    assert blocks["claude"] > _now_utc()


def test_past_block_is_not_returned(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        agent_budget.record_block(conn, "codex", _now_utc() - timedelta(hours=1), "stale")
    with pgqueue.connect(fleet_db) as conn:
        assert "codex" not in agent_budget.get_blocks(conn)


def test_record_block_never_shortens_a_later_block(fleet_db):
    far = _now_utc() + timedelta(hours=5)
    near = _now_utc() + timedelta(minutes=10)
    with pgqueue.connect(fleet_db) as conn:
        agent_budget.record_block(conn, "claude", far, "usage_limit_wall")
        agent_budget.record_block(conn, "claude", near, "predictive_spend")  # must NOT shorten
    with pgqueue.connect(fleet_db) as conn:
        blocks = agent_budget.get_blocks(conn)
    assert blocks["claude"] > _now_utc() + timedelta(hours=4)


def test_rolling_spend_sums_only_in_window_by_provider(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_spend(conn, "claude", 1.00, secs_ago=60)      # in window
        _seed_spend(conn, "claude", 2.00, secs_ago=120)     # in window
        _seed_spend(conn, "claude", 9.00, secs_ago=99999)   # out of window
        _seed_spend(conn, "codex", 5.00, secs_ago=60)       # different provider
    with pgqueue.connect(fleet_db) as conn:
        got = agent_budget.rolling_spend(conn, "claude", window_seconds=3600)
    assert abs(got - 3.00) < 1e-6


def test_evaluate_soft_blocks_blocks_over_cap_only(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_spend(conn, "claude", 8.00, secs_ago=60)   # over a $5 cap
        _seed_spend(conn, "codex", 1.00, secs_ago=60)    # under its $5 cap
    with pgqueue.connect(fleet_db) as conn:
        actions = agent_budget.evaluate_soft_blocks(
            conn, soft_caps={"claude": 5.0, "codex": 5.0},
            window_seconds=3600, cooldown_seconds=1800,
        )
    blocked = {a for a, _ in actions}
    assert blocked == {"claude"}
    with pgqueue.connect(fleet_db) as conn:
        blocks = agent_budget.get_blocks(conn)
        reason = agent_budget.get_block_reason(conn, "claude")
    assert "claude" in blocks and "codex" not in blocks
    assert reason == "predictive_spend"


def test_write_apply_result_attributes_agent_to_llm_usage(fleet_db):
    # The apply spend must carry the agent (as llm_usage.provider) so rolling_spend can
    # measure per-agent Claude spend for the predictive monitor.
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, application_url, score, status, lane, "
                "approved_batch, dedup_key, apply_domain) "
                "VALUES ('ja','http://x/y','9','queued','ats','b1','dk-ja','acme.com')")
        conn.commit()
        _authorize_apply(conn, "ja")
    with pgqueue.connect(fleet_db) as conn:
        queue.lease_apply(conn, "w-attr", home_ip="1.1.1.1")
    with pgqueue.connect(fleet_db) as conn:
        ok = queue.write_apply_result(conn, "w-attr", "ja", status="applied",
                                      apply_status="applied", target_host="acme.com",
                                      home_ip="1.1.1.1", est_cost_usd=0.5, agent="claude")
        assert ok is True
    with pgqueue.connect(fleet_db) as conn:
        assert abs(agent_budget.rolling_spend(conn, "claude", window_seconds=3600) - 0.5) < 1e-6


def test_worker_passthrough_forwards_agent_from_result(fleet_db, monkeypatch, tmp_path):
    # A real apply_fn reports which agent ran (make_apply_fn includes it); the worker
    # passthrough must forward it so the spend is attributed.
    from applypilot.fleet.worker import WorkerLoop
    from applypilot import config
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "applypilot.db")
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, application_url, score, status, lane, "
                "approved_batch, dedup_key, apply_domain) "
                "VALUES ('jz','http://x/y','9','queued','ats','b1','dk-jz','acme-z.com')")
        conn.commit()
        _authorize_apply(conn, "jz")
    loop = WorkerLoop(
        lambda: pgqueue.connect(fleet_db), "w-fwd", home_ip="1.1.1.1", role="apply",
        apply_fn=lambda job: {"run_status": "applied", "est_cost_usd": 0.7, "agent": "deepseek"},
    )
    assert loop.run_once()["action"] == "applied"
    with pgqueue.connect(fleet_db) as conn:
        assert abs(agent_budget.rolling_spend(conn, "deepseek", window_seconds=3600) - 0.7) < 1e-6


def test_evaluate_soft_blocks_ignores_zero_or_missing_cap(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_spend(conn, "claude", 99.0, secs_ago=60)
    with pgqueue.connect(fleet_db) as conn:
        # cap 0 = disabled; and an agent with no cap entry is never blocked
        actions = agent_budget.evaluate_soft_blocks(
            conn, soft_caps={"claude": 0.0}, window_seconds=3600, cooldown_seconds=1800)
    assert actions == []
    with pgqueue.connect(fleet_db) as conn:
        assert "claude" not in agent_budget.get_blocks(conn)
