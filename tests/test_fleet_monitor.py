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


def test_health_report_has_sections_and_flags_anomaly():
    snapshot = {
        "machines": [
            {"worker_id": "w1", "role": "compute", "state": "idle", "last_beat": "2026-06-27T04:00:00Z"},
            {"worker_id": "w2", "role": "apply", "state": "applying", "last_beat": None},  # offline
        ],
        "governor": [
            {"scope_key": "host:ok.com", "breaker_state": "ok", "challenge_rate": 0.05, "count_24h": 10},
            {"scope_key": "host:bad.com", "breaker_state": "ok", "challenge_rate": 0.55, "count_24h": 20},  # anomaly
        ],
        "queue_depth": {"apply": {"queued": 3}, "compute": {"queued": 7}, "search": {}, "linkedin": {}},
        "captcha_backlog": 2,
        "quarantine": 1,
        "spend_today": 9.5,
    }
    report = monitor.build_health_report(snapshot, captcha_threshold=0.4, cost_cap_total=10.0)
    for section in ("MACHINES", "QUEUES", "GOVERNOR", "CAPTCHA BACKLOG", "SPEND", "NEEDS YOUR DECISION"):
        assert section in report
    # the high-challenge scope and the offline worker both surface as anomalies
    assert "host:bad.com" in report
    assert "w2" in report
    # spend 9.5 of 10.0 cap (>=90%) is flagged
    assert "cap" in report.lower()


def test_health_report_clean_when_no_anomalies():
    snapshot = {
        "machines": [{"worker_id": "w1", "role": "compute", "state": "idle", "last_beat": "2026-06-27T04:00:00Z"}],
        "governor": [{"scope_key": "host:ok.com", "breaker_state": "ok", "challenge_rate": 0.0, "count_24h": 5}],
        "queue_depth": {"apply": {}, "compute": {}, "search": {}, "linkedin": {}},
        "captcha_backlog": 0, "quarantine": 0, "spend_today": 1.0,
    }
    report = monitor.build_health_report(snapshot, captcha_threshold=0.4, cost_cap_total=100.0)
    assert "NEEDS YOUR DECISION" in report
    assert "none" in report.lower()  # the decision section says nothing needs attention
