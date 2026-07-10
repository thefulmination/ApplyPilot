"""Transactional repository for canonical policy and job decisions."""

from __future__ import annotations

import itertools
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterable, Iterator, Mapping


class ImmutableDecisionConflict(RuntimeError):
    """Raised when an immutable policy or decision is re-used with new values."""


class PolicyValidationError(RuntimeError):
    """Raised when a policy does not satisfy a lifecycle or release constraint."""


_POLICY_COLUMNS = (
    "policy_version",
    "lane",
    "status",
    "qualification_model",
    "preference_model",
    "outcome_model",
    "kg_version",
    "label_snapshot",
    "pairwise_snapshot",
    "outcome_snapshot",
    "config_json",
    "metrics_json",
    "created_at",
    "validated_at",
    "activated_at",
    "retired_at",
)

_DECISION_COLUMNS = (
    "decision_id",
    "job_url",
    "policy_version",
    "lane",
    "qualification_score",
    "preference_score",
    "outcome_score",
    "final_score",
    "qualification_verdict",
    "action",
    "confidence",
    "uncertainty_json",
    "blockers_json",
    "requirements_json",
    "evidence_node_ids_json",
    "title_signals_json",
    "explanation",
    "input_hash",
    "created_at",
    "expires_at",
)

_SAVEPOINT_IDS = itertools.count()
_RELEASE_GATES = (
    "hard_negative_false_positives",
    "queue_provenance_failures",
    "title_only_promotions",
)


@contextmanager
def _transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Own a write transaction, or isolate work within the caller's transaction."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            conn.rollback()
            raise
        else:
            conn.commit()
        return

    name = f"canonical_decisions_{next(_SAVEPOINT_IDS)}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except BaseException:
        conn.execute(f"ROLLBACK TO SAVEPOINT {name}")
        conn.execute(f"RELEASE SAVEPOINT {name}")
        raise
    else:
        conn.execute(f"RELEASE SAVEPOINT {name}")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp(value: Any | None) -> str:
    if value is None:
        return _utc_now()
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return str(value)


