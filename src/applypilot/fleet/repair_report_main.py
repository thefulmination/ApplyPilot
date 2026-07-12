"""CLI for the read-only fleet repair report."""
from __future__ import annotations

import argparse
import json
import os
import sys

from applypilot.apply import pgqueue
from applypilot.fleet import email_reconcile, repair_report, schema as fleet_schema


def _default_home_db() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "ApplyPilot", "applypilot.db")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="applypilot-fleet-repair-report")
    parser.add_argument("--dsn", default=None, help="Fleet Postgres DSN (default: env).")
    parser.add_argument("--home-db", default=_default_home_db(), help="Home brain SQLite path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    parser.add_argument("--min-score", type=float, default=email_reconcile.MIN_STRONG)
    parser.add_argument("--sample-limit", type=int, default=5)
    parser.add_argument("--overbroad-limit", type=int, default=10)
    args = parser.parse_args(argv)

    with pgqueue.connect(args.dsn) as conn:
        fleet_schema.ensure_schema_v3(conn)
        report = repair_report.build_report(
            conn,
            home_db_path=args.home_db,
            min_score=args.min_score,
            sample_limit=args.sample_limit,
            overbroad_limit=args.overbroad_limit,
        )
    if args.json:
        print(json.dumps(report, sort_keys=True, default=str))
    else:
        print(repair_report.format_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
