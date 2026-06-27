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


def build_health_report(snapshot: dict, *, captcha_threshold: float = 0.4,
                        cost_cap_total: float | None = None) -> str:
    """Render a text health report from a dashboard_snapshot, with a 'NEEDS YOUR
    DECISION' section listing anomalies the monitor will NOT auto-fix."""
    lines: list[str] = []
    anomalies: list[str] = []

    lines.append("=== MACHINES ===")
    for m in snapshot.get("machines", []):
        beat = m.get("last_beat")
        flag = "  <OFFLINE: no heartbeat>" if not beat else ""
        lines.append(f"  {m.get('worker_id')} [{m.get('role')}] state={m.get('state')}{flag}")
        if not beat:
            anomalies.append(f"worker {m.get('worker_id')} offline (no heartbeat)")

    lines.append("=== QUEUES ===")
    for lane, depths in (snapshot.get("queue_depth") or {}).items():
        lines.append(f"  {lane}: {dict(depths)}")

    lines.append("=== GOVERNOR ===")
    for g in snapshot.get("governor", []):
        rate = float(g.get("challenge_rate") or 0)
        flag = "  <HIGH CHALLENGE RATE>" if rate >= captcha_threshold else ""
        lines.append(f"  {g.get('scope_key')} state={g.get('breaker_state')} "
                     f"rate={rate:.2f} n={g.get('count_24h')}{flag}")
        if rate >= captcha_threshold:
            anomalies.append(f"scope {g.get('scope_key')} challenge_rate {rate:.2f} >= {captcha_threshold}")

    lines.append(f"=== CAPTCHA BACKLOG ===\n  open challenges: {snapshot.get('captcha_backlog', 0)}; "
                 f"quarantined jobs: {snapshot.get('quarantine', 0)}")

    spend = float(snapshot.get("spend_today") or 0)
    cap_str = f" / cap {cost_cap_total}" if cost_cap_total else ""
    lines.append(f"=== SPEND ===\n  last 24h: ${spend:.2f}{cap_str}")
    if cost_cap_total and cost_cap_total > 0 and spend >= 0.9 * cost_cap_total:
        anomalies.append(f"spend ${spend:.2f} is within 90% of cap ${cost_cap_total:.2f}")

    lines.append("=== NEEDS YOUR DECISION ===")
    if anomalies:
        lines.extend(f"  - {a}" for a in anomalies)
    else:
        lines.append("  none")

    return "\n".join(lines)
