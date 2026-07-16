"""Controller-owned CLI for explicit ApplyPilot fleet schema migration."""
from __future__ import annotations

import argparse
import sys

from applypilot.apply import pgqueue
from applypilot.fleet import schema as fleet_schema


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="applypilot-fleet-migrate-schema")
    parser.add_argument(
        "--dsn",
        default=None,
        help="Fleet owner Postgres DSN (default: FLEET_PG_DSN or APPLYPILOT_FLEET_DSN).",
    )
    args = parser.parse_args(argv)

    try:
        dsn = pgqueue.get_dsn(args.dsn)
        with pgqueue.connect(dsn) as conn:
            fleet_schema.ensure_schema_v3(conn)
    except Exception as exc:
        print(f"fleet schema migration failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print("fleet schema v3 migration complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
