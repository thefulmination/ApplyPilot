"""CLI for historical applied-outcome re-audit."""
from __future__ import annotations

import argparse
import json
import os
import sys

from applypilot.apply import pgqueue
from applypilot.fleet import historical_apply_reaudit, schema as fleet_schema


def _default_home_db() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "ApplyPilot", "applypilot.db")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="applypilot-fleet-reaudit-applied")
    parser.add_argument("--dsn", default=None, help="Fleet Postgres DSN (default: env).")
    parser.add_argument("--home-db", default=_default_home_db(), help="Home brain SQLite path.")
    parser.add_argument("--apply", action="store_true", help="Write conservative corrections.")
    parser.add_argument(
        "--ssh-map",
        action="append",
        default=[],
        metavar="HOME_IP=SSH_HOST",
        help="Read logs for a worker address over SSH; repeat for each machine.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument(
        "--event-id",
        action="append",
        type=int,
        default=[],
        help="Audit only this result-event ID; repeat for multiple suspects.",
    )
    parser.add_argument(
        "--home-ip",
        action="append",
        default=[],
        help="Audit only events recorded from this worker address; repeat as needed.",
    )
    args = parser.parse_args(argv)
    remote_hosts = {}
    for value in args.ssh_map:
        if "=" not in value:
            parser.error(f"invalid --ssh-map {value!r}; expected HOME_IP=SSH_HOST")
        home_ip, ssh_host = value.split("=", 1)
        if not home_ip.strip() or not ssh_host.strip():
            parser.error(f"invalid --ssh-map {value!r}; expected HOME_IP=SSH_HOST")
        remote_hosts[home_ip.strip()] = ssh_host.strip()

    with pgqueue.connect(args.dsn) as conn:
        fleet_schema.ensure_schema_v3(conn)
        report = historical_apply_reaudit.reaudit_applied_outcomes(
            conn,
            home_db_path=args.home_db,
            remote_hosts=remote_hosts,
            event_ids=args.event_id or None,
            home_ips=args.home_ip or None,
            apply=args.apply,
        )
    if args.json:
        print(json.dumps(report, sort_keys=True, default=str))
    else:
        print(historical_apply_reaudit.format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
