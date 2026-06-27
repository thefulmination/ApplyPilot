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


def main() -> int:  # pragma: no cover - stdio server loop, not unit-testable
    """Entry point: run the FastMCP server over stdio. No DB access here."""
    mcp.run()
    return 0
