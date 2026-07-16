"""Durable exactly-once checkpoints for adapter-owned application submits."""
from __future__ import annotations

import uuid

from psycopg import errors
from psycopg.types.json import Jsonb


class AttemptTransitionError(RuntimeError):
    """The attempt no longer has the caller's expected state."""


class AttemptConflictError(RuntimeError):
    """Another unresolved submit already owns this dedup key."""


_TRANSITIONS = {
    "prepared": frozenset({"submit_started", "failed_pre_submit"}),
    "submit_started": frozenset(
        {"submitted_unverified", "verified", "contradicted", "quarantined"}
    ),
    "submitted_unverified": frozenset({"verified", "contradicted", "quarantined"}),
    "verified": frozenset(),
    "contradicted": frozenset(),
    "quarantined": frozenset(),
    "failed_pre_submit": frozenset(),
}
_FINAL_STATES = frozenset(
    {"verified", "contradicted", "quarantined", "failed_pre_submit"}
)


def create_prepared(
    conn,
    *,
    queue_name: str,
    url: str,
    dedup_key: str | None,
    worker_id: str,
    route: str,
    route_version: str | None,
    evidence: dict | None = None,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_catalog.has_table_privilege(current_user,'public.apply_attempts','INSERT') AS controller"
        )
        controller = bool(cur.fetchone()["controller"])
    if not controller:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT public.fleet_worker_attempt_create(%s,%s,%s) AS attempt_id",
                (route, route_version, Jsonb(evidence or {})),
            )
            attempt_id = str(cur.fetchone()["attempt_id"])
        conn.commit()
        return attempt_id
    attempt_id = str(uuid.uuid4())
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO apply_attempts "
            "(attempt_id,queue_name,url,dedup_key,worker_id,route,route_version,state,evidence) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'prepared',%s)",
            (
                attempt_id,
                queue_name,
                url,
                dedup_key,
                worker_id,
                route,
                route_version,
                Jsonb(evidence or {}),
            ),
        )
    conn.commit()
    return attempt_id


def transition(
    conn,
    attempt_id: str,
    *,
    expected: str,
    state: str,
    verification_method: str | None = None,
    verification_ref: str | None = None,
    evidence: dict | None = None,
) -> dict:
    if state not in _TRANSITIONS.get(expected, frozenset()):
        raise ValueError(f"invalid apply attempt transition: {expected} -> {state}")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_catalog.has_table_privilege(current_user,'public.apply_attempts','UPDATE') AS controller"
        )
        controller = bool(cur.fetchone()["controller"])
    if not controller:
        if verification_method is not None or verification_ref is not None:
            raise ValueError("worker transitions cannot supply verification authority")
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT public.fleet_worker_attempt_transition(%s,%s,%s,%s) AS attempt",
                    (attempt_id, expected, state, Jsonb(evidence or {})),
                )
                row = cur.fetchone()["attempt"]
            conn.commit()
            return dict(row)
        except errors.SerializationFailure as exc:
            conn.rollback()
            raise AttemptTransitionError(
                f"attempt {attempt_id} is not in expected state {expected}"
            ) from exc
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_attempts SET state=%s, "
                "submit_started_at=CASE WHEN %s='submit_started' "
                "THEN COALESCE(submit_started_at,now()) ELSE submit_started_at END, "
                "finalized_at=CASE WHEN %s=ANY(%s) THEN now() ELSE finalized_at END, "
                "verification_method=COALESCE(%s,verification_method), "
                "verification_ref=COALESCE(%s,verification_ref), "
                "evidence=COALESCE(evidence,'{}'::jsonb) || %s::jsonb "
                "WHERE attempt_id=%s AND state=%s RETURNING *",
                (
                    state,
                    state,
                    state,
                    list(_FINAL_STATES),
                    verification_method,
                    verification_ref,
                    Jsonb(evidence or {}),
                    attempt_id,
                    expected,
                ),
            )
            row = cur.fetchone()
        if row is None:
            conn.rollback()
            raise AttemptTransitionError(
                f"attempt {attempt_id} is not in expected state {expected}"
            )
        conn.commit()
        return dict(row)
    except errors.UniqueViolation as exc:
        conn.rollback()
        raise AttemptConflictError(
            "another unresolved submit already exists for this dedup key"
        ) from exc
