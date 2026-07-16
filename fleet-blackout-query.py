"""fleet-blackout-query.py <machine_label> [role]

PowerShell-friendly guard for launchers. Prints:
  OK|label|role|||
  BLOCKED|label|role|policy|expires_at|reason

Any configuration or database error is reported as BLOCKED so the fleet agent
and direct launchers fail closed.
"""
from __future__ import annotations

import os
import re
import sys

from psycopg.conninfo import conninfo_to_dict


def _normalized_dsn(dsn: str) -> dict[str, str]:
    return dict(conninfo_to_dict(dsn))


def _safe_field(value: object) -> str:
    return " ".join(str(value).replace("|", "/").split())[:300]


def _sanitized_error(exc: Exception, *dsns: str) -> str:
    message = str(exc)
    for dsn in dsns:
        try:
            parsed = _normalized_dsn(dsn)
        except Exception:
            parsed = {}
        for key in ("password", "sslpassword"):
            secret = parsed.get(key)
            if secret:
                message = message.replace(secret, "***")
    message = re.sub(
        r"(?i)\b(postgres(?:ql)?://)(?:[^@\s]+@)?([^\s]+)",
        r"\1***@\2",
        message,
    )
    message = re.sub(
        r"(?i)\b(password|sslpassword|passfile)\s*=\s*(?:'[^']*'|\S+)",
        r"\1=***",
        message,
    )
    return _safe_field(message)

label = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("APPLYPILOT_FLEET_LABEL", "home")
role = sys.argv[2] if len(sys.argv) > 2 else "fleet"

try:
    from applypilot.apply import pgqueue
    from applypilot.fleet import machine_blackout

    fleet_dsn = (os.environ.get("FLEET_PG_DSN") or "").strip()
    applypilot_fleet_dsn = (os.environ.get("APPLYPILOT_FLEET_DSN") or "").strip()
    if (
        fleet_dsn
        and applypilot_fleet_dsn
        and _normalized_dsn(fleet_dsn) != _normalized_dsn(applypilot_fleet_dsn)
    ):
        raise RuntimeError("Inconsistent fleet Postgres DSN references")
    dsn = fleet_dsn or applypilot_fleet_dsn
    if not dsn:
        raise RuntimeError("No fleet Postgres DSN: set FLEET_PG_DSN or APPLYPILOT_FLEET_DSN")
    conn = pgqueue.connect(dsn)
    conn.read_only = True
    print(machine_blackout.status_line(conn, label, role=role))
except Exception as exc:
    diagnostic = _sanitized_error(
        exc,
        os.environ.get("FLEET_PG_DSN", ""),
        os.environ.get("APPLYPILOT_FLEET_DSN", ""),
    )
    safe_label = _safe_field(label).lower()
    safe_role = _safe_field(role).lower()
    print(f"fleet-blackout-query: {type(exc).__name__}: {diagnostic}", file=sys.stderr)
    print(f"BLOCKED|{safe_label}|{safe_role}|blackout-query-error||{diagnostic}")
