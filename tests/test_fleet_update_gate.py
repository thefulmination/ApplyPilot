"""PG-backed tests for the auto-updater between-jobs gate (update_gate module).

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema applied +
truncated). The gate is the safety interlock that keeps -AutoUpdate from yanking code
out from under a mid-apply worker, so it is exercised against real SQL — including the
two live-verified traps: challenge-PARKED leases (expiry ~10 years out) and ORPHANED
leases whose owning worker is dead. Neither may block an update.
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import heartbeat, update_gate


def _lease(cur, url: str, owner: str, expires_sql: str, status: str = "leased"):
    cur.execute(
        "INSERT INTO apply_queue (url, application_url, score, lane, status, "
        "lease_owner, lease_expires_at) "
        f"VALUES (%s, %s, 5.0, 'ats', %s, %s, {expires_sql})",
        (url, url, status, owner))


def test_idle_when_no_rows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        assert update_gate.busy_reasons(conn, "m2") == []


def test_busy_on_fresh_nonidle_heartbeat(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        assert update_gate.busy_reasons(conn, "m2") == ["heartbeat:m2-0:applying"]


def test_idle_heartbeat_and_other_label_do_not_block(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        heartbeat.beat(conn, "m4-0", machine_owner="m4", role="apply", state="applying")
        # label prefix anchors with '-': 'm2x-0' must not match label 'm2'
        heartbeat.beat(conn, "m2x-0", machine_owner="m2x", role="apply", state="applying")
        assert update_gate.busy_reasons(conn, "m2") == []


def test_paused_worker_does_not_block(fleet_db):
    # a remotely-paused worker holds no job by definition -> safe to update
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="paused")
        assert update_gate.busy_reasons(conn, "m2") == []


def test_discovery_worker_ids_match_label_prefix(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-disc-1", machine_owner="m2", role="discovery",
                       state="applying")
        assert update_gate.busy_reasons(conn, "m2") == ["heartbeat:m2-disc-1:applying"]


def test_stale_nonidle_heartbeat_does_not_block(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="applying")
        with conn.cursor() as cur:
            cur.execute("UPDATE worker_heartbeat SET last_beat = now() - interval '10 minutes' "
                        "WHERE worker_id = 'm2-0'")
        assert update_gate.busy_reasons(conn, "m2") == []


def test_live_lease_with_live_owner_blocks(fleet_db):
    # THE race the lease check exists for: worker leased a job but its heartbeat still
    # says idle (beats are ~20s apart). Owner alive + lease live -> BUSY.
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        with conn.cursor() as cur:
            _lease(cur, "https://x.test/j1", "m2-0", "now() + interval '10 minutes'")
        assert update_gate.busy_reasons(conn, "m2") == ["apply_queue:live_leases:1"]


def test_orphan_lease_dead_owner_does_not_block(fleet_db):
    # live-verified 2026-07-03: leases from workers dead since 6/30 must not block --
    # there is no process to interrupt; reclaim owns these rows.
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            _lease(cur, "https://x.test/j2", "m2-0", "now() + interval '10 minutes'")
        assert update_gate.busy_reasons(conn, "m2") == []


def test_parked_far_future_lease_does_not_block(fleet_db):
    # live-verified 2026-07-03: challenge-parked rows hold lease_expires_at ~10 years
    # out BY DESIGN (m2 had 105 of them). Even with a live owner they must not block.
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        with conn.cursor() as cur:
            _lease(cur, "https://x.test/j3", "m2-0", "now() + interval '10 years'")
        assert update_gate.busy_reasons(conn, "m2") == []


def test_expired_or_foreign_lease_does_not_block(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        heartbeat.beat(conn, "m4-0", machine_owner="m4", role="apply", state="idle")
        with conn.cursor() as cur:
            _lease(cur, "https://x.test/j4", "m2-0", "now() - interval '1 minute'")
            _lease(cur, "https://x.test/j5", "m4-0", "now() + interval '10 minutes'")
        assert update_gate.busy_reasons(conn, "m2") == []


def test_terminal_status_with_future_expiry_does_not_block(fleet_db):
    # a finished row may keep lease_owner + a future lease_expires_at; status gates it
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.beat(conn, "m2-0", machine_owner="m2", role="apply", state="idle")
        with conn.cursor() as cur:
            _lease(cur, "https://x.test/j6", "m2-0", "now() + interval '10 minutes'",
                   status="applied")
        assert update_gate.busy_reasons(conn, "m2") == []
