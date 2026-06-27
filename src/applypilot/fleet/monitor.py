"""Layer B -- the bounded Claude-monitor surface (spec §3). The monitor is a
periodic SECOND OPINION, not load-bearing: read health, write a report, and take
only ALLOWLISTED actions. The deny-set (resolve a parked challenge, change a cost
cap, resume a paused/LinkedIn scope, approve/cause an apply) is enforced by
ABSENCE -- those operations are simply not methods on MonitorActions, so neither a
prompt-injected agent nor a bug can invoke them through this surface."""
from __future__ import annotations

from applypilot.fleet import heartbeat


class MonitorActions:
    """The ONLY mutation surface the monitor is given. Every method here is on the
    allow-list (spec §3.1). Denied operations are intentionally NOT defined."""

    def __init__(self, conn):
        self._conn = conn

    def restart_worker(self, worker_id: str) -> int:
        """Enqueue a 'restart' command for a stuck worker."""
        return heartbeat.issue_command(self._conn, worker_id, "restart")

    def quarantine(self, url: str, *, worker: str, reason: str) -> bool:
        """Quarantine a poison job so it stops being re-leased."""
        return heartbeat.quarantine_job(self._conn, url, worker=worker, reason=reason)

    def pause_scope(self, scope_key: str) -> None:
        """Pause a host/board scope (bounded write). Does NOT resume -- resume of any
        paused scope (esp. the LinkedIn lane) is owner-only and absent by design."""
        with self._conn.cursor() as cur:
            cur.execute("UPDATE rate_governor SET breaker_state='paused', breaker_until=NULL, "
                        "updated_at=now() WHERE scope_key=%s", (scope_key,))
        self._conn.commit()

    def report(self, text: str) -> str:
        """Emit/return a report string (alert hook)."""
        return text
