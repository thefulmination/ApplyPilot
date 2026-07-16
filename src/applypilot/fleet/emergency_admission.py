"""Fail-closed admission boundary for the preimplementation authority hold."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
import os
from typing import Any


class AdmissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True)
class AdmissionResult:
    decision: AdmissionDecision
    reason: str
    code: str = "EMERGENCY_HOLD"

    @property
    def allowed(self) -> bool:
        return self.decision is AdmissionDecision.ALLOW


EMERGENCY_HOLD_REASON = (
    "emergency acquisition hold is active; browser acquisition remains denied "
    "until the separately approved authority cutover"
)

_READ_ONLY_HOME_COMMANDS = frozenset(
    {
        "status",
        "readiness",
        "canary-readiness",
        "challenges",
        "crash-review",
        "dedup-review",
        # This command only refreshes public-posting evidence for parked rows;
        # it never acquires a browser or changes an apply outcome.
        "crash-liveness",
    }
)


DENIAL_MARKER = "APPLYPILOT_ADMISSION_DENIED"
DENIAL_EXIT_CODE = 78


def allow(reason: str) -> AdmissionResult:
    return AdmissionResult(AdmissionDecision.ALLOW, reason, "ALLOWED")


def deny(reason: str) -> AdmissionResult:
    return AdmissionResult(AdmissionDecision.DENY, reason)


def require_allowed(result: AdmissionResult) -> None:
    if not result.allowed:
        raise SystemExit(result.reason)


def denial_marker(result: AdmissionResult) -> str:
    return f"{DENIAL_MARKER}:{result.code}"


def acquisition_admission(source: str) -> AdmissionResult:
    return deny(f"{source} denied: {EMERGENCY_HOLD_REASON}")


def local_apply_admission(*, target_url: str | None = None) -> AdmissionResult:
    invocation = "targeted local apply" if target_url else "local apply"
    return deny(f"{invocation} denied: {EMERGENCY_HOLD_REASON}")


def launcher_admission(conn=None) -> AdmissionResult:
    return _runtime_admission(conn, lane="apply", source="direct launcher invocation")


def worker_tick_admission(conn) -> AdmissionResult:
    return _runtime_admission(conn, lane="apply", source="apply worker tick")


def linkedin_worker_admission(conn=None) -> AdmissionResult:
    return _runtime_admission(conn, lane="linkedin", source="LinkedIn worker startup")


def linkedin_tick_admission(conn) -> AdmissionResult:
    return _runtime_admission(conn, lane="linkedin", source="LinkedIn worker tick")


def compute_worker_admission(conn=None) -> AdmissionResult:
    return _runtime_admission(conn, lane="compute", source="compute worker startup")


def discovery_worker_admission(conn=None) -> AdmissionResult:
    return _runtime_admission(conn, lane="discovery", source="discovery worker startup")


def workday_onboard_admission() -> AdmissionResult:
    return acquisition_admission("Workday onboarding")


def workday_rollout_admission(stage: str) -> AdmissionResult:
    if stage == "report":
        return allow("read-only Workday rollout report")
    return acquisition_admission(f"Workday rollout stage '{stage}'")


def linkedin_home_admission(command: str) -> AdmissionResult:
    if command in {"status", "challenges"}:
        return allow(f"read-only LinkedIn home command: {command}")
    return acquisition_admission(f"LinkedIn home mutation '{command}'")


def apply_home_admission(command: str) -> AdmissionResult:
    if command in _READ_ONLY_HOME_COMMANDS:
        return allow(f"read-only apply-home command: {command}")
    return deny(f"apply-home mutation '{command}' denied: {EMERGENCY_HOLD_REASON}")


def _row_value(row: Any, name: str, index: int = 0) -> Any:
    if row is None:
        return None
    if hasattr(row, "get"):
        return row.get(name)
    try:
        return row[index]
    except (IndexError, KeyError, TypeError):
        return None


def worker_admission(
    conn,
    *,
    machine_label: str | None,
    machine_owner: str | None,
    worker_id: str | None,
    now: datetime | None = None,
    max_desired_state_age: timedelta = timedelta(minutes=5),
) -> AdmissionResult:
    """Read control state without DDL and deny every ambiguous or stale worker."""
    label = (machine_label or "").strip()
    owner = (machine_owner or "").strip()
    worker = (worker_id or "").strip()
    if not label:
        return deny("worker denied: APPLYPILOT_FLEET_LABEL is missing")
    if not owner or not worker:
        return deny("worker denied: machine-owner or worker-id enrollment identity is missing")
    if label.casefold() != owner.casefold():
        return deny("worker denied: machine label does not match machine-owner enrollment")

    try:
        with conn.cursor() as cur:
            cur.execute("SELECT public.fleet_worker_admission_snapshot() AS snapshot")
            snapshot = cur.fetchone()["snapshot"]
    except Exception:
        return deny("worker denied: control database unavailable or installed state ambiguous")

    if not snapshot or str(snapshot.get("worker_id") or "") != worker:
        return deny("worker denied: database principal does not match worker identity")
    desired_workers = snapshot.get("desired_workers")
    generation = snapshot.get("generation")
    updated_at = snapshot.get("desired_updated_at")
    if isinstance(updated_at, str):
        try:
            updated_at = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
        except ValueError:
            updated_at = None
    if not isinstance(desired_workers, int) or desired_workers <= 0 or not isinstance(generation, int):
        return deny("worker denied: desired state is inactive or ambiguous")
    if not isinstance(updated_at, datetime):
        return deny("worker denied: desired state freshness is unavailable")
    enrolled_owner = str(snapshot.get("machine_owner") or "")
    if enrolled_owner.casefold() != owner.casefold():
        return deny("worker denied: worker enrollment belongs to another machine")
    if not bool(snapshot.get("validated")) or snapshot.get("revoked_at") is not None:
        return deny("worker denied: worker enrollment is unvalidated or revoked")
    current = now or datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    if current - updated_at.astimezone(timezone.utc) > max_desired_state_age:
        return deny("worker denied: desired state is stale")
    if not bool(snapshot.get("admission_allowed")) and not _paused_heartbeat_only(snapshot):
        return deny(f"worker denied: {snapshot.get('admission_reason') or 'admission_failed'}")
    if _paused_heartbeat_only(snapshot):
        return allow("worker admitted for paused heartbeat only")
    return allow("worker admission authorized by mapped database control state")


def _paused_heartbeat_only(snapshot: dict[str, Any]) -> bool:
    """Permit an enrolled worker to stay alive while all acquisition is paused.

    Lease authorization remains fail-closed in Postgres. This exception exists so
    operators can prove identity, version, and liveness before lifting a pause.
    """
    reason = snapshot.get("admission_reason")
    return (
        not snapshot.get("admission_allowed")
        and reason in {"global_paused", "ats_paused", "heartbeat_stale", "version_mismatch"}
        and bool(snapshot.get("validated"))
        and snapshot.get("revoked_at") is None
        and isinstance(snapshot.get("desired_workers"), int)
        and snapshot.get("desired_workers") > 0
        and (bool(snapshot.get("paused")) or bool(snapshot.get("ats_paused")))
    )


def _runtime_admission(conn, *, lane: str, source: str) -> AdmissionResult:
    owns_connection = conn is None
    if owns_connection:
        dsn = os.environ.get("FLEET_PG_DSN")
        if not dsn:
            return deny(f"{source} denied: fleet DSN unavailable")
        try:
            from applypilot.apply import pgqueue

            conn = pgqueue.connect(dsn)
        except Exception:
            return deny(f"{source} denied: control database unavailable")
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT public.fleet_worker_admission_snapshot() AS snapshot")
            snapshot = cur.fetchone()["snapshot"] or {}
        conn.rollback()
    except Exception:
        return deny(f"{source} denied: control database unavailable or principal unmapped")
    finally:
        if owns_connection and conn is not None:
            conn.close()
    expected = "apply" if lane == "ats" else lane
    if snapshot.get("contract") != expected:
        return deny(f"{source} denied: mapped worker contract mismatch")
    if not snapshot.get("admission_allowed") and not _paused_heartbeat_only(snapshot):
        return deny(f"{source} denied: {snapshot.get('admission_reason') or 'admission_failed'}")
    if _paused_heartbeat_only(snapshot):
        return allow(f"{source} admitted for paused heartbeat only")
    return allow(f"{source} authorized by mapped database control state")
