"""v3 schema: loads cleanly against real Postgres, idempotent, all tables + columns present."""
from __future__ import annotations

import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue  # noqa: E402
from applypilot.fleet import schema as fleet_schema  # noqa: E402

EXPECTED_TABLES = {
    "apply_queue", "fleet_config", "fleet_assets",  # base
    "compute_queue", "search_tasks", "linkedin_queue", "rate_governor", "llm_usage",
    "applied_set", "answer_bank", "auth_challenge", "otp_request", "inbox_events",
    "workers", "worker_heartbeat", "poison_jobs", "remote_commands", "autotriage_actions",
    "fleet_decision_policies",
}

CANONICAL_QUEUE_COLUMNS = {
    "decision_id", "policy_version", "decision_action", "qualification_verdict",
    "qualification_score", "qualification_floor", "preference_score", "outcome_score",
    "final_score", "decision_confidence", "decision_created_at",
    "decision_expires_at", "input_hash",
}

CANONICAL_CONSTRAINT_SUFFIXES = {
    "canonical_provenance_ck", "lane_ck", "policy_lane_fk",
}


def _canonical_row(*, table: str, url: str = "canonical", **overrides):
    row = {
        "url": url,
        "application_url": "https://example.test/apply",
        "score": 0.75,
        "lane": "ats" if table == "apply_queue" else "linkedin",
        "decision_id": "decision-1",
        "policy_version": "policy-1",
        "decision_action": "apply",
        "qualification_verdict": "qualified",
        "qualification_score": 0.8,
        "qualification_floor": 0.7,
        "preference_score": 0.6,
        "outcome_score": 0.5,
        "final_score": 0.75,
        "decision_confidence": 0.9,
        "decision_created_at": "2026-07-11T12:00:00Z",
        "decision_expires_at": "2026-07-12T12:00:00Z",
        "input_hash": "sha256:abc",
    }
    row.update(overrides)
    return row


def _insert_canonical(cur, table: str, row: dict) -> None:
    columns = list(row)
    cur.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})",
        [row[column] for column in columns],
    )


