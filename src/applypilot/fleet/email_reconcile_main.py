"""applypilot-fleet-reconcile-email: match crash_unconfirmed apply jobs to outcome emails and
(with --apply) flip confirmed ones to 'applied'. Dry-run by default. Home-side: needs the home
brain (read-only) and the fleet Postgres. ADVISORY unless --apply; never re-applies a job."""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys

from applypilot.apply import pgqueue
from applypilot.fleet import email_reconcile as er


def _default_home_db() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "ApplyPilot", "applypilot.db")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-reconcile-email")
    p.add_argument("--dsn", default=None, help="Fleet Postgres DSN (default: env).")
    p.add_argument("--home-db", default=_default_home_db(), help="Home brain SQLite path.")
    p.add_argument("--scan-days", type=int, default=7, help="Gmail look-back for the Phase-0 scan.")
    p.add_argument("--no-scan", action="store_true", help="Skip the Phase-0 Gmail scan.")
    p.add_argument("--apply", action="store_true", help="Flip CONFIRMED matches to applied.")
    p.add_argument("--apply-probable", action="store_true", help="Also flip probable matches.")
    p.add_argument("--min-score", type=float, default=er.MIN_STRONG, help="Fuzzy confirm threshold.")
    args = p.parse_args(argv)

    if not args.no_scan:
        try:
            from applypilot.outcome_scan import scan_outcomes
            counts = scan_outcomes(days=args.scan_days)
            print(f"phase0 scan: {counts}")
        except Exception as exc:  # best-effort enrichment; reconcile still runs on existing data
            print(f"phase0 scan skipped ({type(exc).__name__}: {exc}); using existing email_events")

    try:
        home = sqlite3.connect(f"file:{args.home_db}?mode=ro", uri=True)
        try:
            emails = er.load_outcome_emails(home)
        finally:
            home.close()
    except sqlite3.OperationalError as exc:
        print(f"no outcome data: cannot open home brain at {args.home_db} ({exc})")
        return 0

    with pgqueue.connect(args.dsn) as conn:
        jobs = er.load_crash_jobs(conn)
        result = er.reconcile(emails, jobs, min_strong=args.min_score)
        print(er.format_report(result))
        if args.apply:
            counts = er.apply_resolutions(conn, result, include_probable=args.apply_probable)
            print(f"applied: {counts}")
        elif args.apply_probable:
            print("(dry-run; --apply-probable requires --apply to write)")
        else:
            print("(dry-run; pass --apply to flip confirmed matches)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
