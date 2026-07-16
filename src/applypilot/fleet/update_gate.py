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

def busy_reasons(conn, label: str) -> list[str]:
    """Return [] when the box labelled `label` is between jobs, else reasons.

    `label` is the machine label; its workers are `<label>-<slot>` (apply) and
    `<label>-disc-<n>` (discovery) — both match the `<label>-%` prefix.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT public.fleet_worker_runtime_state(%s) AS state", (label,))
        state = cur.fetchone()["state"] or {}
    conn.rollback()
    return list(state.get("update_busy_reasons") or [])
