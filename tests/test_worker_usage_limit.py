"""Residential-fleet worker: usage/session-limit walls are RE-QUEUED, not parked.

A turn-1 usage/quota wall provably never touched the application form (launcher returns
failed:usage_limit only when application-touching tool calls == 0), so the job must go
back to 'queued' to be re-leased later -- NOT crash_unconfirmed (which is permanent +
dedup-polluting) and NOT a phantom apply. Runs against the disposable test Postgres
(fleet_db fixture).
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


def _seed_linkedin_queued(conn, url):
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET paused=FALSE, linkedin_canary_enabled=FALSE WHERE id=1"
        )
        cur.execute(
            "INSERT INTO linkedin_queue (url, company, title, application_url, score, "
            "status, lane, approved_batch, dedup_key) "
            "VALUES (%s,'Acme','Role',%s,'9','queued','ats','b1',%s)",
            (url, url, "dk-" + url),
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


def test_tick_linkedin_usage_limit_requeues_not_failed(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_linkedin_queued(conn, "https://linkedin.test/ul")
    loop = WorkerLoop(
        lambda: pgqueue.connect(fleet_db),
        "w-li-ul",
        home_ip="1.1.1.1",
        role="linkedin",
        public_ip="1.1.1.1",
        owner_ip="1.1.1.1",
        on_owner_machine=True,
        apply_fn=lambda job: {"run_status": "failed:usage_limit", "est_cost_usd": 0.0},
    )

    assert loop.run_once()["action"] == "usage_limit"

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, lease_owner, attempts, apply_error "
            "FROM linkedin_queue WHERE url='https://linkedin.test/ul'"
        )
        row = cur.fetchone()
        assert row["status"] == "queued"
        assert row["lease_owner"] is None
        assert row["attempts"] == 0
        assert row["apply_error"] == "failed:usage_limit"
        cur.execute("SELECT count(*) AS n FROM applied_set WHERE dedup_key='dk-https://linkedin.test/ul'")
        assert cur.fetchone()["n"] == 0


def test_tick_linkedin_usage_limit_refunds_canary_and_account_reservation(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_linkedin_queued(conn, "https://linkedin.test/canary")
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE fleet_config SET linkedin_canary_enabled=TRUE, "
                "linkedin_canary_remaining=1 WHERE id=1"
            )
        conn.commit()
    loop = WorkerLoop(
        lambda: pgqueue.connect(fleet_db),
        "w-li-refund",
        home_ip="1.1.1.1",
        role="linkedin",
        public_ip="1.1.1.1",
        owner_ip="1.1.1.1",
        on_owner_machine=True,
        apply_fn=lambda job: {"run_status": "failed:usage_limit", "est_cost_usd": 0.0},
    )

    assert loop.run_once()["action"] == "usage_limit"

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT linkedin_canary_enabled, linkedin_canary_remaining "
            "FROM fleet_config WHERE id=1"
        )
        cfg = cur.fetchone()
        assert cfg["linkedin_canary_enabled"] is True
        assert cfg["linkedin_canary_remaining"] == 1
        cur.execute(
            "SELECT count_24h, last_applied_at FROM rate_governor "
            "WHERE scope_key='account:linkedin'"
        )
        acct = cur.fetchone()
        assert acct["count_24h"] == 0
        assert acct["last_applied_at"] is None
