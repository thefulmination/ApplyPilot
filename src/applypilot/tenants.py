"""ats_tenants registry: status/submit/halt helpers for login-gated ATS tenants
(auth-gated-tenant-lane Task 1).

A "tenant" here is an ATS host (e.g. a Workday subdomain like
acme.wd1.myworkdayjobs.com). Rollout is tenant-by-tenant and gated by a
three-state status:

    excluded    -- never attempted (default; safest state)
    supervised  -- human-in-the-loop submits allowed
    trusted     -- autonomous submits allowed (requires evidence: >=3 clean
                   submits, or an explicit --force override)

No ATS password/secret is ever stored here -- this module is a status
registry only.
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from datetime import datetime, timezone
from typing import Any

STATUSES = {"excluded", "supervised", "trusted"}
_TRUSTED_EVIDENCE_THRESHOLD = 3


def _host_of(url: str) -> str:
    """Extract the lowercased hostname from a URL, stripping a leading 'www.'.

    Mirrors applypilot.apply.liveness.host_of / applypilot.fleet.queue.host_of;
    duplicated locally (rather than imported) to avoid pulling apply/fleet
    module dependencies into this lightweight registry module.
    """
    h = (urllib.parse.urlsplit(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def tenant_status(conn: sqlite3.Connection, host: str) -> str:
    """Return the tenant's status, or 'excluded' if there's no row, or the
    table doesn't exist yet (defensive against pre-migration DBs)."""
    try:
        row = conn.execute(
            "SELECT status FROM ats_tenants WHERE host = ?", (host,)
        ).fetchone()
    except sqlite3.OperationalError:
        return "excluded"

    if row is None:
        return "excluded"
    # Support both sqlite3.Row and plain tuple row factories.
    return row["status"] if isinstance(row, sqlite3.Row) else row[0]


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {k: row[k] for k in row.keys()}


def list_tenants(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return all rows in ats_tenants as plain dicts."""
    rows = conn.execute(
        "SELECT host, status, clean_submits, failed_submits, daily_cap, "
        "halted_until, last_result, updated_at FROM ats_tenants ORDER BY host"
    ).fetchall()
    return [_row_to_dict(r) for r in rows]


def _get_row(conn: sqlite3.Connection, host: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT host, status, clean_submits, failed_submits, daily_cap, "
        "halted_until, last_result, updated_at FROM ats_tenants WHERE host = ?",
        (host,),
    ).fetchone()


def set_tenant(
    conn: sqlite3.Connection, host: str, status: str, *, force: bool = False
) -> dict[str, Any]:
    """Upsert a tenant's status.

    Raises ValueError if `status` isn't one of the 3-set, or if promoting to
    'trusted' without enough clean-submit evidence (and not forced).
    """
    if status not in STATUSES:
        raise ValueError(f"invalid status {status!r}; must be one of {sorted(STATUSES)}")

    existing = _get_row(conn, host)

    if status == "trusted" and not force:
        clean_submits = existing["clean_submits"] if existing is not None else 0
        if clean_submits < _TRUSTED_EVIDENCE_THRESHOLD:
            raise ValueError("needs >=3 clean submits (or --force)")

    now = datetime.now(timezone.utc).isoformat()

    if existing is None:
        conn.execute(
            "INSERT INTO ats_tenants (host, status, updated_at) VALUES (?, ?, ?)",
            (host, status, now),
        )
    else:
        conn.execute(
            "UPDATE ats_tenants SET status = ?, updated_at = ? WHERE host = ?",
            (status, now, host),
        )
    conn.commit()

    return _row_to_dict(_get_row(conn, host))


def record_submit(
    conn: sqlite3.Connection, host: str, *, ok: bool, result: str | None
) -> None:
    """Record a submit attempt outcome, incrementing clean_submits or
    failed_submits and updating last_result/updated_at. Creates the row
    (status='excluded') if it doesn't exist yet."""
    existing = _get_row(conn, host)
    now = datetime.now(timezone.utc).isoformat()

    if existing is None:
        conn.execute(
            "INSERT INTO ats_tenants (host, status, clean_submits, failed_submits, "
            "last_result, updated_at) VALUES (?, 'excluded', ?, ?, ?, ?)",
            (host, 1 if ok else 0, 0 if ok else 1, result, now),
        )
    else:
        col = "clean_submits" if ok else "failed_submits"
        conn.execute(
            f"UPDATE ats_tenants SET {col} = {col} + 1, last_result = ?, "
            "updated_at = ? WHERE host = ?",
            (result, now, host),
        )
    conn.commit()


def halt_tenant(conn: sqlite3.Connection, host: str, until_iso: str) -> None:
    """Set halted_until for a tenant, creating the row if absent."""
    existing = _get_row(conn, host)
    now = datetime.now(timezone.utc).isoformat()

    if existing is None:
        conn.execute(
            "INSERT INTO ats_tenants (host, status, halted_until, updated_at) "
            "VALUES (?, 'excluded', ?, ?)",
            (host, until_iso, now),
        )
    else:
        conn.execute(
            "UPDATE ats_tenants SET halted_until = ?, updated_at = ? WHERE host = ?",
            (until_iso, now, host),
        )
    conn.commit()


def is_halted(conn: sqlite3.Connection, host: str, now_iso: str) -> bool:
    """True if the tenant has a halted_until timestamp that is still in the
    future relative to now_iso (ISO-8601 strings compare lexicographically
    when in the same, zero-padded format)."""
    row = conn.execute(
        "SELECT halted_until FROM ats_tenants WHERE host = ?", (host,)
    ).fetchone()
    if row is None:
        return False
    halted_until = row["halted_until"] if isinstance(row, sqlite3.Row) else row[0]
    if not halted_until:
        return False
    return now_iso < halted_until


def submits_today(
    conn: sqlite3.Connection, host: str, *, today_iso: str | None = None
) -> int:
    """Count applications submitted today for the given tenant host.

    Selects `applications.job_url` for rows whose `applied_at` starts with the
    day prefix, then filters host-equality in Python via `_host_of` (the same
    hostname derivation Task 3's acquire filter uses, so the daily-cap counts
    the same host key). No SQL JOIN — `applications` has no host column.

    The day is a UTC calendar day by default (`today_iso` = today's UTC date,
    ISO 'YYYY-MM-DD'), so the per-tenant cap resets at 00:00 UTC, not local
    midnight. Pass an explicit `today_iso` to count a different day. Assumes
    `applied_at` is an ISO-8601 string prefixed by its date (true for every
    writer in database.py's applications/backfill code).
    """
    if today_iso is None:
        today_iso = datetime.now(timezone.utc).date().isoformat()

    rows = conn.execute(
        "SELECT job_url FROM applications WHERE applied_at LIKE ? ",
        (f"{today_iso}%",),
    ).fetchall()

    count = 0
    for row in rows:
        url = row["job_url"] if isinstance(row, sqlite3.Row) else row[0]
        if url and _host_of(url) == host:
            count += 1
    return count


def daily_cap(conn: sqlite3.Connection, host: str) -> int:
    """Return the tenant's configured daily_cap, or 5 (the table default) if
    there's no row for this host yet or the table doesn't exist (defensive
    against pre-migration DBs)."""
    try:
        row = conn.execute(
            "SELECT daily_cap FROM ats_tenants WHERE host = ?", (host,)
        ).fetchone()
    except sqlite3.OperationalError:
        return 5

    if row is None:
        return 5
    value = row["daily_cap"] if isinstance(row, sqlite3.Row) else row[0]
    return 5 if value is None else int(value)
