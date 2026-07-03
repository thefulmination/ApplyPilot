"""PURE fleet dead-man detector -- read-only watcher for the autonomous apply lane.

The owner removed the lifetime spend cap; the rolling ``cost_cap_daily_usd`` is the
only remaining throttle. This module is the safety net that notices when the fleet
goes silent, its queue stalls, its self-healer (Doctor/Watchdog) itself dies, or it
starts running hot against the daily cap.

``deadman_check`` is a PURE function: it issues SELECT-only queries against the given
connection, never writes/commits, and never calls ``datetime.now()`` -- the caller
(Task 2's persistent-loop wrapper) injects ``now`` so this module stays fully testable
and deterministic. The caller is also responsible for persisting ``new_hot_streak``
across invocations and passing it back in as ``prev_hot_streak``.
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Thresholds (module constants -- tune here, not inline).
# ---------------------------------------------------------------------------
STALE_MIN = 30          # silent_death / selfheal_dead: heartbeat staleness (minutes)
STALL_HOURS = 3         # stalled_queue: how long without an 'applied' row counts as stalled
HOT_FRACTION = 0.95     # running_hot: fraction of cost_cap_daily_usd that counts as "hot"
HOT_STREAK_MIN = 2      # running_hot: consecutive hot checks required before alerting

_WATCHDOG_OR_LINKEDIN_RE = re.compile(r"watchdog|linkedin")
_WATCHDOG_RE = re.compile(r"watchdog")


@dataclass
class Alert:
    kind: str
    severity: str
    detail: str


def _fleet_config(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT paused, ats_paused, cost_cap_daily_usd FROM fleet_config WHERE id=1;"
        )
        row = cur.fetchone()
    return dict(row) if row else {"paused": True, "ats_paused": True, "cost_cap_daily_usd": 0}


def _is_armed(cfg: dict) -> bool:
    return cfg["paused"] is False and cfg["ats_paused"] is False


def _max_last_beat(conn, pattern_sql: str, negate: bool) -> dt.datetime | None:
    op = "!~" if negate else "~"
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT MAX(last_beat) AS max_beat FROM worker_heartbeat WHERE worker_id {op} %s;",
            (pattern_sql,),
        )
        row = cur.fetchone()
    return row["max_beat"] if row else None


def _check_silent_death(conn, cfg: dict, now: dt.datetime) -> Alert | None:
    if not _is_armed(cfg):
        return None
    max_beat = _max_last_beat(conn, "watchdog|linkedin", negate=True)
    stale_before = now - dt.timedelta(minutes=STALE_MIN)
    if max_beat is None or max_beat < stale_before:
        detail = (
            "no apply-worker heartbeat" if max_beat is None
            else f"last apply-worker heartbeat at {max_beat.isoformat()}"
        )
        return Alert(kind="silent_death", severity="critical", detail=detail)
    return None


def _check_stalled_queue(conn, cfg: dict, now: dt.datetime) -> Alert | None:
    if not _is_armed(cfg):
        return None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM apply_queue WHERE status='queued' "
            "AND approved_batch IS NOT NULL) AS has_backlog;"
        )
        has_backlog = cur.fetchone()["has_backlog"]
    if not has_backlog:
        return None
    stall_before = now - dt.timedelta(hours=STALL_HOURS)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXISTS(SELECT 1 FROM apply_queue WHERE status='applied' "
            "AND updated_at > %s) AS has_recent_apply;",
            (stall_before,),
        )
        has_recent_apply = cur.fetchone()["has_recent_apply"]
    if has_recent_apply:
        return None
    return Alert(
        kind="stalled_queue", severity="critical",
        detail=f"approved backlog queued but no 'applied' row in the last {STALL_HOURS}h",
    )


def _check_selfheal_dead(conn, now: dt.datetime) -> Alert | None:
    max_beat = _max_last_beat(conn, "watchdog", negate=False)
    stale_before = now - dt.timedelta(minutes=STALE_MIN)
    if max_beat is None or max_beat < stale_before:
        detail = (
            "no watchdog heartbeat" if max_beat is None
            else f"last watchdog heartbeat at {max_beat.isoformat()}"
        )
        return Alert(kind="selfheal_dead", severity="critical", detail=detail)
    return None


def _check_running_hot(
    conn, cfg: dict, now: dt.datetime, prev_hot_streak: int
) -> tuple[Alert | None, int]:
    daily_cap = cfg["cost_cap_daily_usd"]
    if not daily_cap or daily_cap <= 0:
        return None, 0
    window_start = now - dt.timedelta(hours=24)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS spend FROM llm_usage WHERE ts >= %s;",
            (window_start,),
        )
        spend = cur.fetchone()["spend"]
    spend = float(spend)
    daily_cap = float(daily_cap)
    if spend >= HOT_FRACTION * daily_cap:
        streak = prev_hot_streak + 1
        if streak >= HOT_STREAK_MIN:
            return Alert(
                kind="running_hot", severity="warning",
                detail=f"rolling-24h spend ${spend:.2f} >= {HOT_FRACTION:.0%} of "
                       f"${daily_cap:.2f} daily cap (streak={streak})",
            ), streak
        return None, streak
    return None, 0


def deadman_check(
    conn, *, now: dt.datetime, prev_hot_streak: int = 0
) -> tuple[list[Alert], int]:
    """Read-only dead-man check. Never writes/commits; ``now`` is always injected.

    Returns (alerts, new_hot_streak) -- the caller is responsible for persisting
    ``new_hot_streak`` and passing it back in as ``prev_hot_streak`` on the next call.
    """
    cfg = _fleet_config(conn)
    alerts: list[Alert] = []

    silent = _check_silent_death(conn, cfg, now)
    if silent:
        alerts.append(silent)

    stalled = _check_stalled_queue(conn, cfg, now)
    if stalled:
        alerts.append(stalled)

    selfheal = _check_selfheal_dead(conn, now)
    if selfheal:
        alerts.append(selfheal)

    hot_alert, new_streak = _check_running_hot(conn, cfg, now, prev_hot_streak)
    if hot_alert:
        alerts.append(hot_alert)

    return alerts, new_streak
