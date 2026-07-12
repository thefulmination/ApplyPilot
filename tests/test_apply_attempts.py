from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import apply_attempts


def _prepared(conn, *, dedup_key="acme|operator") -> str:
    return apply_attempts.create_prepared(
        conn,
        queue_name="apply_queue",
        url="https://example.test/job/1",
        dedup_key=dedup_key,
        worker_id="m4-0",
        route="adapter_submit:greenhouse",
        route_version="greenhouse-v1",
        evidence={"plan_ready": True},
    )


def test_create_prepared_and_legal_transitions(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        attempt_id = _prepared(conn)
        started = apply_attempts.transition(
            conn,
            attempt_id,
            expected="prepared",
            state="submit_started",
            evidence={"action_count": 5},
        )
        verified = apply_attempts.transition(
            conn,
            attempt_id,
            expected="submit_started",
            state="verified",
            verification_method="confirmation_dom",
            verification_ref="application submitted",
            evidence={"success_url": "https://example.test/thanks"},
        )

    assert started["state"] == "submit_started"
    assert started["submit_started_at"] is not None
    assert started["evidence"] == {"plan_ready": True, "action_count": 5}
    assert verified["state"] == "verified"
    assert verified["finalized_at"] is not None
    assert verified["verification_method"] == "confirmation_dom"


def test_illegal_transition_is_rejected_before_sql(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        attempt_id = _prepared(conn)
        with pytest.raises(ValueError, match="prepared.*verified"):
            apply_attempts.transition(
                conn,
                attempt_id,
                expected="prepared",
                state="verified",
            )


def test_stale_expected_state_is_rejected(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        attempt_id = _prepared(conn)
        apply_attempts.transition(
            conn, attempt_id, expected="prepared", state="failed_pre_submit"
        )
        with pytest.raises(apply_attempts.AttemptTransitionError):
            apply_attempts.transition(
                conn, attempt_id, expected="prepared", state="submit_started"
            )


def test_second_unresolved_submit_for_dedup_key_is_rejected(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        first = _prepared(conn)
        second = _prepared(conn)
        apply_attempts.transition(
            conn, first, expected="prepared", state="submit_started"
        )
        with pytest.raises(apply_attempts.AttemptConflictError):
            apply_attempts.transition(
                conn, second, expected="prepared", state="submit_started"
            )


def test_pg_attempt_store_binds_queue_job_and_worker_identity(fleet_db):
    from applypilot.fleet.apply_worker_main import PgAttemptStore

    with pgqueue.connect(fleet_db) as conn:
        store = PgAttemptStore(
            conn,
            {
                "url": "bound-job",
                "dedup_key": "bound-dedup",
            },
            worker_id="m4-7",
        )
        attempt_id = store.create_prepared(
            route="adapter_submit:greenhouse",
            route_version="greenhouse-v1",
            evidence={"plan_ready": True},
        )
        store.transition(
            attempt_id, expected="prepared", state="failed_pre_submit"
        )
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM apply_attempts WHERE attempt_id=%s", (attempt_id,))
            row = cur.fetchone()

    assert row["queue_name"] == "apply_queue"
    assert row["url"] == "bound-job"
    assert row["dedup_key"] == "bound-dedup"
    assert row["worker_id"] == "m4-7"
    assert row["state"] == "failed_pre_submit"


def test_unresolved_submit_blocks_a_second_queue_lease(fleet_db):
    from applypilot.fleet import queue

    with pgqueue.connect(fleet_db) as conn:
        policy = "attempt-test-ats-policy"
        now = datetime.now(timezone.utc)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
                "VALUES (%s,'ats','active')",
                (policy,),
            )
            cur.execute(
                "UPDATE fleet_config SET ats_policy_version=%s, ats_apply_mode='steady', "
                "paused=FALSE, ats_paused=FALSE WHERE id=1",
                (policy,),
            )
        conn.commit()
        queue.push_apply_jobs(
            conn,
            [{
                "url": "second-source",
                "company": "Acme",
                "title": "Operator",
                "application_url": "https://boards.greenhouse.io/acme/jobs/2",
                "score": 9.0,
                "target_host": "boards.greenhouse.io",
                "dedup_key": "same-role",
                "decision_id": "decision-second-source",
                "policy_version": policy,
                "decision_action": "apply",
                "qualification_verdict": "qualified",
                "qualification_score": 9.0,
                "qualification_floor": 7.0,
                "preference_score": 8.0,
                "outcome_score": 8.0,
                "final_score": 9.0,
                "decision_confidence": 0.9,
                "decision_created_at": now,
                "decision_expires_at": now + timedelta(days=1),
                "input_hash": "hash-second-source",
            }],
            approved_batch="batchA",
        )
        attempt_id = apply_attempts.create_prepared(
            conn,
            queue_name="apply_queue",
            url="first-source",
            dedup_key="same-role",
            worker_id="m4-0",
            route="adapter_submit:greenhouse",
            route_version="greenhouse-v1",
        )
        apply_attempts.transition(
            conn, attempt_id, expected="prepared", state="submit_started"
        )

        leased = queue.lease_apply(conn, "m4-1", home_ip="1.2.3.4")

    assert leased is None
