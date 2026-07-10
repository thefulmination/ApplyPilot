from __future__ import annotations

import json
import sqlite3

import pytest

from applypilot import canonical_decisions as repo
from applypilot import database


NOW = "2026-07-10T12:00:00Z"


@pytest.fixture
def conn(tmp_path):
    connection = database.init_db(tmp_path / "brain.db")
    connection.executemany(
        "INSERT INTO jobs(url, title) VALUES (?, ?)",
        (("u1", "One"), ("u2", "Two")),
    )
    connection.commit()
    yield connection
    connection.close()


def policy(version: str = "p1", lane: str = "ats") -> dict:
    return {
        "policy_version": version,
        "lane": lane,
        "qualification_model": "qualification-v1",
        "preference_model": "preference-v1",
        "config_json": '{"floor":0.8}',
        "created_at": NOW,
    }


def decision(
    decision_id: str = "d1",
    job_url: str = "u1",
    policy_version: str = "p1",
    lane: str = "ats",
    action: str = "apply",
    verdict: str = "qualified",
) -> dict:
    return {
        "decision_id": decision_id,
        "job_url": job_url,
        "policy_version": policy_version,
        "lane": lane,
        "qualification_score": 0.91,
        "preference_score": 0.72,
        "outcome_score": 0.63,
        "final_score": 0.81,
        "qualification_verdict": verdict,
        "action": action,
        "confidence": 0.88,
        "uncertainty_json": "[]",
        "blockers_json": "[]",
        "requirements_json": "[]",
        "evidence_node_ids_json": "[]",
        "title_signals_json": "[]",
        "explanation": "Evidence-backed match",
        "input_hash": f"hash-{decision_id}",
        "created_at": NOW,
        "expires_at": "2026-07-11T12:00:00Z",
    }


def valid_metrics(**overrides) -> dict:
    metrics = {
        "hard_negative_false_positives": 0,
        "queue_provenance_failures": 0,
        "title_only_promotions": 0,
    }
    metrics.update(overrides)
    return metrics


def prepare_policy(conn, version: str = "p1", lane: str = "ats") -> None:
    repo.create_draft_policy(conn, policy(version, lane))


def validate(conn, version: str = "p1") -> None:
    repo.record_replay_metrics(conn, version, valid_metrics())
    repo.validate_policy(conn, version)


def test_create_draft_policy_is_idempotent_but_changed_row_conflicts(conn) -> None:
    row = policy()
    repo.create_draft_policy(conn, row)
    repo.create_draft_policy(conn, dict(row))

    assert [
        tuple(row)
        for row in conn.execute("SELECT status FROM decision_policy_versions").fetchall()
    ] == [("draft",)]
    with pytest.raises(repo.ImmutableDecisionConflict):
        repo.create_draft_policy(conn, {**row, "config_json": '{"floor":0.9}'})


def test_create_draft_policy_rejects_non_draft_input(conn) -> None:
    with pytest.raises(repo.PolicyValidationError):
        repo.create_draft_policy(conn, {**policy(), "status": "active"})


def test_insert_decisions_is_immutable_idempotent_and_projects_exact_action(conn) -> None:
    prepare_policy(conn)
    apply_row = decision()
    review_row = decision("d2", "u2", action="review", verdict="uncertain")

    assert repo.insert_decisions(conn, [apply_row, review_row]) == 2
    assert repo.insert_decisions(conn, [dict(apply_row), dict(review_row)]) == 0
    assert repo.get_decision(conn, "d1") == apply_row
    assert repo.get_decision(conn, "missing") is None
    assert conn.execute(
        "SELECT canonical_action FROM jobs WHERE url = 'u2'"
    ).fetchone()[0] == "review"

    with pytest.raises(repo.ImmutableDecisionConflict):
        repo.insert_decisions(conn, [{**apply_row, "final_score": 0.2}])


