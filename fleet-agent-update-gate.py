"""fleet-agent-update-gate.py <machine_label>

Print "IDLE" when this box's workers are between jobs (safe to stop for a code
update), else "BUSY|<reason;...>". Used by fleet-agent.ps1 -AutoUpdate and reads only
the explicitly fleet-scoped FLEET_PG_DSN.

FAIL-CLOSED: on any error (DB unreachable, bad label) prints "ERR|<msg>" — the agent
treats anything that is not exactly IDLE as busy and skips the update this tick.
"""
import os
import sys

from fleet_agent_env import require_fleet_pg_dsn

label = sys.argv[1] if len(sys.argv) > 1 else "home"
try:
    from applypilot.apply import pgqueue
    dsn = require_fleet_pg_dsn(os.environ)
    conn = pgqueue.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SELECT public.fleet_worker_runtime_state('') AS state")
        state = cur.fetchone()["state"] or {}
    conn.rollback()
    reasons = list(state.get("update_busy_reasons") or [])
    if reasons:
        print("BUSY|" + ";".join(reasons[:5]))
    else:
        print("IDLE")
except Exception as exc:
    print(f"fleet-agent-update-gate: {type(exc).__name__}: {exc}", file=sys.stderr)
    print(f"ERR|{type(exc).__name__}")  # fail-closed: not IDLE -> no update
