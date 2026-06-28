# tests/test_fleet_apply_lane.py
import concurrent.futures as cf

from applypilot.apply import pgqueue


def _seed_approved_apply_rows(conn, n, *, batch="b1"):
    with conn.cursor() as cur:
        for i in range(n):
            cur.execute(
                "INSERT INTO apply_queue (url, application_url, score, status, lane, approved_batch, dedup_key, apply_domain) "
                "VALUES (%s,%s,%s,'queued','ats',%s,%s,'acme.com')",
                (f"u{i}", f"http://acme.com/{i}", 9.0 - i*0.01, batch, f"dk{i}"))
        conn.commit()


def test_canary_columns_exist_and_default(fleet_db):
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT canary_enabled, canary_remaining FROM fleet_config WHERE id=1")
        row = cur.fetchone()
    assert row["canary_enabled"] is False
    assert row["canary_remaining"] is None


def test_lease_blocked_when_paused(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_approved_apply_rows(conn, 1)
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
        conn.commit()
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None


def test_canary_caps_total_leases_fleetwide(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_approved_apply_rows(conn, 5)
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET canary_enabled=TRUE, canary_remaining=2, paused=FALSE WHERE id=1")
        conn.commit()
        a = queue.lease_apply(conn, "w1", home_ip="1.1.1.1")
        b = queue.lease_apply(conn, "w2", home_ip="1.1.1.1")
        c = queue.lease_apply(conn, "w3", home_ip="1.1.1.1")
    assert a is not None and b is not None and c is None  # exactly 2 leases
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT canary_remaining, paused FROM fleet_config WHERE id=1")
        row = cur.fetchone()
        assert row["canary_remaining"] == 0 and row["paused"] is True


def test_canary_atomic_under_concurrency(fleet_db):
    # N concurrent workers, K=1 -> EXACTLY one lease succeeds (no overshoot).
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_approved_apply_rows(conn, 8)
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET canary_enabled=TRUE, canary_remaining=1, paused=FALSE WHERE id=1")
        conn.commit()

    def _lease(i):
        with pgqueue.connect(fleet_db) as c:
            return queue.lease_apply(c, f"w{i}", home_ip="1.1.1.1") is not None

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(_lease, range(8)))
    assert sum(results) == 1  # exactly one of eight workers leased


def test_canary_disabled_does_not_decrement(fleet_db):
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn:
        _seed_approved_apply_rows(conn, 1)  # canary disabled by fixture default
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is not None
        with conn.cursor() as cur:
            cur.execute("SELECT canary_remaining FROM fleet_config WHERE id=1")
            assert cur.fetchone()["canary_remaining"] is None  # untouched


def test_lease_blocked_when_spend_cap_breached(fleet_db):
    # G5 as a HARD lease guard: cumulative apply spend >= spend_cap_usd -> no lease.
    from applypilot.fleet import queue
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        _seed_approved_apply_rows(conn, 1)
        cur.execute("UPDATE apply_queue SET est_cost_usd = 5.0 WHERE url='u0'")  # already-spent row
        # add a second leasable row so the SUM (5.0) is what blocks, not an empty queue
        cur.execute("INSERT INTO apply_queue (url, application_url, score, status, lane, approved_batch, dedup_key, apply_domain) "
                    "VALUES ('u1','http://acme.com/1','8','queued','ats','b1','dk1','acme.com')")
        cur.execute("UPDATE fleet_config SET spend_cap_usd = 1.0 WHERE id=1")
        conn.commit()
        assert queue.lease_apply(conn, "w1", home_ip="1.1.1.1") is None  # 5.0 >= 1.0 cap
