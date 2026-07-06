"""Central expiring machine blackout policies for ApplyPilot fleet work."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Iterable


@dataclass(frozen=True)
class MachinePolicyVerdict:
    allowed: bool
    machine: str
    role: str
    policy_name: str | None = None
    expires_at: datetime | None = None
    reason: str | None = None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_label(label: str | None) -> str:
    return (label or "").strip().lower()


def _patterns(values: Iterable[str] | None) -> list[str]:
    return [normalize_label(v) for v in (values or []) if normalize_label(v)]


def _matches(label: str, patterns: Iterable[str] | None) -> bool:
    norm = normalize_label(label)
    for pattern in _patterns(patterns):
        if pattern == "*" or fnmatchcase(norm, pattern):
            return True
    return False


def active_blackouts(conn, *, now: datetime | None = None) -> list[dict]:
    """Return active, non-expired blackout policies ordered newest first."""
    current = now or _utcnow()
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, starts_at, expires_at, allow_patterns, block_patterns, reason
            FROM fleet_machine_blackout
            WHERE active = TRUE
              AND cleared_at IS NULL
              AND starts_at <= %s
              AND expires_at > %s
            ORDER BY starts_at DESC, id DESC
            """,
            (current, current),
        )
        rows = [dict(r) for r in cur.fetchall()]
    conn.rollback()
    return rows


def is_machine_allowed(
    conn,
    machine_label: str,
    *,
    role: str = "fleet",
    now: datetime | None = None,
) -> MachinePolicyVerdict:
    """Return whether this machine may run fleet work under current policies."""
    label = normalize_label(machine_label)
    role_name = normalize_label(role) or "fleet"
    for policy in active_blackouts(conn, now=now):
        if _matches(label, policy.get("allow_patterns")):
            continue
        if _matches(label, policy.get("block_patterns")):
            return MachinePolicyVerdict(
                allowed=False,
                machine=label,
                role=role_name,
                policy_name=policy.get("name"),
                expires_at=policy.get("expires_at"),
                reason=policy.get("reason"),
            )
    return MachinePolicyVerdict(allowed=True, machine=label, role=role_name)


def create_blackout(
    conn,
    *,
    name: str,
    expires_at: datetime,
    allow_patterns: Iterable[str],
    block_patterns: Iterable[str] = ("*",),
    reason: str = "",
    starts_at: datetime | None = None,
    created_by: str = "operator",
    now: datetime | None = None,
) -> int:
    """Create a new active blackout policy and return its id."""
    start = starts_at or now or _utcnow()
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if expires_at <= start:
        raise ValueError("expires_at must be after starts_at")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fleet_machine_blackout
                (name, starts_at, expires_at, allow_patterns, block_patterns, reason, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                name,
                start,
                expires_at,
                _patterns(allow_patterns),
                _patterns(block_patterns),
                reason,
                created_by,
            ),
        )
        policy_id = int(cur.fetchone()["id"])
    conn.commit()
    return policy_id


def clear_blackouts(conn, *, name: str | None = None) -> int:
    """Clear active blackouts. If name is provided, clear only that policy name."""
    with conn.cursor() as cur:
        if name:
            cur.execute(
                """
                UPDATE fleet_machine_blackout
                SET active = FALSE, cleared_at = now()
                WHERE active = TRUE AND cleared_at IS NULL AND name = %s
                """,
                (name,),
            )
        else:
            cur.execute(
                """
                UPDATE fleet_machine_blackout
                SET active = FALSE, cleared_at = now()
                WHERE active = TRUE AND cleared_at IS NULL
                """
            )
        count = cur.rowcount
    conn.commit()
    return int(count)


def status_line(conn, machine_label: str, *, role: str = "fleet", now: datetime | None = None) -> str:
    """Return a compact line for PowerShell guards."""
    verdict = is_machine_allowed(conn, machine_label, role=role, now=now)
    if verdict.allowed:
        return f"OK|{verdict.machine}|{verdict.role}|||"
    expires = verdict.expires_at.isoformat() if verdict.expires_at else ""
    reason = (verdict.reason or "").replace("\n", " ")[:300]
    return f"BLOCKED|{verdict.machine}|{verdict.role}|{verdict.policy_name or ''}|{expires}|{reason}"
