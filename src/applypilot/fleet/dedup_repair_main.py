"""CLI for narrow overbroad dedup-key repair."""
from __future__ import annotations

import argparse
import json
import sys

from applypilot.apply import pgqueue
from applypilot.fleet import dedup_repair


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="applypilot-fleet-dedup-repair")
    parser.add_argument("--dsn", default=None, help="Fleet Postgres DSN (default: env).")
    parser.add_argument("--dedup-key", required=True)
    parser.add_argument("--limit", type=int, default=25)
    parser.add_argument("--max-rows", type=int, default=25)
    parser.add_argument("--reason", default="source_specific_overbroad_key")
    parser.add_argument("--apply", action="store_true", help="Apply the repair. Dry-run by default.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args(argv)

    if args.limit <= 0 or args.max_rows < 0:
        print("error: --limit must be > 0 and --max-rows must be >= 0", file=sys.stderr)
        return 2

    with pgqueue.connect(args.dsn) as conn:
        if args.apply:
            result = dedup_repair.execute_repair(
                conn,
                dedup_key=args.dedup_key,
                max_rows=args.max_rows,
                reason=args.reason,
            )
        else:
            result = dedup_repair.plan_repair(conn, dedup_key=args.dedup_key, limit=args.limit)
            result["dry_run"] = True

    if args.json:
        print(json.dumps(result, sort_keys=True, default=str))
    else:
        print(f"dedup_key: {result.get('dedup_key')}")
        print(f"safe_to_apply: {result.get('safe_to_apply', result.get('updated', 0) > 0)}")
        if result.get("refused_reason"):
            print(f"refused_reason: {result['refused_reason']}")
        for candidate in result.get("candidates", []):
            print(f"{candidate['url']}: {candidate['old_dedup_key']} -> {candidate['new_dedup_key']}")
        if args.apply:
            print(f"updated: {result.get('updated', 0)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
