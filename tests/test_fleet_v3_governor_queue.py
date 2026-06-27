"""Governor + governed claims, against real Postgres.

The lease tests are the ones that matter most: a double-grab, a re-applied posting,
or a lease that ignores a tripped breaker is a real-world account/IP risk.
"""
from __future__ import annotations

import threading
import time

from applypilot.apply import pgqueue
from applypilot.fleet import config as fcfg
from applypilot.fleet import governor
from applypilot.fleet import queue


def _seed_apply(conn, url, *, host, score=5.0, company="Co", title="Role", approved="b1", dedup_key=None):
    queue.push_apply_jobs(conn, [{
        "url": url, "company": company, "title": title,
        "application_url": f"https://{host}/jobs/x", "score": score,
        "target_host": host, "dedup_key": dedup_key,
    }], approved_batch=approved)


# ---- approval gate (R11) -------------------------------------------------------

def test_lease_requires_approval(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "u1", host="greenhouse.io", approved=None)  # not approved
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None
        assert queue.approve_jobs(conn, ["u1"], "batchA") == 1
        leased = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        assert leased and leased["url"] == "u1"


# ---- cross-board dedup (R9) ----------------------------------------------------

def test_lease_dedup_guard_blocks_reapply(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        # same dedup_key on two boards
        _seed_apply(conn, "u-gh", host="greenhouse.io", company="Acme", title="Chief of Staff")
        _seed_apply(conn, "u-lev", host="lever.co", company="Acme, Inc", title="CoS")
        a = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        assert a is not None
        queue.write_apply_result(conn, "w1", a["url"], status="applied",
                                 target_host=a["target_host"], home_ip="1.1.1.1")
        # the OTHER board's posting (same company+role) must not be leasable now
        b = queue.lease_apply(conn, "w2", home_ip="2.2.2.2")
        assert b is None, "cross-board dedup should block the duplicate posting"


# ---- per-host min-gap ----------------------------------------------------------

def test_per_host_gap_blocks_then_allows(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "h1", host="greenhouse.io", score=9, title="Role A")
        _seed_apply(conn, "h2", host="greenhouse.io", score=8, title="Role B")
        a = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        queue.write_apply_result(conn, "w1", a["url"], status="applied",
                                 target_host="greenhouse.io", home_ip="1.1.1.1")
        # same host just hit -> gap blocks the second
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None
        # wind the host's last_applied_at back past the gap
        with conn.cursor() as cur:
            cur.execute("UPDATE rate_governor SET last_applied_at = now() - interval '300 seconds' "
                        "WHERE scope_key='host:greenhouse.io'")
        conn.commit()
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is not None


# ---- adaptive circuit-breaker (R6) ---------------------------------------------

def test_breaker_pauses_high_challenge_host(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        sk = governor.host_scope("walls.io")
        governor.ensure_scope(conn, sk, min_gap_seconds=1)
        # 2 success, 8 captcha -> challenge_rate 0.8 >> 0.4*1.5 -> paused
        governor.record_outcome(conn, [sk], "success")
        governor.record_outcome(conn, [sk], "success")
        for _ in range(8):
            governor.record_outcome(conn, [sk], "captcha")
        changed = dict(governor.evaluate_breakers(conn))
        assert changed.get(sk) == "paused"
        # a job on that host is not leasable while paused
        _seed_apply(conn, "p1", host="walls.io")
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None


def test_breaker_demotes_on_hard_blocks(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        sk = governor.home_ip_scope("9.9.9.9")
        for _ in range(3):
            governor.record_outcome(conn, [sk], "block")
        changed = dict(governor.evaluate_breakers(conn))
        assert changed.get(sk) == "demoted"


def test_breaker_recovers_after_cooldown(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        sk = governor.host_scope("flappy.io")
        governor.ensure_scope(conn, sk)
        for _ in range(8):
            governor.record_outcome(conn, [sk], "captcha")
        governor.evaluate_breakers(conn, cool_seconds=0)  # breaker_until = now
        time.sleep(0.05)
        cleared = governor.clear_expired_breakers(conn)
        assert sk in cleared
        with pgqueue.connect(fleet_db) as c2, c2.cursor() as cur:
            cur.execute("SELECT breaker_state FROM rate_governor WHERE scope_key=%s", (sk,))
            assert cur.fetchone()["breaker_state"] == "ok"


# ---- write_apply_result bookkeeping --------------------------------------------

def test_write_result_records_outcome_and_applied_set(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        _seed_apply(conn, "r1", host="greenhouse.io", company="Beta", title="Data Scientist")
        a = queue.lease_apply(conn, "w1", home_ip="1.2.3.4")
        ok = queue.write_apply_result(conn, "w1", a["url"], status="applied",
                                      target_host="greenhouse.io", home_ip="1.2.3.4", est_cost_usd=0.5)
        assert ok
        with conn.cursor() as cur:
            cur.execute("SELECT success_24h, count_24h FROM rate_governor WHERE scope_key='global'")
            g = cur.fetchone()
            assert g["success_24h"] == 1 and g["count_24h"] == 1
            cur.execute("SELECT count(*) AS n FROM applied_set")
            assert cur.fetchone()["n"] == 1
        # a stale worker can't double-close
        assert queue.write_apply_result(conn, "intruder", a["url"], status="applied",
                                        target_host="greenhouse.io", home_ip="1.2.3.4") is False


# ---- compute cost cap (R14) ----------------------------------------------------

def test_compute_lease_and_cost_cap(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        queue.push_compute_jobs(conn, [{"url": "c1", "task": "score", "payload": {"x": 1}}])
        c = queue.lease_compute(conn, "w1")
        assert c and c["task"] == "score"
        queue.write_compute_result(conn, "w1", "c1", result={"fit": 7}, cost_usd=0.10, model="deepseek")
        # set a tiny daily cap below spend -> next compute lease refused
        fcfg.set_cost_caps(conn, daily_usd=0.05)
        queue.push_compute_jobs(conn, [{"url": "c2", "task": "audit"}])
        assert queue.lease_compute(conn, "w1") is None


# ---- recurring search scheduler (RF3) ------------------------------------------

def test_search_recurs_after_completion(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO search_tasks (task_id, query, board, location, cadence_seconds) "
                        "VALUES ('t1','chief of staff','greenhouse','remote', 3600)")
        conn.commit()
        s = queue.lease_search(conn, "w1")
        assert s and s["task_id"] == "t1"
        # nothing else due
        assert queue.lease_search(conn, "w2") is None
        queue.complete_search(conn, "w1", "t1", result_count=12, board="greenhouse")
        # rescheduled into the future -> not immediately re-leasable
        assert queue.lease_search(conn, "w1") is None
        with conn.cursor() as cur:
            cur.execute("SELECT status, result_count, next_due_at > now() AS future FROM search_tasks WHERE task_id='t1'")
            r = cur.fetchone()
            assert r["status"] == "queued" and r["result_count"] == 12 and r["future"] is True


# ---- LinkedIn mutex (R1) -------------------------------------------------------

def test_linkedin_owner_ip_and_mutex(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        governor.ensure_scope(conn, governor.LINKEDIN_ACCOUNT, daily_cap=20, min_gap_seconds=300)
        with conn.cursor() as cur:
            for i in range(2):
                cur.execute("INSERT INTO linkedin_queue (url, company, title, application_url, score, lane, approved_batch) "
                            "VALUES (%s,'Co','Role','https://linkedin.com/jobs/%s', %s, 'linkedin', 'b1')",
                            (f"li{i}", i, 9 - i))
        conn.commit()
        # wrong IP -> refused
        assert queue.lease_linkedin(conn, "w1", public_ip="3.3.3.3", owner_ip="1.1.1.1") is None
        # owner IP -> leases one
        a = queue.lease_linkedin(conn, "w1", public_ip="1.1.1.1", owner_ip="1.1.1.1")
        assert a is not None
        # record the apply -> stamps the account mutex -> second machine blocked by gap
        governor.record_outcome(conn, [governor.LINKEDIN_ACCOUNT], "success", bump_cap=True)
        b = queue.lease_linkedin(conn, "w2", public_ip="1.1.1.1", owner_ip="1.1.1.1")
        assert b is None, "account:linkedin mutex must serialize -> never two concurrent sessions"


# ---- lease atomicity under concurrency (the safety property) -------------------

def test_apply_lease_atomic_under_concurrency(fleet_db):
    n_jobs, n_workers = 24, 6
    with pgqueue.connect(fleet_db) as conn:
        for i in range(n_jobs):
            _seed_apply(conn, f"j{i}", host=f"host{i}.io", score=float(i))  # distinct hosts -> gap never blocks

    grabbed: list[str] = []
    lock = threading.Lock()

    def worker(wid):
        with pgqueue.connect(fleet_db) as c:
            while True:
                row = queue.lease_apply(c, wid, home_ip="1.1.1.1")
                if row is None:
                    return
                with lock:
                    grabbed.append(row["url"])

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(n_workers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(grabbed) == n_jobs
    assert len(set(grabbed)) == n_jobs, "a job was double-grabbed"
