"""Layer B -- the bounded Claude-monitor surface (spec §3). The monitor is a
periodic SECOND OPINION, not load-bearing: read health, write a report, and take
only ALLOWLISTED actions. The deny-set (resolve a parked challenge, change a cost
cap, resume a paused/LinkedIn scope, approve/cause an apply) is enforced by
ABSENCE -- those operations are simply not methods on MonitorActions, so neither a
prompt-injected agent nor a bug can invoke them through this surface."""
from __future__ import annotations

from applypilot.fleet import governor, heartbeat

# A4: the ONLY scope-key prefixes the bounded monitor surface may pause. A free-form scope_key
# with NO guard let pause_scope('account:linkedin') halt the LinkedIn catastrophe lane forever
# (breaker_until=NULL) outside any gate -- the exact D2 violation, reached through an actuator the
# Doctor's gate never sees. We allow-LIST host:/board: only; everything else (account:linkedin,
# global, home_ip:, bare strings) is rejected BEFORE the UPDATE.
_PAUSABLE_SCOPE_PREFIXES = ("host:", "board:")

# A4: default cool-down for a legitimate monitor pause -- breaker_until is set (never NULL) so
# clear_expired_breakers can time-recover it (mirrors evaluate_breakers' paused branch).
_MONITOR_PAUSE_COOL_SECONDS = 1800


class ScopeNotPausable(ValueError):
    """A4: raised when pause_scope is asked to pause a forbidden scope (LinkedIn / global /
    home_ip / any non host:/board: scope). Surfaced as a structured codex_bridge error."""


class MonitorActions:
    """The ONLY mutation surface the monitor is given. Every method here is on the
    allow-list (spec §3.1). Denied operations are intentionally NOT defined."""

    def __init__(self, conn):
        self._conn = conn

    def restart_worker(self, worker_id: str) -> int:
        """Enqueue a 'restart' command for a stuck worker."""
        return heartbeat.issue_command(self._conn, worker_id, "restart")

    def quarantine(self, url: str, *, worker: str, reason: str) -> bool:
        """Quarantine a poison job (deliberate one-shot: pulls immediately, does not
        pollute crash_count). Stops the job being re-leased."""
        return heartbeat.quarantine_job(self._conn, url, worker=worker, reason=reason, manual=True)

    def pause_scope(self, scope_key: str) -> None:
        """Pause a host/board scope (bounded write). Does NOT resume -- resume of any
        paused scope is owner-only and absent by design.

        A4 HARD GUARD: only ``host:``/``board:`` scopes may be paused. The LinkedIn account
        (``account:linkedin``), ``global``, and any ``home_ip:`` scope are REJECTED before any
        write -- a free-form scope_key on this surface could otherwise halt the LinkedIn
        catastrophe lane forever, outside the Doctor gate entirely. A4: the pause is
        AUTO-EXPIRING (breaker_until = now()+cool, never NULL) so clear_expired_breakers can
        time-recover it instead of leaving it sticky."""
        sk = str(scope_key or "")
        if sk == governor.LINKEDIN_ACCOUNT or sk == governor.GLOBAL \
                or sk.startswith("home_ip:") or not sk.startswith(_PAUSABLE_SCOPE_PREFIXES):
            raise ScopeNotPausable(
                f"scope_key {scope_key!r} is not pausable via the monitor surface; only "
                "'host:'/'board:' scopes may be paused (the LinkedIn lane / global / per-IP "
                "scopes are off-limits here). (A4)")
        # A4: route through the auto-expiring breaker primitive (breaker_until set, never NULL).
        governor.trip_breaker(self._conn, sk, state="paused",
                              cool_seconds=_MONITOR_PAUSE_COOL_SECONDS, commit=True)

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
