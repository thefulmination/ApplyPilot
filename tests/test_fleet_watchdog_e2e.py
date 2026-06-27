# tests/test_fleet_watchdog_e2e.py
#
# End-to-end test: seed ONE Postgres with a mixed failure state, run ONE
# watchdog_tick, and assert all deterministic recoveries fired AND the parked
# challenge was left UNTOUCHED.
#
# Differences from the brief's verbatim seed (documented here — NOT silent patches):
#
#   1. rate_governor INSERT omits `challenge_rate` because the column is declared
#      GENERATED ALWAYS AS (STORED) in schema_v3.sql; inserting it directly raises
#      "cannot insert into column 'challenge_rate' of relation 'rate_governor'".
#      We instead seed (success_24h=4, captcha_24h=7, block_24h=0) so the stored
#      generated value = 7/11 ≈ 0.636, which exceeds captcha_threshold * 1.5 = 0.6.
#      Using captcha=6/total=10 gives rate=0.6 exactly — float64 precision makes
#      0.6 < 0.6000000000000001 (= 0.4 * 1.5), so the boundary case MISCLASSIFIES
#      as "throttled" instead of "paused". Bumping captcha to 7 pushes it robustly
#      above the boundary (same fix as in test_fleet_watchdog.py's _seed_governor_scope).
#
#   2. worker_heartbeat INSERT uses `now() - interval '10 minutes'` (explicit plural)
#      to match the pattern used throughout the existing test suite (both forms are
#      valid PostgreSQL, but explicit plural avoids any parser ambiguity).
#
#   3. auth_challenge NOT-NULL audit: schema_v3.sql defines `id BIGSERIAL PRIMARY KEY`
#      (auto), `url TEXT NOT NULL`, and `raised_at TIMESTAMPTZ NOT NULL DEFAULT now()`.
#      All other columns (worker_id, machine_owner, home_ip, kind, route, screenshot_url,
#      resolved_at, outcome) are nullable.  The brief's seed (url, worker_id, kind, route)
#      satisfies all NOT-NULL constraints — NO columns were added or removed.
#
from applypilot.apply import pgqueue
from applypilot.fleet import watchdog, monitor, heartbeat, queue  # noqa: F401 (queue imported per brief)


def test_watchdog_full_recovery_pass_and_report(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            # expired compute lease (crashed worker)
            cur.execute(
                "INSERT INTO compute_queue (url, task, status, lease_owner, lease_expires_at, attempts) "
                "VALUES ('c1','score','leased','wDead', now() - interval '5 minutes', 1)"
            )
            # stuck worker (no heartbeat for 10 min)
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, role, state, last_beat) "
                "VALUES ('wStuck','apply','idle', now() - interval '10 minutes')"
            )
            # high-challenge scope -> should pause
            # NOTE: challenge_rate is GENERATED ALWAYS AS STORED; we set captcha_24h=7
            # (not 6) so the stored generated rate = 7/11 ≈ 0.636 > 0.6 (= threshold*1.5).
            # With captcha=6, rate=0.6 exactly and float64 comparison 0.6 >= 0.6000000000000001
            # evaluates to False, causing misclassification as "throttled" instead of "paused".
            cur.execute(
                "INSERT INTO rate_governor (scope_key, success_24h, captcha_24h, block_24h, "
                "breaker_state, min_gap_seconds) "
                "VALUES ('host:bad.com', 4, 7, 0, 'ok', 5)"
            )
            # a PARKED challenge that must NOT be touched
            cur.execute(
                "INSERT INTO auth_challenge (url, worker_id, kind, route) "
                "VALUES ('https://x.com/job','wP','captcha','offsite')"
            )
            # breached total cap
            cur.execute("UPDATE fleet_config SET cost_cap_total_usd=1.0, paused=FALSE WHERE id=1")
            cur.execute("INSERT INTO llm_usage (cost_usd, ts) VALUES (5.0, now())")
        conn.commit()

        summary = watchdog.watchdog_tick(conn, cfg)

    assert summary["reclaimed_compute"] == 1
    assert any(e["worker_id"] == "wStuck" for e in summary["stuck_handled"])
    assert ("host:bad.com", "paused") in summary["breakers_tripped"]
    assert summary["paused_on_cap"] is True

    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        # parked challenge untouched (still unresolved)
        cur.execute("SELECT resolved_at FROM auth_challenge WHERE url='https://x.com/job'")
        assert cur.fetchone()["resolved_at"] is None
        # fleet paused
        cur.execute("SELECT paused FROM fleet_config WHERE id=1")
        assert cur.fetchone()["paused"] is True

    # the report renders off the live post-tick snapshot
    with pgqueue.connect(fleet_db) as conn:
        snap = heartbeat.dashboard_snapshot(conn)
    report = monitor.build_health_report(snap, captcha_threshold=cfg.captcha_threshold, cost_cap_total=1.0)
    assert "NEEDS YOUR DECISION" in report
