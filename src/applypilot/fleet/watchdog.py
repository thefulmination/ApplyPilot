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

    heartbeat.beat(conn, WATCHDOG_ID, role=WATCHDOG_ROLE, state="idle", spend_today_usd=0, commit=True)
    return summary
