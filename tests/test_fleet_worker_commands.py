"""PG-backed tests: remote_commands are finally CONSUMED by the worker loop.

Before 2026-07-03 the command channel was scaffold-only: the watchdog issued
'restart' commands that no worker ever polled (audit finding). run_once now
handles commands at the top of each tick -- strictly BETWEEN jobs. Uses the
shared ``fleet_db`` fixture; discovery role with an empty queue gives a pure
idle tick to observe command behavior against.
"""
from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import heartbeat
from applypilot.fleet.worker import WorkerLoop


def _mk_loop(fleet_db, wid="m2-0"):
    return WorkerLoop(lambda: pgqueue.connect(fleet_db), wid, home_ip="1.2.3.4",
                      role="discovery", search_fn=lambda task: [], machine_owner="m2")


def test_no_commands_normal_idle(fleet_db):
    assert _mk_loop(fleet_db).run_once()["action"] == "idle"


def test_restart_returns_stop_and_hard_acks(fleet_db):
    loop = _mk_loop(fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        cmd_id = heartbeat.issue_command(conn, "m2-0", "restart")
    res = loop.run_once()
    assert res == {"action": "stop", "command": "restart"}
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT acked_at FROM remote_commands WHERE id=%s", (cmd_id,))
            assert cur.fetchone()["acked_at"] is not None  # direct command hard-closed
    # acked -> not re-delivered: next tick is a normal idle
    assert loop.run_once()["action"] == "idle"


def test_stale_direct_restart_from_previous_process_is_acked_but_ignored(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        cmd_id = heartbeat.issue_command(conn, "m2-0", "restart")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE remote_commands SET issued_at = now() - interval '10 minutes' "
                "WHERE id=%s",
                (cmd_id,),
            )
        conn.commit()
    loop = _mk_loop(fleet_db)
    assert loop.run_once()["action"] == "idle"
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT acked_at FROM remote_commands WHERE id=%s", (cmd_id,))
            assert cur.fetchone()["acked_at"] is not None


def test_broadcast_drain_reaches_every_worker(fleet_db):
    w0, w1 = _mk_loop(fleet_db, "m2-0"), _mk_loop(fleet_db, "m2-1")
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.issue_command(conn, "*", "drain")
    assert w0.run_once() == {"action": "stop", "command": "drain"}
    assert w1.run_once() == {"action": "stop", "command": "drain"}  # not consumed by w0
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM command_acks")
            assert cur.fetchone()["n"] == 2


def test_pause_idles_without_leasing_then_resume(fleet_db):
    loop = _mk_loop(fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.issue_command(conn, "m2-0", "pause")
    assert loop.run_once()["action"] == "paused"
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT state FROM worker_heartbeat WHERE worker_id='m2-0'")
            assert cur.fetchone()["state"] == "paused"
    assert loop.run_once()["action"] == "paused"  # stays paused across ticks
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.issue_command(conn, "m2-0", "resume")
    assert loop.run_once()["action"] == "idle"


def test_self_update_is_acked_noop(fleet_db):
    loop = _mk_loop(fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        cmd_id = heartbeat.issue_command(conn, "m2-0", "self_update", target_version="v9")
    assert loop.run_once()["action"] == "idle"  # no stop, no pause
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT acked_at FROM remote_commands WHERE id=%s", (cmd_id,))
            assert cur.fetchone()["acked_at"] is not None  # acked so it never piles up


def test_pause_then_restart_same_batch_stops(fleet_db):
    loop = _mk_loop(fleet_db)
    with pgqueue.connect(fleet_db) as conn:
        heartbeat.issue_command(conn, "m2-0", "pause")
        heartbeat.issue_command(conn, "m2-0", "restart")
    assert loop.run_once()["action"] == "stop"
