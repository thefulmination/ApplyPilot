"""Between-jobs gate for the fleet-agent auto-updater.

A worker box may only stop/restart its local workers for a code update when it is
BETWEEN JOBS (owner-confirmed semantic, spec 2026-07-03-fleet-pull-updater-design):

1. no fresh worker_heartbeat row for this box's workers in a non-idle state, and
2. no live lease held by this box's workers in apply_queue / linkedin_queue.

Polarity is FAIL-CLOSED: callers treat any error as BUSY (do not update blind). That is
the opposite of fleet-agent-query.py's fail-open KEEP — deliberate: leaving workers
alone is safe on a blip, yanking code out from under a mid-apply worker is not.
"""
from __future__ import annotations

HEARTBEAT_FRESH_SECONDS = 150  # console liveness bar; stale beats are dead workers

# 'paused' counts as between-jobs: a remotely-paused worker is heartbeating but by
# definition holds no job, so it must not block a code update (it respawns paused-aware).
_HB_BUSY = """
SELECT worker_id, state
  FROM worker_heartbeat
 WHERE worker_id LIKE %(prefix)s
   AND state NOT IN ('idle', 'paused')
   AND last_beat > now() - make_interval(secs => %(fresh)s)
"""

# A lease blocks the update ONLY if it is plausibly mid-flight: unexpired within a sane
# TTL horizon (real leases run ~20 min; challenge-PARKED rows sit ~10 years out and must
# not block) AND its owner has a fresh heartbeat (a lease whose worker died is an orphan
# awaiting reclaim -- there is no process to interrupt). Verified live 2026-07-03: m2 had
# 105 parked leases (expiry 2036) from workers dead since 6/30.
_LEASE_BUSY = """
SELECT COUNT(*) AS n
  FROM {table} q
 WHERE q.lease_owner LIKE %(prefix)s
   AND q.status = 'leased'
   AND q.lease_expires_at > now()
   AND q.lease_expires_at < now() + interval '1 day'
   AND EXISTS (SELECT 1 FROM worker_heartbeat h
                WHERE h.worker_id = q.lease_owner
                  AND h.last_beat > now() - make_interval(secs => %(fresh)s))
"""


def busy_reasons(conn, label: str) -> list[str]:
    """Return [] when the box labelled `label` is between jobs, else reasons.

    `label` is the machine label; its workers are `<label>-<slot>` (apply) and
    `<label>-disc-<n>` (discovery) — both match the `<label>-%` prefix.
    """
    prefix = f"{label}-%"
    reasons: list[str] = []

    with conn.cursor() as cur:
        cur.execute(_HB_BUSY, {"prefix": prefix, "fresh": HEARTBEAT_FRESH_SECONDS})
        for row in cur.fetchall():
            reasons.append(f"heartbeat:{row['worker_id']}:{row['state']}")

    for table in ("apply_queue", "linkedin_queue"):
        try:
            with conn.cursor() as cur:
                cur.execute(_LEASE_BUSY.format(table=table),
                            {"prefix": prefix, "fresh": HEARTBEAT_FRESH_SECONDS})
                n = cur.fetchone()["n"]
            if n:
                reasons.append(f"{table}:live_leases:{n}")
        except Exception:
            # table absent on this PG (older schema) -> nothing to lease there
            conn.rollback()

    return reasons
