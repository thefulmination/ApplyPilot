from __future__ import annotations

from applypilot.apply import pgqueue
from applypilot.fleet import queue
from applypilot.fleet.worker import WorkerLoop


def test_liveness_retry_policy_is_reason_specific():
    assert queue.liveness_retry_policy("server_503") == (
        "transient", queue.LIVENESS_TRANSIENT_RETRY_SECONDS
    )
    assert queue.liveness_retry_policy("redirect_login") == (
        "structural", queue.LIVENESS_STRUCTURAL_RETRY_SECONDS
    )
    assert queue.liveness_retry_policy("access_gate:captcha") == (
        "uncertain", queue.LIVENESS_UNCERTAIN_RETRY_SECONDS
    )
    assert queue.liveness_retry_policy("server_503", 3) == (
        "uncertain", queue.LIVENESS_UNCERTAIN_RETRY_SECONDS
    )
    assert queue.liveness_retry_policy("server_503", 6) == (
        "structural", queue.LIVENESS_STRUCTURAL_RETRY_SECONDS
    )


def test_liveness_env_seconds_rejects_invalid_and_negative_values(monkeypatch):
    monkeypatch.setenv("TEST_LIVENESS_SECONDS", "invalid")
    assert queue._nonnegative_env_seconds("TEST_LIVENESS_SECONDS", 123) == 123
    monkeypatch.setenv("TEST_LIVENESS_SECONDS", "-1")
    assert queue._nonnegative_env_seconds("TEST_LIVENESS_SECONDS", 123) == 123
    monkeypatch.setenv("TEST_LIVENESS_SECONDS", "0")
    assert queue._nonnegative_env_seconds("TEST_LIVENESS_SECONDS", 123) == 0
    monkeypatch.setenv("TEST_LIVENESS_SECONDS", "45")
    assert queue._nonnegative_env_seconds("TEST_LIVENESS_SECONDS", 123) == 45


def _seed_required(conn, url: str, *, score: float = 9.0):
    queue.push_apply_jobs(
        conn,
        [{
            "url": url,
            "company": "Acme",
            "title": "Operator",
            "application_url": url,
            "score": score,
            "target_host": "acme.wd5.myworkdayjobs.com",
        }],
        approved_batch="batch-live",
        require_liveness=True,
    )


