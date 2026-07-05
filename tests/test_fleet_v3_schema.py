"""v3 schema: loads cleanly against real Postgres, idempotent, all tables + columns present."""
from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import schema as fleet_schema

EXPECTED_TABLES = {
    "apply_queue", "fleet_config", "fleet_assets",  # base
    "compute_queue", "search_tasks", "linkedin_queue", "rate_governor", "llm_usage",
    "applied_set", "answer_bank", "auth_challenge", "otp_request", "inbox_events",
    "workers", "worker_heartbeat", "poison_jobs", "remote_commands",
}


def _tables(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
        return {r["table_name"] for r in cur.fetchall()}


def test_v3_schema_creates_all_tables(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        got = _tables(conn)
    missing = EXPECTED_TABLES - got
    assert not missing, f"missing tables: {missing}"


def test_v3_schema_idempotent(fleet_db):
    # the fixture already applied it once; applying twice more must not error.
    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        fleet_schema.ensure_schema_v3(conn)
        got = _tables(conn)
    assert {"rate_governor", "search_tasks", "compute_queue"} <= got


def test_v3_schema_migrates_compute_queue_url_primary_key(fleet_pg):
    with pgqueue.connect(fleet_pg) as conn:
        pgqueue.ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS compute_queue")
            cur.execute(
                """
                CREATE TABLE compute_queue (
                    url TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    payload JSONB,
                    status fleet_task_status NOT NULL DEFAULT 'queued',
                    lease_owner TEXT,
                    lease_expires_at TIMESTAMPTZ,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    result JSONB,
                    est_cost_usd NUMERIC(10,4) NOT NULL DEFAULT 0,
                    synced_to_home_at TIMESTAMPTZ,
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()

        fleet_schema.ensure_schema_v3(conn)

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO compute_queue (url, task) VALUES "
                "('same-url', 'score'), ('same-url', 'audit')"
            )
            cur.execute(
                """
                SELECT a.attname
                FROM pg_constraint c
                JOIN unnest(c.conkey) WITH ORDINALITY u(attnum, ordinality) ON true
                JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = u.attnum
                WHERE c.conrelid = 'compute_queue'::regclass AND c.contype = 'p'
                ORDER BY u.ordinality
                """
            )
            pk_cols = [r["attname"] for r in cur.fetchall()]
        conn.commit()

    assert pk_cols == ["url", "task"]


def test_apply_queue_v3_columns(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='apply_queue'")
        cols = {r["column_name"] for r in cur.fetchall()}
    for c in ("worker_home_ip", "target_host", "lane", "dedup_key", "approved_batch"):
        assert c in cols, f"apply_queue missing {c}"


def test_fleet_config_v3_columns(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='fleet_config'")
        cols = {r["column_name"] for r in cur.fetchall()}
    for c in ("approval_threshold", "approval_policy", "approval_sampling_rate",
              "cost_cap_daily_usd", "cost_cap_total_usd", "pinned_worker_version"):
        assert c in cols, f"fleet_config missing {c}"


def test_challenge_rate_is_generated(fleet_db):
    # the generated challenge_rate = (captcha+block)/(success+captcha+block)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO rate_governor (scope_key, daily_cap, success_24h, captcha_24h, block_24h) "
                "VALUES ('host:example.com', 100, 8, 1, 1)"
            )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute("SELECT challenge_rate FROM rate_governor WHERE scope_key='host:example.com'")
            rate = cur.fetchone()["challenge_rate"]
    assert abs(rate - 0.2) < 1e-6  # (1+1)/(8+1+1)
