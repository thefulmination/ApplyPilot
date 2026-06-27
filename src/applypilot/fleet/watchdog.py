"""Deterministic fleet watchdog (spec §2) -- the no-LLM workhorse that runs the
foundation's recovery primitives on a cadence so the fleet self-heals unattended.

`watchdog_tick(conn, cfg)` is a single pure pass (testable against seeded PG);
`run_watchdog(conn_factory, cfg, stop=...)` drives it on a clock; `main` is the
`applypilot-fleet-watchdog` entrypoint. The watchdog beats its own liveness via
`worker_heartbeat` (worker_id='watchdog') so a dead watchdog is itself visible.
"""
from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass

from applypilot.apply import pgqueue
from applypilot.fleet import governor, heartbeat, queue

WATCHDOG_ID = "watchdog"
WATCHDOG_ROLE = "watchdog"


@dataclass
class WatchdogConfig:
    heartbeat_timeout: int = 90
    job_max_seconds: int = 600
    quarantine_threshold: int = 3
    captcha_threshold: float = 0.4
    breaker_min_samples: int = 8
    breaker_cool_seconds: int = 1800
    reclaim_grace_seconds: int = 30
    cadence_seconds: int = 25
    nightly_roll_hour: int = 4


def watchdog_tick(conn, cfg: WatchdogConfig) -> dict:
    """Run one recovery pass. Returns a summary of what changed. Each phase is a
    foundation primitive; the watchdog only SCHEDULES them. Always beats its own
    liveness last so a crash between phases still leaves a recent heartbeat absent."""
    summary: dict = {}
    summary["reclaimed_compute"] = queue.reclaim_compute(conn, grace_seconds=cfg.reclaim_grace_seconds)
    summary["reclaimed_search"] = queue.reclaim_search(conn, grace_seconds=cfg.reclaim_grace_seconds)
    summary["reclaimed_apply"] = len(pgqueue.reclaim_stale_leases(conn, grace_seconds=cfg.reclaim_grace_seconds))

    # Order is load-bearing: clear timer-expired breakers FIRST (restore scopes whose
    # cooldown passed), THEN re-evaluate current conditions (which re-trips anything
    # still bad in the same tick). Reversing this makes evaluate_breakers recover quiet
    # expired scopes itself -> they'd surface in breakers_tripped, not breakers_recovered.
    summary["breakers_recovered"] = governor.clear_expired_breakers(conn)
    summary["breakers_tripped"] = governor.evaluate_breakers(
        conn, captcha_threshold=cfg.captcha_threshold, min_samples=cfg.breaker_min_samples,
        cool_seconds=cfg.breaker_cool_seconds,
    )

    summary["stuck_handled"] = _handle_stuck(conn, cfg)

    summary["paused_on_cap"] = _enforce_cap(conn)

    heartbeat.beat(conn, WATCHDOG_ID, role=WATCHDOG_ROLE, state="idle", spend_today_usd=0, commit=True)
    return summary


def _total_cap_breached(conn) -> bool:
    """True if a configured daily OR total cost cap is met/exceeded (mirrors
    queue._cost_cap_exceeded). A 0/NULL cap means 'no cap'."""
    with conn.cursor() as cur:
        cur.execute("SELECT cost_cap_daily_usd, cost_cap_total_usd FROM fleet_config WHERE id=1")
        cfg_row = cur.fetchone()
        if not cfg_row:
            return False
        daily = float(cfg_row["cost_cap_daily_usd"] or 0)
        total = float(cfg_row["cost_cap_total_usd"] or 0)
        if daily > 0:
            cur.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage WHERE ts >= now() - interval '24 hours'")
            if float(cur.fetchone()["s"]) >= daily:
                return True
        if total > 0:
            cur.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage")
            if float(cur.fetchone()["s"]) >= total:
                return True
    return False


def _enforce_cap(conn) -> bool:
    """If a cap is breached, set fleet_config.paused=true to make the halt explicit
    (surfaced to dashboard/monitor; leasing already self-halts on the cap).

    Return semantics: reflects WHETHER the cap IS breached, not whether this call
    flipped the row. An already-paused fleet that is still over-cap returns True;
    a fleet under-cap returns False. The UPDATE uses IS DISTINCT FROM TRUE so it's
    a no-op if paused is already true, but the return value is still True because
    the cap is still exceeded."""
    breached = _total_cap_breached(conn)
    if breached:
        with conn.cursor() as cur:
            cur.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1 AND paused IS DISTINCT FROM TRUE")
        conn.commit()
    return breached


def _handle_stuck(conn, cfg: WatchdogConfig) -> list[dict]:
    """Restart every stuck worker (and quarantine its job if it blew the job-max).
    NEVER acts on the watchdog's own reserved id."""
    out: list[dict] = []
    stuck = heartbeat.detect_stuck(conn, heartbeat_timeout=cfg.heartbeat_timeout,
                                   job_max_seconds=cfg.job_max_seconds)
    for s in stuck:
        wid = s["worker_id"]
        if wid == WATCHDOG_ID:
            continue
        actions = ["restart"]
        heartbeat.issue_command(conn, wid, "restart")
        if s["reason"] == "job_over_max":
            # the worker's current job has been running too long -> quarantine it so a
            # restart doesn't immediately re-lease the same poison job.
            with conn.cursor() as cur:
                cur.execute("SELECT current_job FROM worker_heartbeat WHERE worker_id=%s", (wid,))
                row = cur.fetchone()
            job = row["current_job"] if row else None
            if job:
                if heartbeat.quarantine_job(conn, job, worker=wid, reason="job_over_max",
                                            threshold=cfg.quarantine_threshold):
                    actions.append("quarantine")
        out.append({"worker_id": wid, "reason": s["reason"], "action": actions})
    return out