def test_required_stale_row_cannot_receive_paid_lease(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR1"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)
        assert queue.lease_apply(conn, "paid-worker", home_ip="1.2.3.4") is None
        with conn.cursor() as cur:
            cur.execute("SELECT attempts, status FROM apply_queue WHERE url=%s", (url,))
            row = cur.fetchone()
            assert row["attempts"] == 0
            assert row["status"] == "queued"


def test_preflight_claim_is_exclusive_and_live_verdict_unlocks_lease(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR2"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)
        check = queue.claim_liveness_check(conn, "preflight-a")
        assert check["url"] == url
        assert queue.claim_liveness_check(conn, "preflight-b") is None
        assert queue.write_liveness_result(
            conn,
            "preflight-a",
            url,
            status="live",
            reason="workday_cxs_200",
        )
        job = queue.lease_apply(conn, "paid-worker", home_ip="1.2.3.4")
        assert job["url"] == url


def test_host_probe_is_single_flight_and_transient_failure_starts_cooldown(fleet_db):
    first_url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-host-a"
    second_url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-host-b"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, first_url, score=10.0)
        _seed_required(conn, second_url, score=9.0)
        assert queue.claim_liveness_check(conn, "preflight-a")["url"] == first_url
        assert queue.claim_liveness_check(conn, "preflight-b") is None
        assert queue.write_liveness_result(
            conn, "preflight-a", first_url, status="uncertain", reason="server_503"
        )
        assert queue.claim_liveness_check(conn, "preflight-b") is None
        assert queue.claim_liveness_check(
            conn, "preflight-b", host_cooldown_seconds=0
        )["url"] == second_url


def test_application_url_change_invalidates_claim_and_all_liveness_state(fleet_db):
    url = "https://aggregator.test/jobs/123"
    old_target = "https://old.example.test/jobs/123"
    new_target = "https://new.example.test/jobs/123"
    with pgqueue.connect(fleet_db) as conn:
        queue.push_apply_jobs(
            conn,
            [{
                "url": url,
                "company": "Acme",
                "title": "Operator",
                "application_url": old_target,
                "score": 9.0,
                "target_host": "old.example.test",
            }],
            approved_batch="batch-live",
            require_liveness=True,
        )
        assert queue.claim_liveness_check(conn, "stale-worker")["application_url"] == old_target
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET liveness_status='uncertain', liveness_reason='server_503', "
                "liveness_checked_at=now(), liveness_check_count=4, "
                "liveness_consecutive_uncertain=4 WHERE url=%s",
                (url,),
            )
        conn.commit()
        queue.push_apply_jobs(
            conn,
            [{
                "url": url,
                "company": "Acme",
                "title": "Operator",
                "application_url": new_target,
                "score": 9.0,
                "target_host": "new.example.test",
            }],
            approved_batch="batch-live",
            require_liveness=True,
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT liveness_status, liveness_reason, liveness_checked_at, "
                "liveness_check_owner, liveness_check_expires_at, liveness_check_count, "
                "liveness_consecutive_uncertain FROM apply_queue WHERE url=%s",
                (url,),
            )
            row = cur.fetchone()
        assert row["liveness_status"] is None
        assert row["liveness_reason"] is None
        assert row["liveness_checked_at"] is None
        assert row["liveness_check_owner"] is None
        assert row["liveness_check_expires_at"] is None
        assert row["liveness_check_count"] == 0
        assert row["liveness_consecutive_uncertain"] == 0
        assert not queue.write_liveness_result(
            conn, "stale-worker", url, status="dead", reason="http_404"
        )


def test_expired_liveness_claim_is_recovered_and_stale_owner_cannot_write(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-recover"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)
        assert queue.claim_liveness_check(conn, "worker-a")["url"] == url
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET liveness_check_expires_at=now() - interval '1 second' "
                "WHERE url=%s",
                (url,),
            )
        conn.commit()
        assert queue.claim_liveness_check(conn, "worker-b")["url"] == url
        assert not queue.write_liveness_result(
            conn, "worker-a", url, status="dead", reason="http_404"
        )
        assert queue.write_liveness_result(
            conn, "worker-b", url, status="live", reason="workday_cxs_200"
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT liveness_status, liveness_reason, liveness_check_owner, "
                "liveness_check_count FROM apply_queue WHERE url=%s",
                (url,),
            )
            row = cur.fetchone()
        assert row["liveness_status"] == "live"
        assert row["liveness_reason"] == "workday_cxs_200"
        assert row["liveness_check_owner"] is None
        assert row["liveness_check_count"] == 1


def test_job_specific_uncertainty_does_not_cool_down_entire_host(fleet_db):
    first_url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-login-a"
    second_url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-login-b"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, first_url, score=10.0)
        _seed_required(conn, second_url, score=9.0)
        assert queue.claim_liveness_check(conn, "preflight-a")["url"] == first_url
        assert queue.write_liveness_result(
            conn, "preflight-a", first_url, status="uncertain", reason="redirect_login"
        )
        assert queue.claim_liveness_check(conn, "preflight-b")["url"] == second_url


def test_liveness_result_tracks_and_resets_uncertain_streak(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-streak"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)
        assert queue.claim_liveness_check(conn, "preflight-a")["url"] == url
        assert queue.write_liveness_result(
            conn, "preflight-a", url, status="uncertain", reason="server_503"
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT liveness_check_count, liveness_consecutive_uncertain "
                "FROM apply_queue WHERE url=%s",
                (url,),
            )
            assert tuple(cur.fetchone().values()) == (1, 1)
            cur.execute(
                "UPDATE apply_queue SET liveness_checked_at=now() - interval '2 hours' WHERE url=%s",
                (url,),
            )
        conn.commit()
        assert queue.claim_liveness_check(conn, "preflight-a")["url"] == url
        assert queue.write_liveness_result(
            conn, "preflight-a", url, status="live", reason="workday_cxs_200"
        )
        with conn.cursor() as cur:
            cur.execute(
                "SELECT liveness_check_count, liveness_consecutive_uncertain "
                "FROM apply_queue WHERE url=%s",
                (url,),
            )
            assert tuple(cur.fetchone().values()) == (2, 0)


def test_uncertain_liveness_uses_longer_retry_window(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-uncertain"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET liveness_status='uncertain', liveness_reason='access_gate:captcha', "
                "liveness_checked_at=now() - interval '16 minutes' WHERE url=%s",
                (url,),
            )
        conn.commit()
        assert queue.claim_liveness_check(conn, "preflight-a") is None
        check = queue.claim_liveness_check(
            conn,
            "preflight-a",
            uncertain_retry_seconds=15 * 60,
        )
        assert check["url"] == url


def test_transient_uncertain_retries_after_one_hour(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-server"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET liveness_status='uncertain', liveness_reason='server_503', "
                "liveness_checked_at=now() - interval '61 minutes' WHERE url=%s",
                (url,),
            )
        conn.commit()
        assert queue.claim_liveness_check(conn, "preflight-a")["url"] == url


def test_repeated_transient_uncertainty_escalates_scheduler_backoff(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-server-repeat"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET liveness_status='uncertain', liveness_reason='server_503', "
                "liveness_consecutive_uncertain=3, "
                "liveness_checked_at=now() - interval '2 hours' WHERE url=%s",
                (url,),
            )
        conn.commit()
        assert queue.claim_liveness_check(conn, "preflight-a") is None
        assert queue.claim_liveness_check(
            conn, "preflight-a", uncertain_retry_seconds=60 * 60
        )["url"] == url


def test_long_uncertain_streak_escalates_to_daily_scheduler_backoff(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-server-stuck"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET liveness_status='uncertain', liveness_reason='server_503', "
                "liveness_consecutive_uncertain=6, "
                "liveness_checked_at=now() - interval '7 hours' WHERE url=%s",
                (url,),
            )
        conn.commit()
        assert queue.claim_liveness_check(conn, "preflight-a") is None
        assert queue.claim_liveness_check(
            conn, "preflight-a", structural_retry_seconds=6 * 60 * 60
        )["url"] == url


def test_structural_uncertain_defaults_to_daily_retry(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-login"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET liveness_status='uncertain', liveness_reason='redirect_login', "
                "liveness_checked_at=now() - interval '7 hours' WHERE url=%s",
                (url,),
            )
        conn.commit()
        assert queue.claim_liveness_check(conn, "preflight-a") is None
        check = queue.claim_liveness_check(
            conn,
            "preflight-a",
            structural_retry_seconds=6 * 60 * 60,
        )
        assert check["url"] == url


def test_liveness_claim_prioritizes_stale_live_then_unchecked_then_uncertain(fleet_db):
    live_url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-live"
    unchecked_url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-unchecked"
    uncertain_url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR-uncertain-old"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, unchecked_url, score=10.0)
        _seed_required(conn, uncertain_url, score=9.9)
        _seed_required(conn, live_url, score=8.0)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET liveness_status='live', liveness_reason='ok_200', "
                "liveness_checked_at=now() - interval '20 minutes' WHERE url=%s",
                (live_url,),
            )
            cur.execute(
                "UPDATE apply_queue SET liveness_status='uncertain', liveness_reason='access_gate:captcha', "
                "liveness_checked_at=now() - interval '7 hours' WHERE url=%s",
                (uncertain_url,),
            )
        conn.commit()

        first = queue.claim_liveness_check(conn, "preflight-a")
        assert first["url"] == live_url
        assert queue.write_liveness_result(
            conn, "preflight-a", live_url, status="live", reason="ok_200"
        )
        second = queue.claim_liveness_check(conn, "preflight-a")
        assert second["url"] == unchecked_url
        assert queue.write_liveness_result(
            conn, "preflight-a", unchecked_url, status="live", reason="ok_200"
        )
        third = queue.claim_liveness_check(conn, "preflight-a")
        assert third["url"] == uncertain_url


def test_dead_verdict_closes_row_without_application_attempt(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR3"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)

    applied = []
    loop = WorkerLoop(
        lambda: pgqueue.connect(fleet_db),
        "w-live",
        home_ip="1.2.3.4",
        role="apply",
        liveness_fn=lambda *_args, **_kwargs: ("dead", "workday_cxs_404"),
        apply_fn=lambda job: applied.append(job),
        sw_version="0.3.0",
    )
    result = loop.run_once()

    assert result["action"] == "liveness_checked"
    assert result["liveness_status"] == "dead"
    assert applied == []
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT status, apply_status, apply_error, attempts FROM apply_queue WHERE url=%s",
            (url,),
        )
        row = cur.fetchone()
        assert row["status"] == "failed"
        assert row["apply_status"] == "expired"
        assert row["apply_error"] == "liveness:workday_cxs_404"
        assert row["attempts"] == 0


def test_worker_can_probe_live_and_apply_in_same_tick(fleet_db):
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Operator/JR4"
    with pgqueue.connect(fleet_db) as conn:
        _seed_required(conn, url)

    calls = []
    loop = WorkerLoop(
        lambda: pgqueue.connect(fleet_db),
        "w-live",
        home_ip="1.2.3.4",
        role="apply",
        liveness_fn=lambda *_args, **_kwargs: ("live", "workday_cxs_200"),
        apply_fn=lambda job: calls.append(job) or {
            "run_status": "failed:validation",
            "est_cost_usd": 0.1,
        },
        sw_version="0.3.0",
    )
    result = loop.run_once()

    assert result["action"] == "failed"
    assert len(calls) == 1
    with pgqueue.connect(fleet_db) as conn, conn.cursor() as cur:
        cur.execute("SELECT liveness_status, attempts FROM apply_queue WHERE url=%s", (url,))
        row = cur.fetchone()
        assert row["liveness_status"] == "live"
        assert row["attempts"] == 1
