"""CLI for central ApplyPilot fleet machine blackout controls."""
from __future__ import annotations

import argparse
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

from applypilot.apply import pgqueue
from applypilot.fleet import machine_blackout

ET = ZoneInfo("America/New_York")


def parse_operator_time(raw: str) -> datetime:
    text = raw.strip()
    if text.lower().endswith(" america/new_york"):
        text = text[: -len(" america/new_york")].strip()
        tz = ET
    else:
        tz = ET
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"invalid --until value {raw!r}; use ISO time like 2026-07-06 17:00") from exc
    if value.tzinfo is None:
        value = value.replace(tzinfo=tz)
    return value


def _connect():
    conn = pgqueue.connect()
    from applypilot.fleet import schema

    schema.ensure_schema_v3(conn)
    return conn


def main(
    argv: list[str] | None = None,
    *,
    now_fn: Callable[[], datetime] | None = None,
) -> int:
    now_fn = now_fn or (lambda: datetime.now(ET))
    parser = argparse.ArgumentParser(prog="applypilot-fleet-control")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_blackout = sub.add_parser("blackout", help="Create an expiring machine blackout policy")
    p_blackout.add_argument("--until", required=True, help="Expiration time, interpreted as America/New_York if naive")
    p_blackout.add_argument("--name", default="operator-machine-blackout")
    p_blackout.add_argument("--allow", action="append")
    p_blackout.add_argument("--block", action="append")
    p_blackout.add_argument("--reason", default="")

    p_status = sub.add_parser("status", help="Show blackout status for a machine label")
    p_status.add_argument("--label", required=True)
    p_status.add_argument("--role", default="fleet")

    p_clear = sub.add_parser("clear", help="Clear active machine blackouts")
    p_clear.add_argument("--name")

    args = parser.parse_args(argv)
    with _connect() as conn:
        if args.cmd == "blackout":
            policy_id = machine_blackout.create_blackout(
                conn,
                name=args.name,
                expires_at=parse_operator_time(args.until),
                allow_patterns=args.allow or ["home", "mac", "mac-*"],
                block_patterns=args.block or ["*"],
                reason=args.reason,
                now=now_fn(),
            )
            print(f"created|{policy_id}|{args.name}")
            return 0
        if args.cmd == "status":
            print(machine_blackout.status_line(conn, args.label, role=args.role))
            return 0
        if args.cmd == "clear":
            print(f"cleared|{machine_blackout.clear_blackouts(conn, name=args.name)}")
            return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