def test_insert_decisions_rolls_back_batch_and_projection_on_failure(conn) -> None:
    prepare_policy(conn)

    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_decisions(conn, [decision(), decision("d2", "missing")])

    assert conn.execute("SELECT COUNT(*) FROM job_decisions").fetchone()[0] == 0
    assert conn.execute(
        "SELECT canonical_decision_id FROM jobs WHERE url = 'u1'"
    ).fetchone()[0] is None


def test_repository_savepoint_preserves_callers_outer_transaction(conn) -> None:
    prepare_policy(conn)
    conn.execute("UPDATE jobs SET title = 'outer change' WHERE url = 'u1'")
    assert conn.in_transaction

    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_decisions(conn, [decision(), decision("d2", "missing")])

    assert conn.in_transaction
    assert conn.execute("SELECT title FROM jobs WHERE url = 'u1'").fetchone()[0] == "outer change"
    conn.rollback()
    assert conn.execute("SELECT title FROM jobs WHERE url = 'u1'").fetchone()[0] == "One"


def test_failed_batch_does_not_overwrite_an_existing_projection(conn) -> None:
    prepare_policy(conn)
    repo.insert_decisions(conn, [decision()])
    before = conn.execute(
        "SELECT canonical_decision_id, canonical_score FROM jobs WHERE url = 'u1'"
    ).fetchone()

    with pytest.raises(sqlite3.IntegrityError):
        repo.insert_decisions(
            conn,
            [decision("d2", "u1"), decision("d3", "missing")],
        )

    assert conn.execute(
        "SELECT canonical_decision_id, canonical_score FROM jobs WHERE url = 'u1'"
    ).fetchone() == before


def test_record_replay_metrics_uses_canonical_sorted_json(conn) -> None:
    prepare_policy(conn)
    repo.record_replay_metrics(conn, "p1", {"z": 1, "a": {"y": 2, "x": 1}})

    stored = conn.execute(
        "SELECT metrics_json FROM decision_policy_versions WHERE policy_version = 'p1'"
    ).fetchone()[0]
    assert stored == '{"a":{"x":1,"y":2},"z":1}'
    assert json.loads(stored)["z"] == 1


@pytest.mark.parametrize(
    "metrics",
    [
        None,
        {},
        valid_metrics(hard_negative_false_positives=1),
        valid_metrics(queue_provenance_failures=1),
        valid_metrics(title_only_promotions=1),
    ],
)
def test_validate_policy_enforces_release_gates(conn, metrics) -> None:
    prepare_policy(conn)
    if metrics is not None:
        repo.record_replay_metrics(conn, "p1", metrics)

    with pytest.raises(repo.PolicyValidationError):
        repo.validate_policy(conn, "p1")
    state = conn.execute(
        "SELECT status, validated_at FROM decision_policy_versions WHERE policy_version = 'p1'"
    ).fetchone()
    assert tuple(state) == ("draft", None)


def test_validate_policy_requires_draft_and_sets_validation_state(conn) -> None:
    prepare_policy(conn)
    validate(conn)
    status, validated_at = conn.execute(
        "SELECT status, validated_at FROM decision_policy_versions WHERE policy_version = 'p1'"
    ).fetchone()
    assert status == "validated"
    assert validated_at is not None

    with pytest.raises(repo.PolicyValidationError):
        repo.validate_policy(conn, "p1")


def test_activate_policy_rejects_lane_mismatch_and_invalid_status(conn) -> None:
    prepare_policy(conn)
    with pytest.raises(repo.PolicyValidationError):
        repo.activate_policy(conn, "p1", lane="ats")

    validate(conn)
    with pytest.raises(repo.PolicyValidationError):
        repo.activate_policy(conn, "p1", lane="linkedin")
    assert conn.execute(
        "SELECT status FROM decision_policy_versions WHERE policy_version = 'p1'"
    ).fetchone()[0] == "validated"


