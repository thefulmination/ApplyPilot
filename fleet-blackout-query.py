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

    conn = pgqueue.connect()
    conn.read_only = True
    print(machine_blackout.status_line(conn, label, role=role))
except Exception as exc:
    print(f"fleet-blackout-query: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"KEEP|{label}|{role}|||error")
