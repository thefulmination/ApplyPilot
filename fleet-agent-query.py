"""fleet-agent-query.py <machine_label>

Read the mapped machine's server-validated admission snapshot. Used by fleet-agent.ps1 on
each worker box (which has Python+psycopg but usually no psql.exe). It returns desired state only
when the authenticated principal is admitted for the requested machine label. Reads an explicitly
fleet-scoped DSN only and fails closed on every error.

On any error it prints "STOP|||" so stale desired state cannot preserve acquisition authority.
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
        cur.execute("SELECT public.fleet_worker_admission_snapshot() AS state")
        r = cur.fetchone()["state"] or {}
    conn.rollback()
    if r.get("admission_allowed") and str(r.get("machine_owner") or "").casefold() == label.casefold():
        print(
            f"{int(r['desired_workers'])}|{r.get('desired_agent') or 'codex'}|"
            f"{r.get('desired_model') or ''}|{int(r['generation'])}"
        )
    else:
        print("STOP|||")
except Exception as exc:
    print(f"fleet-agent-query: {type(exc).__name__}: control state unavailable", file=sys.stderr)
    print("STOP|||")