def _canonical_schema_objects(cur, table: str) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    cur.execute(
        """
        SELECT c.conname, pg_get_constraintdef(c.oid, true) AS definition
        FROM pg_constraint c
        JOIN pg_class rel ON rel.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = rel.relnamespace
        WHERE n.nspname = current_schema() AND rel.relname = %s
          AND (c.conname LIKE %s OR c.conname LIKE %s OR c.conname LIKE %s)
        """,
        (table, "%canonical_provenance_ck", "%lane_ck", "%policy_lane_fk"),
    )
    constraints = {(r["conname"], r["definition"]) for r in cur.fetchall()}
    cur.execute(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = current_schema() AND tablename = %s
          AND indexname LIKE %s
        """,
        (table, "%canonical_lease"),
    )
    indexes = {(r["indexname"], r["indexdef"]) for r in cur.fetchall()}
    return constraints, indexes


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


def test_constraint_lookup_ignores_same_name_on_other_schema(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS canonical_shadow CASCADE")
        cur.execute("CREATE SCHEMA canonical_shadow")
        cur.execute(
            "CREATE TABLE canonical_shadow.apply_queue ("
            "value INTEGER CONSTRAINT apply_queue_canonical_provenance_ck CHECK (value > 0))"
        )
        cur.execute("ALTER TABLE public.apply_queue DROP CONSTRAINT apply_queue_canonical_provenance_ck")
        conn.commit()

        fleet_schema.ensure_schema_v3(conn)

        cur.execute(
            """
            SELECT count(*) AS count
            FROM pg_constraint c
            JOIN pg_class rel ON rel.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = rel.relnamespace
            WHERE n.nspname = 'public' AND rel.relname = 'apply_queue'
              AND c.conname = 'apply_queue_canonical_provenance_ck'
            """
        )
        assert cur.fetchone()["count"] == 1
        cur.execute("DROP SCHEMA canonical_shadow CASCADE")
        conn.commit()


def test_v3_schema_declares_dedup_repair_actions_once():
    schema = (Path(__file__).resolve().parents[1] / "src" / "applypilot" / "fleet" / "schema_v3.sql").read_text(
        encoding="utf-8"
    )

    assert schema.count("CREATE TABLE IF NOT EXISTS dedup_repair_actions") == 1


def test_v3_schema_declares_autotriage_actions_once():
    schema = (Path(__file__).resolve().parents[1] / "src" / "applypilot" / "fleet" / "schema_v3.sql").read_text(
        encoding="utf-8"
    )

    assert schema.count("CREATE TABLE IF NOT EXISTS autotriage_actions") == 1


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
    for c in (
        "worker_home_ip", "target_host", "lane", "dedup_key", "approved_batch",
        "liveness_check_count", "liveness_consecutive_uncertain",
    ):
        assert c in cols, f"apply_queue missing {c}"
    assert CANONICAL_QUEUE_COLUMNS <= cols


def test_linkedin_queue_has_canonical_provenance(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='linkedin_queue'")
        cols = {r["column_name"] for r in cur.fetchall()}
    assert CANONICAL_QUEUE_COLUMNS <= cols


def test_linkedin_queue_freshness_columns(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='linkedin_queue'")
        cols = {r["column_name"] for r in cur.fetchall()}
    for c in (
        "linkedin_resolve_status",
        "linkedin_resolved_at",
        "linkedin_resolve_error",
        "linkedin_unresolved_kind",
        "linkedin_next_action",
    ):
        assert c in cols, f"linkedin_queue missing {c}"


def test_fleet_config_v3_columns(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='fleet_config'")
        cols = {r["column_name"] for r in cur.fetchall()}
    for c in ("approval_threshold", "approval_policy", "approval_sampling_rate",
              "cost_cap_daily_usd", "cost_cap_total_usd", "pinned_worker_version",
              "canary_enabled", "canary_remaining", "ats_apply_mode",
              "linkedin_canary_enabled", "linkedin_canary_remaining",
              "linkedin_apply_mode", "daily_apply_target", "ats_policy_version",
              "linkedin_policy_version"):
        assert c in cols, f"fleet_config missing {c}"


def test_policy_registry_enforces_lane_status_and_one_active_policy(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version, lane, status) "
            "VALUES ('ats-v1', 'ats', 'active')"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO fleet_decision_policies (policy_version, lane, status) "
                "VALUES ('ats-v2', 'ats', 'active')"
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO fleet_decision_policies (policy_version, lane, status) "
                "VALUES ('bad', 'email', 'active')"
            )


def test_fleet_config_policy_foreign_keys_are_lane_specific(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version, lane, status) "
            "VALUES ('linkedin-v1', 'linkedin', 'active')"
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            cur.execute("UPDATE fleet_config SET ats_policy_version='linkedin-v1' WHERE id=1")


def test_queue_provenance_constraints_and_policy_foreign_key(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version, lane, status) "
            "VALUES ('ats-v1', 'ats', 'active')"
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            _insert_canonical(
                cur,
                "apply_queue",
                _canonical_row(table="apply_queue", policy_version="missing"),
            )
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, application_url, score, decision_action) "
                "VALUES ('u2', 'https://example.test/apply', 1, 'maybe')"
            )


@pytest.mark.parametrize("table", ["apply_queue", "linkedin_queue"])
@pytest.mark.parametrize(
    ("overrides", "case"),
    [
        ({"decision_id": "only", "policy_version": None, "decision_action": None,
          "qualification_verdict": None, "qualification_score": None,
          "qualification_floor": None, "preference_score": None, "outcome_score": None,
          "final_score": None, "decision_confidence": None, "decision_created_at": None,
          "decision_expires_at": None, "input_hash": None}, "decision-id-only"),
        ({"decision_created_at": None}, "missing-created-at"),
        ({"decision_expires_at": None}, "missing-expires-at"),
        ({"outcome_score": None}, "missing-component-score"),
        ({"decision_id": "  "}, "blank-decision-id"),
        ({"policy_version": "  "}, "blank-policy-version"),
        ({"input_hash": "  "}, "blank-hash"),
        ({"decision_expires_at": "2026-07-10T12:00:00Z"}, "reversed-expiry"),
        ({"qualification_score": float("nan")}, "nan-score"),
        ({"decision_confidence": float("inf")}, "infinite-confidence"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_queue_rejects_incomplete_or_invalid_canonical_provenance(fleet_db, table, overrides, case):
    del case
    lane = "ats" if table == "apply_queue" else "linkedin"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version, lane, status) "
            "VALUES ('policy-1', %s, 'active')",
            (lane,),
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_canonical(cur, table, _canonical_row(table=table, **overrides))


@pytest.mark.parametrize("table", ["apply_queue", "linkedin_queue"])
def test_queue_accepts_legacy_all_null_and_complete_canonical_rows(fleet_db, table):
    lane = "ats" if table == "apply_queue" else "linkedin"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO {table} (url, application_url, score) VALUES ('legacy', 'https://example.test', 1)"
        )
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version, lane, status) "
            "VALUES ('policy-1', %s, 'active')",
            (lane,),
        )
        _insert_canonical(cur, table, _canonical_row(table=table))


@pytest.mark.parametrize("table", ["apply_queue", "linkedin_queue"])
def test_existing_queue_is_explicitly_upgraded_to_fresh_canonical_schema(fleet_pg, table):
    lane = "ats" if table == "apply_queue" else "linkedin"
    canonical_index = f"idx_{table}_canonical_lease"
    with pgqueue.connect(fleet_pg) as conn:
        pgqueue.ensure_schema(conn)
        fleet_schema.ensure_schema_v3(conn)
        with conn.cursor() as cur:
            fresh_objects = _canonical_schema_objects(cur, table)
        with conn.cursor() as cur:
            cur.execute(f"TRUNCATE {table}")
            cur.execute(f"DROP INDEX IF EXISTS idx_{table}_canonical_lease")
            for column in CANONICAL_QUEUE_COLUMNS:
                cur.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column} CASCADE")
            cur.execute(
                f"INSERT INTO {table} (url, application_url, score) "
                "VALUES ('legacy', 'https://linkedin.com/jobs/view/1', 1)"
            )
        conn.commit()

        fleet_schema.ensure_schema_v3(conn)

        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name=%s",
                (table,),
            )
            cols = {r["column_name"] for r in cur.fetchall()}
            cur.execute(f"SELECT lane, decision_id FROM {table} WHERE url='legacy'")
            legacy = cur.fetchone()
            upgraded_objects = _canonical_schema_objects(cur, table)
        conn.commit()

    assert CANONICAL_QUEUE_COLUMNS <= cols
    assert legacy == {"lane": lane, "decision_id": None}
    assert upgraded_objects == fresh_objects
    constraint_names = {name for name, _ in upgraded_objects[0]}
    assert constraint_names == {f"{table}_{suffix}" for suffix in CANONICAL_CONSTRAINT_SUFFIXES}
    assert {name for name, _ in upgraded_objects[1]} == {canonical_index}
    index_definition = next(iter(upgraded_objects[1]))[1]
    for column in CANONICAL_QUEUE_COLUMNS:
        assert column in index_definition
    assert "decision_action = 'apply'" in index_definition
    assert "qualification_verdict = 'qualified'" in index_definition


def test_autotriage_actions_schema(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='autotriage_actions'")
        cols = {r["column_name"] for r in cur.fetchall()}
    for c in (
        "url", "worker_id", "chosen_action", "decision_source", "confidence",
        "action_status", "prior_status", "prior_apply_error", "evidence",
        "how_to_reverse",
    ):
        assert c in cols, f"autotriage_actions missing {c}"


def test_fleet_console_audit_schema(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name='fleet_console_audit'")
        cols = {r["column_name"] for r in cur.fetchall()}
    for c in ("id", "action", "actor", "lane", "target", "message", "ok", "created_at"):
        assert c in cols, f"fleet_console_audit missing {c}"


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
