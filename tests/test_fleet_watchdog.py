# tests/test_fleet_watchdog.py
from applypilot.apply import pgqueue
from applypilot.fleet import watchdog, queue


def _seed_governor_scope_count(conn, scope_key, *, count_24h):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO rate_governor (scope_key, count_24h, min_gap_seconds) VALUES (%s,%s,5)",
                    (scope_key, count_24h))


def _seed_governor_scope(conn, scope_key, *, success=0, captcha=0, block=0, state="ok",
                         challenge_rate=0.0, breaker_until=None):
    # breaker_until may be passed as a SQL expression string (e.g. "now() - interval '1 minute'")
    # which cannot bind via %s; treat any string as None here -- the caller's UPDATE overwrites it.
    bind_until = None if isinstance(breaker_until, str) else breaker_until
    # challenge_rate is a GENERATED ALWAYS AS (STORED) REAL column; we cannot set it directly.
    # The column value is computed from (captcha_24h + block_24h) / total.
    # PostgreSQL REAL text output for 6/10 is "0.6", which Python reads as float64 0.6.
    # Due to float64 precision, 0.6 < 0.4 * 1.5 (= 0.6000000000000001), so the boundary
    # case would mis-classify as "throttled" instead of "paused".  We bump captcha by 1
    # when a non-zero challenge_rate is hinted, pushing the rate robustly above the boundary.
    # (e.g. captcha=6+1=7, total=11 -> rate=7/11≈0.636, unambiguously "paused".)
    seed_captcha = captcha + 1 if challenge_rate > 0 and captcha > 0 else captcha
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rate_governor (scope_key, success_24h, captcha_24h, block_24h, "
            "breaker_state, breaker_until, min_gap_seconds) "
            "VALUES (%s,%s,%s,%s,%s,%s, 5)",
            (scope_key, success, seed_captcha, block, state, bind_until))
    conn.commit()


def test_watchdog_trips_breaker_on_high_challenge_rate(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        # 10 samples, challenge_rate 0.6 >= 0.4*1.5 -> paused
        _seed_governor_scope(conn, "host:acme.com", success=4, captcha=6, block=0, challenge_rate=0.6)
        summary = watchdog.watchdog_tick(conn, cfg)
    assert ("host:acme.com", "paused") in summary["breakers_tripped"]


def test_watchdog_recovers_expired_breaker(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        _seed_governor_scope(conn, "host:old.com", state="paused", challenge_rate=0.0,
                             breaker_until="now() - interval '1 minute'")
        # breaker_until as a literal won't bind via %s; set it directly instead:
        with conn.cursor() as cur:
            cur.execute("UPDATE rate_governor SET breaker_until = now() - interval '1 minute' "
                        "WHERE scope_key='host:old.com'")
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)
    assert "host:old.com" in summary["breakers_recovered"]


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


def _seed_stuck_worker(conn, worker_id="wStuck", *, current_job=None, applying=False):
    state = "applying" if applying else "idle"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO worker_heartbeat (worker_id, role, state, current_job, job_started_at, last_beat) "
            "VALUES (%s,'apply',%s,%s, now() - interval '20 minutes', now() - interval '10 minutes')",
            (worker_id, state, current_job))
    conn.commit()


def test_watchdog_restarts_stuck_worker(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        _seed_stuck_worker(conn, "wStuck")  # last_beat 10m ago > 90s timeout
        summary = watchdog.watchdog_tick(conn, cfg)
        entries = [e for e in summary["stuck_handled"] if e["worker_id"] == "wStuck"]
        assert entries and "restart" in entries[0]["action"]
        # a 'restart' command was actually enqueued
        with conn.cursor() as cur:
            cur.execute("SELECT command FROM remote_commands WHERE worker_id='wStuck'")
            assert cur.fetchone()["command"] == "restart"


def test_watchdog_dedupes_open_restart_for_stuck_worker(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        _seed_stuck_worker(conn, "wStuck")
        watchdog.watchdog_tick(conn, cfg)
        summary = watchdog.watchdog_tick(conn, cfg)
        entries = [e for e in summary["stuck_handled"] if e["worker_id"] == "wStuck"]
        assert entries and entries[0]["action"] == ["restart_pending"]
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM remote_commands "
                "WHERE worker_id='wStuck' AND command='restart' AND acked_at IS NULL"
            )
            assert cur.fetchone()["n"] == 1


def test_watchdog_does_not_restart_stale_idle_discovery_worker(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, role, state, last_beat) "
                "VALUES ('m2-disc','discovery','idle', now() - interval '10 minutes')"
            )
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)

        assert all(e["worker_id"] != "m2-disc" for e in summary["stuck_handled"])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM remote_commands "
                "WHERE worker_id='m2-disc' AND command='restart' AND acked_at IS NULL"
            )
            assert cur.fetchone()["n"] == 0


def test_watchdog_does_not_restart_unmanaged_linkedin_worker(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, role, state, last_beat) "
                "VALUES ('home-linkedin-0','linkedin','idle', now() - interval '10 minutes')"
            )
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)

        assert all(e["worker_id"] != "home-linkedin-0" for e in summary["stuck_handled"])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM remote_commands "
                "WHERE worker_id='home-linkedin-0' AND command='restart' AND acked_at IS NULL"
            )
            assert cur.fetchone()["n"] == 0


def test_watchdog_does_not_restart_stale_fleet_doctor_heartbeat(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, role, state, last_beat) "
                "VALUES ('fleet_doctor','doctor','busy', now() - interval '10 minutes')"
            )
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)

        assert all(e["worker_id"] != "fleet_doctor" for e in summary["stuck_handled"])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM remote_commands "
                "WHERE worker_id='fleet_doctor' AND command='restart' AND acked_at IS NULL"
            )
            assert cur.fetchone()["n"] == 0


