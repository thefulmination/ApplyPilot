"""fleet-blackout-query.py <machine_label> [role]

PowerShell-friendly guard for launchers. Prints:
  OK|label|role|||
  BLOCKED|label|role|policy|expires_at|reason
  KEEP|label|role|||error
"""
from __future__ import annotations

import os
import sys

label = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("APPLYPILOT_FLEET_LABEL", "home")
role = sys.argv[2] if len(sys.argv) > 2 else "fleet"

try:
    from applypilot.apply import pgqueue
    from applypilot.fleet import machine_blackout

    fleet_dsn = (os.environ.get("FLEET_PG_DSN") or "").strip()
    applypilot_fleet_dsn = (os.environ.get("APPLYPILOT_FLEET_DSN") or "").strip()
    if fleet_dsn and applypilot_fleet_dsn and fleet_dsn != applypilot_fleet_dsn:
        raise RuntimeError("Inconsistent fleet Postgres DSN references")
    dsn = fleet_dsn or applypilot_fleet_dsn
    if not dsn:
        raise RuntimeError("No fleet Postgres DSN: set FLEET_PG_DSN or APPLYPILOT_FLEET_DSN")
    conn = pgqueue.connect(dsn)
    conn.read_only = True
    print(machine_blackout.status_line(conn, label, role=role))
except Exception as exc:
    print(f"fleet-blackout-query: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"KEEP|{label}|{role}|||error")
