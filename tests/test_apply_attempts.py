from __future__ import annotations

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