def _dict_row(cursor: sqlite3.Cursor, row: Any | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {description[0]: row[index] for index, description in enumerate(cursor.description)}


def _select_one(
    conn: sqlite3.Connection, sql: str, parameters: tuple[Any, ...]
) -> dict[str, Any] | None:
    cursor = conn.execute(sql, parameters)
    return _dict_row(cursor, cursor.fetchone())


def _canonical_json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _complete_row(
    row: Mapping[str, Any], columns: tuple[str, ...], *, defaults: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    unknown = set(row) - set(columns)
    if unknown:
        raise ValueError(f"unknown columns: {', '.join(sorted(unknown))}")
    values = dict.fromkeys(columns)
    if defaults:
        values.update(defaults)
    values.update(row)
    return values


def _assert_equal(
    existing: Mapping[str, Any], proposed: Mapping[str, Any], *, identity: str
) -> None:
    changed = [key for key in proposed if existing.get(key) != proposed[key]]
    if changed:
        raise ImmutableDecisionConflict(
            f"immutable row {identity!r} differs in: {', '.join(changed)}"
        )


def create_draft_policy(conn: sqlite3.Connection, row: Mapping[str, Any]) -> None:
    """Insert one lane-scoped draft policy, idempotently when fully identical."""
    if row.get("status", "draft") != "draft":
        raise PolicyValidationError("new policies must have draft status")

    proposed = _complete_row(row, _POLICY_COLUMNS, defaults={"status": "draft"})
    version = proposed["policy_version"]
    if not version or not proposed["lane"] or not proposed["created_at"]:
        raise PolicyValidationError("policy_version, lane, and created_at are required")

    with _transaction(conn):
        existing = _select_one(
            conn,
            f"SELECT {', '.join(_POLICY_COLUMNS)} FROM decision_policy_versions "
            "WHERE policy_version = ?",
            (version,),
        )
        if existing is not None:
            _assert_equal(existing, proposed, identity=str(version))
            return

        placeholders = ", ".join("?" for _ in _POLICY_COLUMNS)
        conn.execute(
            f"INSERT INTO decision_policy_versions ({', '.join(_POLICY_COLUMNS)}) "
            f"VALUES ({placeholders})",
            tuple(proposed[column] for column in _POLICY_COLUMNS),
        )


def insert_decisions(conn: sqlite3.Connection, rows: Iterable[Mapping[str, Any]]) -> int:
    """Insert immutable decisions and update their job projections atomically."""
    proposed_rows = [_complete_row(row, _DECISION_COLUMNS) for row in rows]
    inserted = 0

    with _transaction(conn):
        for proposed in proposed_rows:
            decision_id = proposed["decision_id"]
            existing = _select_one(
                conn,
                f"SELECT {', '.join(_DECISION_COLUMNS)} FROM job_decisions "
                "WHERE decision_id = ?",
                (decision_id,),
            )
            if existing is not None:
                _assert_equal(existing, proposed, identity=str(decision_id))
                continue

            identity_match = _select_one(
                conn,
                f"SELECT {', '.join(_DECISION_COLUMNS)} FROM job_decisions "
                "WHERE job_url = ? AND policy_version = ? AND input_hash = ?",
                (
                    proposed["job_url"],
                    proposed["policy_version"],
                    proposed["input_hash"],
                ),
            )
            if identity_match is not None:
                raise ImmutableDecisionConflict(
                    "canonical identity already belongs to decision "
                    f"{identity_match['decision_id']!r}"
                )

            placeholders = ", ".join("?" for _ in _DECISION_COLUMNS)
            conn.execute(
                f"INSERT INTO job_decisions ({', '.join(_DECISION_COLUMNS)}) "
                f"VALUES ({placeholders})",
                tuple(proposed[column] for column in _DECISION_COLUMNS),
            )
            inserted += 1

            conn.execute(
                """
                UPDATE jobs
                SET canonical_decision_id = ?,
                    canonical_policy_version = ?,
                    canonical_action = ?,
                    canonical_score = ?,
                    canonical_decided_at = ?
                WHERE url = ?
                """,
                (
                    proposed["decision_id"],
                    proposed["policy_version"],
                    proposed["action"],
                    proposed["final_score"],
                    proposed["created_at"],
                    proposed["job_url"],
                ),
            )

    return inserted


def get_decision(conn: sqlite3.Connection, decision_id: str) -> dict[str, Any] | None:
    """Return an immutable decision as a plain dictionary."""
    return _select_one(
        conn,
        f"SELECT {', '.join(_DECISION_COLUMNS)} FROM job_decisions WHERE decision_id = ?",
        (decision_id,),
    )


def record_replay_metrics(
    conn: sqlite3.Connection, policy_version: str, metrics: Mapping[str, Any]
) -> None:
    """Store replay metrics using deterministic JSON serialization."""
    encoded = _canonical_json(metrics)
    with _transaction(conn):
        cursor = conn.execute(
            "UPDATE decision_policy_versions SET metrics_json = ? WHERE policy_version = ?",
            (encoded, policy_version),
        )
        if cursor.rowcount != 1:
            raise PolicyValidationError(f"unknown policy: {policy_version}")


def _check_release_gates(metrics_json: str | None) -> None:
    if not metrics_json:
        raise PolicyValidationError("replay metrics are required")
    try:
        metrics = json.loads(metrics_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PolicyValidationError("replay metrics are invalid JSON") from exc
    if not isinstance(metrics, dict) or not metrics:
        raise PolicyValidationError("replay metrics must be a nonempty object")
    failed = [gate for gate in _RELEASE_GATES if metrics.get(gate) != 0]
    if failed:
        raise PolicyValidationError(f"release gates failed: {', '.join(failed)}")


def validate_policy(conn: sqlite3.Connection, policy_version: str) -> None:
    """Validate a draft policy after all fail-closed replay gates pass."""
    with _transaction(conn):
        policy = _select_one(
            conn,
            "SELECT status, metrics_json FROM decision_policy_versions WHERE policy_version = ?",
            (policy_version,),
        )
        if policy is None:
            raise PolicyValidationError(f"unknown policy: {policy_version}")
        if policy["status"] != "draft":
            raise PolicyValidationError("only draft policies can be validated")
        _check_release_gates(policy["metrics_json"])
        conn.execute(
            """
            UPDATE decision_policy_versions
            SET status = 'validated', validated_at = ?
            WHERE policy_version = ?
            """,
            (_utc_now(), policy_version),
        )


def _clear_policy_projection(conn: sqlite3.Connection, policy_version: str) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET canonical_decision_id = NULL,
            canonical_policy_version = NULL,
            canonical_action = NULL,
            canonical_score = NULL,
            canonical_decided_at = NULL
        WHERE canonical_policy_version = ?
        """,
        (policy_version,),
    )


def activate_policy(conn: sqlite3.Connection, policy_version: str, *, lane: str) -> None:
    """Make a validated/canary policy the sole active policy for its lane."""
    with _transaction(conn):
        policy = _select_one(
            conn,
            "SELECT lane, status, metrics_json FROM decision_policy_versions "
            "WHERE policy_version = ?",
            (policy_version,),
        )
        if policy is None:
            raise PolicyValidationError(f"unknown policy: {policy_version}")
        if policy["lane"] != lane:
            raise PolicyValidationError(
                f"policy lane {policy['lane']!r} does not match requested lane {lane!r}"
            )
        if policy["status"] not in {"validated", "canary"}:
            raise PolicyValidationError("policy status must be validated or canary")
        _check_release_gates(policy["metrics_json"])

        activated_at = _utc_now()
        replaced = conn.execute(
            """
            SELECT policy_version
            FROM decision_policy_versions
            WHERE lane = ? AND status = 'active' AND policy_version <> ?
            """,
            (lane, policy_version),
        ).fetchall()
        for row in replaced:
            replaced_version = row[0]
            conn.execute(
                """
                UPDATE decision_policy_versions
                SET status = 'retired', retired_at = ?
                WHERE policy_version = ?
                """,
                (activated_at, replaced_version),
            )
            _clear_policy_projection(conn, replaced_version)

        conn.execute(
            """
            UPDATE decision_policy_versions
            SET status = 'active', activated_at = ?, retired_at = NULL
            WHERE policy_version = ?
            """,
            (activated_at, policy_version),
        )


def retire_policy(conn: sqlite3.Connection, policy_version: str) -> None:
    """Retire a policy and invalidate its job cache without deleting decisions."""
    with _transaction(conn):
        cursor = conn.execute(
            """
            UPDATE decision_policy_versions
            SET status = 'retired', retired_at = COALESCE(retired_at, ?)
            WHERE policy_version = ?
            """,
            (_utc_now(), policy_version),
        )
        if cursor.rowcount != 1:
            raise PolicyValidationError(f"unknown policy: {policy_version}")
        _clear_policy_projection(conn, policy_version)


def eligible_decision(
    conn: sqlite3.Connection,
    job_url: str,
    *,
    lane: str,
    now: Any | None = None,
) -> dict[str, Any] | None:
    """Return the current fail-closed apply decision for a job and lane."""
    cursor = conn.execute(
        f"""
        SELECT {', '.join(f'd.{column}' for column in _DECISION_COLUMNS)}
        FROM jobs AS j
        JOIN job_decisions AS d
          ON d.decision_id = j.canonical_decision_id
         AND d.policy_version = j.canonical_policy_version
         AND d.action = j.canonical_action
         AND d.final_score IS j.canonical_score
         AND d.created_at = j.canonical_decided_at
        JOIN decision_policy_versions AS p
          ON p.policy_version = d.policy_version
         AND p.lane = d.lane
        WHERE j.url = ?
          AND d.job_url = j.url
          AND d.lane = ?
          AND p.lane = ?
          AND p.status IN ('canary', 'active')
          AND d.action = 'apply'
          AND d.qualification_verdict = 'qualified'
          AND (d.expires_at IS NULL OR julianday(d.expires_at) > julianday(?))
        """,
        (job_url, lane, lane, _timestamp(now)),
    )
    return _dict_row(cursor, cursor.fetchone())
