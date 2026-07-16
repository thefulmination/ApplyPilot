"""Fail-closed admission boundary for the preimplementation authority hold."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
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


def launcher_admission() -> AdmissionResult:
    return acquisition_admission("direct launcher invocation")


def worker_tick_admission() -> AdmissionResult:
    return acquisition_admission("apply worker tick")


def linkedin_worker_admission() -> AdmissionResult:
    return acquisition_admission("LinkedIn worker startup")


def linkedin_tick_admission() -> AdmissionResult:
    return acquisition_admission("LinkedIn worker tick")


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
            cur.execute("SELECT to_regclass('fleet_desired_state') AS desired_state_table")
            if _row_value(cur.fetchone(), "desired_state_table") is None:
                return deny("worker denied: fleet enrollment control is unavailable")
            cur.execute(
                "SELECT desired_workers, generation, updated_at "
                "FROM fleet_desired_state WHERE machine_owner=%s",
                (owner,),
            )
            desired = cur.fetchone()
            if desired is None:
                return deny("worker denied: machine is not enrolled in desired state")
            cur.execute(
                "SELECT machine_owner, validated, revoked_at FROM workers WHERE worker_id=%s",
                (worker,),
            )
            enrollment = cur.fetchone()
            if enrollment is None:
                return deny("worker denied: worker-id is not enrolled")
            cur.execute("SELECT paused, COALESCE(ats_paused, FALSE) AS ats_paused FROM fleet_config WHERE id=1")
            pause = cur.fetchone()
    except Exception:
        return deny("worker denied: control database unavailable or installed state ambiguous")

    desired_workers = _row_value(desired, "desired_workers", 0)
    generation = _row_value(desired, "generation", 1)
    updated_at = _row_value(desired, "updated_at", 2)
    if not isinstance(desired_workers, int) or desired_workers <= 0 or not isinstance(generation, int):
        return deny("worker denied: desired state is inactive or ambiguous")
    if not isinstance(updated_at, datetime):
        return deny("worker denied: desired state freshness is unavailable")
    enrolled_owner = str(_row_value(enrollment, "machine_owner", 0) or "")
    if enrolled_owner.casefold() != owner.casefold():
        return deny("worker denied: worker enrollment belongs to another machine")
    if not bool(_row_value(enrollment, "validated", 1)) or _row_value(enrollment, "revoked_at", 2) is not None:
        return deny("worker denied: worker enrollment is unvalidated or revoked")
    current = now or datetime.now(timezone.utc)
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=timezone.utc)
    if current - updated_at.astimezone(timezone.utc) > max_desired_state_age:
        return deny("worker denied: desired state is stale")
    if pause is None or bool(_row_value(pause, "paused", 0)) or bool(_row_value(pause, "ats_paused", 1)):
        return deny("worker denied: control pause is active or unavailable")
    return deny(f"worker admission validated but denied: {EMERGENCY_HOLD_REASON}")
