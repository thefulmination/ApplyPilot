"""Machine-local work-hours blackout rules for browser apply workers."""

from __future__ import annotations

import os
import sys
from datetime import datetime, time
from zoneinfo import ZoneInfo

WORK_HOURS_TZ = ZoneInfo("America/New_York")
WORK_HOURS_START = time(8, 0)
WORK_HOURS_END = time(17, 0)
WORK_HOURS_DAYS = {0, 1, 2, 3, 4}
WORK_HOURS_LABELS = {"m4"}
ALLOW_ENV = "APPLYPILOT_ALLOW_WORK_HOURS_APPLY"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "y", "on", "allow"}


def apply_blackout_active(label: str, *, now: datetime | None = None, allow_override: bool = False) -> bool:
    """Return True when this machine should not run browser apply workers."""
    if allow_override:
        return False
    if label.strip().lower() not in WORK_HOURS_LABELS:
        return False

    current = now or datetime.now(WORK_HOURS_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=WORK_HOURS_TZ)
    current = current.astimezone(WORK_HOURS_TZ)
    return (
        current.weekday() in WORK_HOURS_DAYS
        and WORK_HOURS_START <= current.time() < WORK_HOURS_END
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    label = args[0] if args else os.environ.get("APPLYPILOT_FLEET_LABEL", "")
    override = _truthy(os.environ.get(ALLOW_ENV))
    active = apply_blackout_active(label, allow_override=override)
    now = datetime.now(WORK_HOURS_TZ).isoformat(timespec="seconds")
    if active:
        print(f"BLACKOUT|{label}|{now}|weekday 08:00-17:00 America/New_York")
    else:
        print(f"OK|{label}|{now}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
