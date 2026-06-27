# tests/test_fleet_monitor.py
from applypilot.apply import pgqueue
from applypilot.fleet import monitor


DENIED = ["resolve_challenge", "set_cost_cap", "set_cost_cap_total", "resume_scope",
          "clear_breaker", "approve_job", "approve", "apply", "submit", "lease_apply"]


def test_monitor_actions_allow_ops_work(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO rate_governor (scope_key, min_gap_seconds) VALUES ('host:z.com',5)")
        conn.commit()
        ma = monitor.MonitorActions(conn)
        assert ma.restart_worker("wA") == 1            # one command row enqueued
        ma.pause_scope("host:z.com")
        with conn.cursor() as cur:
            cur.execute("SELECT breaker_state FROM rate_governor WHERE scope_key='host:z.com'")
            assert cur.fetchone()["breaker_state"] == "paused"
        assert ma.report("all good") == "all good"


def test_monitor_actions_deny_ops_absent_from_surface(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        ma = monitor.MonitorActions(conn)
        for name in DENIED:
            assert not hasattr(ma, name), f"DENY op {name!r} must not be reachable on MonitorActions"
