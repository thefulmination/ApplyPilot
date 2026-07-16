"""fleet-agent-query.py <machine_label>

Validate one machine's desired-state control row from home Postgres. Used by fleet-agent.ps1 on
each worker box (which has Python+psycopg but usually no psql.exe). During the emergency hold it
always prints "STOP|||" after a successful validation and on every error. Reads an explicitly
fleet-scoped DSN only.

On any error it prints "STOP|||" so stale desired state cannot preserve acquisition authority.
"""
import os
import sys

label = sys.argv[1] if len(sys.argv) > 1 else "home"
fleet_dsn = os.environ.get("FLEET_PG_DSN")
applypilot_dsn = os.environ.get("APPLYPILOT_FLEET_DSN")
dsn = fleet_dsn or applypilot_dsn
try:
    from applypilot.apply import pgqueue
    if not dsn:
        raise RuntimeError("fleet DSN unavailable")
    if fleet_dsn and applypilot_dsn and fleet_dsn != applypilot_dsn:
        raise RuntimeError("inconsistent fleet DSN references")
    conn = pgqueue.connect(dsn)
    conn.read_only = True
    with conn.cursor() as cur:
        cur.execute(
            "SELECT desired_workers, agent, COALESCE(model,'') AS model, generation "
            "FROM fleet_desired_state WHERE machine_owner=%s", (label,))
        r = cur.fetchone()
    if r:
        print("STOP|||")
    else:
        print("STOP|||")
except Exception as exc:
    print(f"fleet-agent-query: {type(exc).__name__}: control state unavailable", file=sys.stderr)
    print("STOP|||")
