"""fleet-agent-update-gate.py <machine_label>

Print "IDLE" when this box's workers are between jobs (safe to stop for a code
update), else "BUSY|<reason;...>". Used by fleet-agent.ps1 -AutoUpdate. Reads the DSN
from FLEET_PG_DSN / APPLYPILOT_FLEET_DSN / DATABASE_URL (same as fleet-agent-query.py).

FAIL-CLOSED: on any error (DB unreachable, bad label) prints "ERR|<msg>" — the agent
treats anything that is not exactly IDLE as busy and skips the update this tick.
"""
import os
import sys

label = sys.argv[1] if len(sys.argv) > 1 else "home"
dsn = (os.environ.get("FLEET_PG_DSN") or os.environ.get("APPLYPILOT_FLEET_DSN")
       or os.environ.get("DATABASE_URL"))
try:
    from applypilot.apply import pgqueue
    from applypilot.fleet import update_gate

    conn = pgqueue.connect(dsn)
    reasons = update_gate.busy_reasons(conn, label)
    if reasons:
        print("BUSY|" + ";".join(reasons[:5]))
    else:
        print("IDLE")
except Exception as exc:
    print(f"fleet-agent-update-gate: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"ERR|{type(exc).__name__}")  # fail-closed: not IDLE -> no update
