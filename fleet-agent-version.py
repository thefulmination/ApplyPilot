"""Report this checkout's software version and the fleet pin.

Prints one machine-readable line:
    OK|<current_sw_version>|<pinned_worker_version>|match|drift|unpinned

The PowerShell fleet agent uses this as an update guard. On any error it prints
``ERR|||<ExceptionType>`` so the caller can fail closed without killing workers.
"""
from __future__ import annotations

import os
from pathlib import Path

from fleet_agent_env import require_fleet_pg_dsn


def main() -> int:
    try:
        from applypilot.apply import pgqueue
        from applypilot.fleet.software_version import current_sw_version

        dsn = require_fleet_pg_dsn(os.environ)
        current = current_sw_version(repo=Path(__file__).resolve().parent)
        with pgqueue.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("SELECT public.fleet_worker_admission_snapshot() AS state")
            row = cur.fetchone()["state"] or {}
        pinned = row.get("pinned_worker_version") or ""
        state = "unpinned" if not pinned else ("match" if current == pinned else "drift")
        print(f"OK|{current}|{pinned}|{state}")
        return 0
    except Exception as exc:  # pragma: no cover - exercised by shell caller in production
        print(f"fleet-agent-version: {type(exc).__name__}: {exc}", file=__import__("sys").stderr)
        print(f"ERR|||{type(exc).__name__}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
