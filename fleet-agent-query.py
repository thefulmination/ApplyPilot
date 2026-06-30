"""fleet-agent-query.py <machine_label>

Print "<desired_workers>|<agent>|<model>|<generation>" for one machine from the home Postgres
table fleet_desired_state. Used by fleet-agent.ps1 on each worker box (which has Python+psycopg but
usually no psql.exe). Reads the DSN from FLEET_PG_DSN / APPLYPILOT_FLEET_DSN / DATABASE_URL.

On any error (DB unreachable, table missing) it prints "KEEP|||" so the agent leaves the local
workers exactly as-is rather than killing them on a transient blip (fail-safe, never fail-destructive).
"""
import os
import sys

label = sys.argv[1] if len(sys.argv) > 1 else "home"
dsn = (os.environ.get("FLEET_PG_DSN") or os.environ.get("APPLYPILOT_FLEET_DSN")
       or os.environ.get("DATABASE_URL"))
try:
    from applypilot.apply import pgqueue
    conn = pgqueue.connect(dsn)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT desired_workers, agent, COALESCE(model,'') AS model, generation "
            "FROM fleet_desired_state WHERE machine_owner=%s", (label,))
        r = cur.fetchone()
    if r:
        print(f"{r['desired_workers']}|{r['agent']}|{r['model']}|{r['generation']}")
    else:
        # no row for this machine yet -> desired 0 (idle), but say so explicitly
        print("0|claude||0")
except Exception as exc:
    print(f"fleet-agent-query: {type(exc).__name__}: {exc}", file=sys.stderr)
    print("KEEP|||")  # fail-safe: leave local workers untouched