def test_watchdog_does_not_restart_unmanaged_machine_owner(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TEMP TABLE fleet_desired_state ("
                "machine_owner TEXT PRIMARY KEY, desired_workers INTEGER NOT NULL"
                ") ON COMMIT PRESERVE ROWS"
            )
            cur.execute(
                "INSERT INTO fleet_desired_state (machine_owner, desired_workers) "
                "VALUES ('m2', 4), ('m4', 4)"
            )
            cur.execute(
                "INSERT INTO worker_heartbeat "
                "(worker_id, machine_owner, role, state, last_beat) "
                "VALUES ('mac-0','mac-Mac','apply','idle', now() - interval '10 minutes')"
            )
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)

        assert all(e["worker_id"] != "mac-0" for e in summary["stuck_handled"])
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS n FROM remote_commands "
                "WHERE worker_id='mac-0' AND command='restart' AND acked_at IS NULL"
            )
            assert cur.fetchone()["n"] == 0


def test_watchdog_never_restarts_itself(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        # give the watchdog a STALE heartbeat, then run a tick
        with conn.cursor() as cur:
            cur.execute("INSERT INTO worker_heartbeat (worker_id, role, state, last_beat) "
                        "VALUES ('watchdog','watchdog','idle', now() - interval '10 minutes')")
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)
    assert all(e["worker_id"] != "watchdog" for e in summary["stuck_handled"])
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) AS n FROM remote_commands WHERE worker_id='watchdog'")
        assert cur.fetchone()["n"] == 0


def test_watchdog_pauses_on_total_cap_breach(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET cost_cap_total_usd=1.0, paused=FALSE WHERE id=1")
            cur.execute("INSERT INTO llm_usage (cost_usd, ts) VALUES (2.50, now())")
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)
    assert summary["paused_on_cap"] is True
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT paused FROM fleet_config WHERE id=1")
        assert cur.fetchone()["paused"] is True


def test_watchdog_does_not_pause_under_cap(fleet_db):
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET cost_cap_total_usd=100.0, paused=FALSE WHERE id=1")
            cur.execute("INSERT INTO llm_usage (cost_usd, ts) VALUES (2.50, now())")
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)
    assert summary["paused_on_cap"] is False
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT paused FROM fleet_config WHERE id=1")
        assert cur.fetchone()["paused"] is False


def test_watchdog_rolls_window_once_per_night(fleet_db):
    cfg = watchdog.WatchdogConfig(nightly_roll_hour=4)
    with pgqueue.connect(fleet_db) as conn:
        # force "it is 4am and we've never rolled"
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET last_window_roll_at = NULL WHERE id=1")
            _seed_governor_scope_count(conn, "host:x.com", count_24h=99)
        conn.commit()
        rolled1 = watchdog._maybe_roll_window(conn, cfg, now_hour=4)
        rolled2 = watchdog._maybe_roll_window(conn, cfg, now_hour=4)  # same night -> no-op
    assert rolled1 is True and rolled2 is False
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT count_24h FROM rate_governor WHERE scope_key='host:x.com'")
        assert cur.fetchone()["count_24h"] == 0  # roll zeroed the counters


def test_watchdog_does_not_roll_off_hour(fleet_db):
    cfg = watchdog.WatchdogConfig(nightly_roll_hour=4)
    with pgqueue.connect(fleet_db) as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET last_window_roll_at = NULL WHERE id=1")
        conn.commit()
        assert watchdog._maybe_roll_window(conn, cfg, now_hour=13) is False


def test_run_watchdog_runs_until_stop(fleet_db):
    cfg = watchdog.WatchdogConfig(cadence_seconds=0)  # no real sleep in the test
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 3  # let 3 ticks run, then stop

    ticks = watchdog.run_watchdog(lambda: pgqueue.connect(fleet_db), cfg, stop=stop)
    assert ticks == 3


def test_run_watchdog_survives_a_bad_tick(fleet_db, monkeypatch):
    cfg = watchdog.WatchdogConfig(cadence_seconds=0)
    seq = {"n": 0}

    def flaky_tick(conn, c):
        seq["n"] += 1
        if seq["n"] == 1:
            raise RuntimeError("boom")
        return {}

    monkeypatch.setattr(watchdog, "watchdog_tick", flaky_tick)
    ticks = watchdog.run_watchdog(lambda: pgqueue.connect(fleet_db), cfg, max_ticks=2)
    assert ticks == 2  # the RuntimeError on tick 1 did not kill the loop


def test_watchdog_reclaims_linkedin(fleet_db):
    from applypilot.fleet import watchdog
    cfg = watchdog.WatchdogConfig()
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO linkedin_queue (url, application_url, score, status, lane, lease_owner, lease_expires_at, attempts) "
                    "VALUES ('lw','https://linkedin.com/jobs/z','9','leased','ats','wDead', now()-interval '5 min', 1)")
        conn.commit()
        summary = watchdog.watchdog_tick(conn, cfg)
    assert summary["reclaimed_linkedin"] == 1


def test_roll_window_preserves_halt(fleet_db):
    from applypilot.fleet import governor
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO rate_governor (scope_key, halted_until) VALUES ('account:linkedin', now()+interval '6 hours')")
        conn.commit()
        governor.roll_window(conn)
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        assert cur.fetchone()["halted_until"] is not None  # nightly roll did NOT clear the halt
