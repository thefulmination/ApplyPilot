"""Residential-fleet worker: usage/session-limit walls are RE-QUEUED, not parked.

A turn-1 usage/quota wall provably never touched the application form (launcher returns
failed:usage_limit only when tool_calls == 0), so the job must go back to 'queued' to be
re-leased later -- NOT crash_unconfirmed (which is permanent + dedup-polluting) and NOT a
phantom apply. Runs against the disposable test Postgres (fleet_db fixture).
"""
from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import queue
from applypilot.fleet.worker import WorkerLoop


def _seed_queued(conn, url, domain):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_queue (url, application_url, score, status, lane, "
            "approved_batch, dedup_key, apply_domain) "
            "VALUES (%s,'http://x/y','9','queued','ats','b1',%s,%s)",
            (url, "dk-" + url, domain),
        )
    conn.commit()


def test_requeue_apply_returns_leased_job_to_queued(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_queued(conn, "ru", "acme-r.com")
    # lease it so there is a lease_owner to guard on, then requeue.
    with pgqueue.connect(fleet_db) as conn:
        job = queue.lease_apply(conn, "w-req", home_ip="1.1.1.1")
        assert job["url"] == "ru"
    with pgqueue.connect(fleet_db) as conn:
        landed = queue.requeue_apply(conn, "w-req", "ru", apply_error="failed:usage_limit")
        assert landed is True
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status, lease_owner, attempts FROM apply_queue WHERE url='ru'")
        row = cur.fetchone()
        assert row["status"] == "queued"        # re-leasable
        assert row["lease_owner"] is None
        assert row["attempts"] != 99             # not pinned like crash_unconfirmed


def test_requeue_apply_guards_on_lease_owner(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_queued(conn, "rg", "acme-g.com")
    with pgqueue.connect(fleet_db) as conn:
        queue.lease_apply(conn, "owner", home_ip="1.1.1.1")
    with pgqueue.connect(fleet_db) as conn:
        # a DIFFERENT worker must not be able to requeue a lease it does not hold
        assert queue.requeue_apply(conn, "intruder", "rg", apply_error="x") is False


def test_tick_apply_usage_limit_requeues_not_crash(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_queued(conn, "ul", "acme-u.com")
    loop = WorkerLoop(
        lambda: pgqueue.connect(fleet_db), "w-ul", home_ip="1.1.1.1", role="apply",
        apply_fn=lambda job: {"run_status": "failed:usage_limit", "est_cost_usd": 0.0},
    )
    assert loop.run_once()["action"] == "usage_limit"
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM apply_queue WHERE url='ul'")
        assert cur.fetchone()["status"] == "queued"          # re-leasable, not crash_unconfirmed
        cur.execute("SELECT count(*) AS n FROM applied_set WHERE dedup_key='dk-ul'")
        assert cur.fetchone()["n"] == 0                       # never entered the dedup ledger