def test_activate_policy_retires_prior_active_only_in_same_lane(conn) -> None:
    prepare_policy(conn, "old", "ats")
    validate(conn, "old")
    repo.activate_policy(conn, "old", lane="ats")
    repo.insert_decisions(conn, [decision(policy_version="old")])

    for version, lane in (("new", "ats"), ("li", "linkedin")):
        prepare_policy(conn, version, lane)
        validate(conn, version)
        repo.activate_policy(conn, version, lane=lane)

    rows = [
        tuple(row)
        for row in conn.execute(
            "SELECT policy_version, status FROM decision_policy_versions ORDER BY policy_version"
        ).fetchall()
    ]
    assert rows == [("li", "active"), ("new", "active"), ("old", "retired")]
    assert conn.execute(
        "SELECT retired_at FROM decision_policy_versions WHERE policy_version = 'old'"
    ).fetchone()[0] is not None
    assert conn.execute(
        "SELECT canonical_decision_id FROM jobs WHERE url = 'u1'"
    ).fetchone()[0] is None


def test_activation_rechecks_release_metrics(conn) -> None:
    prepare_policy(conn)
    validate(conn)
    repo.record_replay_metrics(conn, "p1", valid_metrics(title_only_promotions=1))

    with pytest.raises(repo.PolicyValidationError):
        repo.activate_policy(conn, "p1", lane="ats")


def test_retire_policy_clears_projection_but_keeps_decisions(conn) -> None:
    prepare_policy(conn)
    repo.insert_decisions(conn, [decision()])
    repo.retire_policy(conn, "p1")

    assert conn.execute("SELECT COUNT(*) FROM job_decisions").fetchone()[0] == 1
    assert conn.execute(
        "SELECT status, retired_at FROM decision_policy_versions WHERE policy_version = 'p1'"
    ).fetchone()[0] == "retired"
    projection = conn.execute(
        "SELECT canonical_decision_id, canonical_policy_version, canonical_action, "
        "canonical_score, canonical_decided_at FROM jobs WHERE url = 'u1'"
    ).fetchone()
    assert tuple(projection) == (None, None, None, None, None)


def test_eligible_decision_requires_current_apply_projection_and_live_policy(conn) -> None:
    prepare_policy(conn)
    validate(conn)
    repo.activate_policy(conn, "p1", lane="ats")
    repo.insert_decisions(conn, [decision()])

    assert repo.eligible_decision(conn, "u1", lane="ats", now=NOW)["decision_id"] == "d1"
    assert repo.eligible_decision(conn, "u1", lane="linkedin", now=NOW) is None

    conn.execute("UPDATE jobs SET canonical_decision_id = NULL WHERE url = 'u1'")
    assert repo.eligible_decision(conn, "u1", lane="ats", now=NOW) is None


def test_eligible_decision_accepts_canary_policy(conn) -> None:
    prepare_policy(conn)
    validate(conn)
    conn.execute(
        "UPDATE decision_policy_versions SET status = 'canary' WHERE policy_version = 'p1'"
    )
    repo.insert_decisions(conn, [decision()])

    assert repo.eligible_decision(conn, "u1", lane="ats", now=NOW)["decision_id"] == "d1"


@pytest.mark.parametrize(
    ("action", "verdict"),
    (("review", "qualified"), ("reject", "unqualified"), ("apply", "uncertain")),
)
def test_eligible_decision_never_returns_nonqualified_apply(conn, action, verdict) -> None:
    prepare_policy(conn)
    validate(conn)
    repo.activate_policy(conn, "p1", lane="ats")
    repo.insert_decisions(conn, [decision(action=action, verdict=verdict)])

    assert repo.eligible_decision(conn, "u1", lane="ats", now=NOW) is None


def test_eligible_decision_rejects_expired_decision(conn) -> None:
    prepare_policy(conn)
    validate(conn)
    repo.activate_policy(conn, "p1", lane="ats")
    repo.insert_decisions(
        conn,
        [decision() | {"expires_at": "2026-07-10T11:59:59Z"}],
    )

    assert repo.eligible_decision(conn, "u1", lane="ats", now=NOW) is None
