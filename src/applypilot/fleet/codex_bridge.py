"""Codex monitoring bridge (spec 2026-06-27) -- a local FastMCP stdio server that
surfaces the fleet's read-only telemetry and three bounded-safe actions into Codex.

SAFETY: the guarantee is the tool REGISTRY -- exactly 8 functions are @mcp.tool().
Action tools delegate only to monitor.MonitorActions (whose surface is restart /
quarantine / pause, no apply/unpause/cap/challenge-resolve). We import only the
`connect` symbol from apply.pgqueue (not the module) so set_paused/set_spend_cap are
not even bound here. No DB access happens at import or in main(); every tool goes
through _with_conn, which reads FLEET_PG_DSN itself and rolls back + closes per call.
"""
from __future__ import annotations

import os
from typing import Any, Callable

from mcp.server.fastmcp import FastMCP

from applypilot.apply.pgqueue import connect
from applypilot.fleet import heartbeat, monitor

mcp = FastMCP("applypilot-fleet")


def _with_conn(fn: Callable[[Any], dict]) -> dict:
    """Open a short-lived dict_row connection from FLEET_PG_DSN, run fn(conn), and
    return its dict result (or a structured {"error": ...}). Rolls back (read-only
    discipline; no-op after an action's own commit) and closes in finally. Reads the
    DSN directly -- never connect() with no arg (that falls back to DATABASE_URL)."""
    dsn = os.environ.get("FLEET_PG_DSN")
    if not dsn:
        return {"error": "FLEET_PG_DSN is not set; set it in the Codex MCP env block"}
    try:
        conn = connect(dsn)
    except Exception as e:  # RuntimeError (no DSN) / OperationalError (dead DB) / etc.
        return {"error": f"could not connect: {e}"}
    try:
        return fn(conn)
    except Exception as e:
        return {"error": str(e)}
    finally:
        try:
            conn.rollback()
        except Exception:
            pass
        conn.close()


@mcp.tool()
def fleet_status() -> dict[str, Any]:
    """Fleet health rollup: machines, breaker states, queue depths, captcha backlog,
    quarantine count, 24h spend."""
    return _with_conn(lambda conn: heartbeat.dashboard_snapshot(conn))


def _read_caps(conn) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT paused, cost_cap_daily_usd, cost_cap_total_usd FROM fleet_config WHERE id=1")
        cfg = cur.fetchone() or {}
        cur.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage WHERE ts >= now() - interval '24 hours'")
        spend_today = float(cur.fetchone()["s"])
        cur.execute("SELECT COALESCE(SUM(cost_usd),0) AS s FROM llm_usage")
        spend_total = float(cur.fetchone()["s"])
    return {
        "paused": cfg.get("paused"),
        "cost_cap_daily_usd": float(cfg.get("cost_cap_daily_usd") or 0),
        "cost_cap_total_usd": float(cfg.get("cost_cap_total_usd") or 0),
        "spend_today": spend_today,
        "spend_total": spend_total,
    }


@mcp.tool()
def caps() -> dict[str, Any]:
    """Cost caps + spend: paused flag, daily/total caps, 24h spend, all-time spend."""
    return _with_conn(_read_caps)


@mcp.tool()
def health_report() -> dict[str, Any]:
    """The text health report (incl. a NEEDS YOUR DECISION anomaly section). The 24h
    spend is compared against the DAILY cap (apples-to-apples)."""
    def _build(conn):
        snap = heartbeat.dashboard_snapshot(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT cost_cap_daily_usd FROM fleet_config WHERE id=1")
            row = cur.fetchone()
        daily = float((row or {}).get("cost_cap_daily_usd") or 0)
        text = monitor.build_health_report(snap, captcha_threshold=0.4, cost_cap_total=daily)
        return {"report": text}
    return _with_conn(_build)


def main() -> int:  # pragma: no cover - stdio server loop, not unit-testable
    """Entry point: run the FastMCP server over stdio. No DB access here."""
    mcp.run()
    return 0
