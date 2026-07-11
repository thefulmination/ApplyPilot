from __future__ import annotations

from datetime import datetime, timedelta, timezone

from applypilot.apply import pgqueue
from applypilot.fleet import queue
from applypilot.fleet.worker import WorkerLoop


def _seed_required(conn, url: str, *, score: float = 9.0):
    policy = "test-ats-policy"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
            "VALUES (%s,'ats','active') ON CONFLICT (policy_version) DO UPDATE SET status='active'",
            (policy,),
        )
        cur.execute("UPDATE fleet_config SET ats_policy_version=%s WHERE id=1", (policy,))
    conn.commit()
    now = datetime.now(timezone.utc)
    queue.push_apply_jobs(
        conn,
        [{
            "url": url,
            "company": "Acme",
            "title": "Operator",
            "application_url": url,
            "score": score,
            "target_host": "acme.wd5.myworkdayjobs.com",
            "decision_id": f"decision-{url}",
            "policy_version": policy,
            "decision_action": "apply",
            "qualification_verdict": "qualified",
            "qualification_score": 9.0,
            "qualification_floor": 7.0,
            "preference_score": 8.0,
            "outcome_score": 8.0,
            "final_score": score,
            "decision_confidence": 0.9,
            "decision_created_at": now,
            "decision_expires_at": now + timedelta(days=1),
            "input_hash": f"hash-{url}",
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
