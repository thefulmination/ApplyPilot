"""Operator CLI for reviewing application-answer exceptions."""
from __future__ import annotations

import argparse
import json

from applypilot.apply.answer_exceptions import approve_exception, list_exceptions
from applypilot.database import get_connection


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    listing = sub.add_parser("list")
    listing.add_argument("--all", action="store_true")
    approve = sub.add_parser("approve")
    approve.add_argument("id", type=int)
    approve.add_argument("answer")
    args = parser.parse_args()

    conn = get_connection()
    try:
        if args.command == "list":
            print(json.dumps(list_exceptions(conn, status=None if args.all else "pending"), indent=2))
            return
        approve_exception(conn, args.id, args.answer)
        print(json.dumps({"id": args.id, "status": "approved"}))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
