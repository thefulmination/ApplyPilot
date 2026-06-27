# tests/test_fleet_watchdog.py
from applypilot.apply import pgqueue
from applypilot.fleet import watchdog, queue


def _seed_expired_compute(conn, url="c1"):
    # queued -> leased with an already-expired lease (simulates a crashed compute worker)
    with conn.cursor() as cur:
        cur.execute("INSERT INTO compute_queue (url, task, status, lease_owner, lease_expires_at, attempts) "
                    "VALUES (%s,'score','leased','wDead', now() - interval '5 minutes', 1)", (url,))
    conn.commit()


def _seed_expired_search(conn, task_id="t1"):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO search_tasks (task_id, query, board, status, lease_owner, lease_expires_at, next_due_at) "
                    "VALUES (%s,'cos','indeed','leased','wDead', now() - interval '5 minutes', now())", (task_id,))
    conn.commit()


def test_watchdog_tick_reclaims_crashed_leases(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        _seed_expired_compute(conn)
        _seed_expired_search(conn)
        summary = watchdog.watchdog_tick(conn, cfg)
    assert summary["reclaimed_compute"] == 1
    assert summary["reclaimed_search"] == 1
    assert summary["reclaimed_apply"] == 0  # apply_queue empty in this test
    # the reclaimed compute row is back to 'queued'
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT status FROM compute_queue WHERE url='c1'")
        assert cur.fetchone()["status"] == "queued"


def test_watchdog_tick_beats_own_liveness(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        watchdog.watchdog_tick(conn, cfg)
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT role, state FROM worker_heartbeat WHERE worker_id='watchdog'")
        row = cur.fetchone()
    assert row is not None and row["role"] == "watchdog"
