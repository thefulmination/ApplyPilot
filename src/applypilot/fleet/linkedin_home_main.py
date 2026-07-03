"""applypilot-fleet-linkedin-home: the owner driver for the LinkedIn apply lane.

Mirrors apply_home_main exactly but operates on linkedin_queue and the
linkedin_canary_* columns (NOT A's canary_enabled / canary_remaining).

Commands: push / approve [--all-pushed] / pull / linkedin-canary K /
          lift-linkedin-canary / challenges / resolve-challenge URL /
          clear-halt / kill / status.

The approve command REFUSES (SystemExit) unless the LinkedIn canary is armed --
so the runbook's arm-then-approve order can never invert.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import uuid

from applypilot.apply import pgqueue
from applypilot.fleet import queue, sync


# ---------------------------------------------------------------------------
# LinkedIn-specific canary helpers (touch linkedin_canary_* only, never A's columns)
# ---------------------------------------------------------------------------

def set_linkedin_canary(conn, k: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET linkedin_canary_enabled=TRUE, linkedin_canary_remaining=%s WHERE id=1",
            (k,),
        )
    conn.commit()


def lift_linkedin_canary(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET linkedin_canary_enabled=FALSE, linkedin_canary_remaining=NULL WHERE id=1")
    conn.commit()


def _linkedin_canary_armed(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT linkedin_canary_enabled FROM fleet_config WHERE id=1")
        row = cur.fetchone()
    return bool(row and row["linkedin_canary_enabled"])


# ---------------------------------------------------------------------------
# Owner-side operations on linkedin_queue
# ---------------------------------------------------------------------------

def approve(conn, *, urls=None, all_pushed: bool = False) -> str:
    """Stamp a fresh batch token on the given (or all queued-unapproved) LinkedIn rows.

    REFUSES unless the LinkedIn canary is armed (so the runbook's
    arm-then-approve order can't invert -- same gate as apply_home_main)."""
    if not _linkedin_canary_armed(conn):
        raise SystemExit(
            "refusing to approve: arm the LinkedIn canary first (linkedin-home linkedin-canary <K>)"
        )
    if all_pushed:
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM linkedin_queue WHERE status='queued' AND approved_batch IS NULL")
            urls = [r["url"] for r in cur.fetchall()]
    token = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    queue.approve_linkedin_jobs(conn, urls or [], token)
    return token


def resolve_challenge_cmd(conn, url: str, *, skip: bool) -> bool:
    return queue.resolve_linkedin_challenge(conn, url, requeue=not skip)


def list_challenges(conn) -> list:
    """List open auth challenges for LinkedIn URLs."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ac.id, ac.url, ac.worker_id, ac.kind, ac.route, ac.raised_at "
            "FROM auth_challenge ac "
            "JOIN linkedin_queue lq ON lq.url = ac.url "
            "WHERE ac.resolved_at IS NULL ORDER BY ac.raised_at DESC"
        )
        return [dict(r) for r in cur.fetchall()]


def print_challenges_grouped(conn) -> None:
    """Plain kind x host -> count table for the linkedin lane only, sourced from
    the SHARED queue.challenge_summary (same helper the console's build_challenges
    detail view and the apply-home CLI use, lane='linkedin' here)."""
    rows = queue.challenge_summary(conn, "linkedin")
    if not rows:
        print("no open/parked challenges")
        return
    for r in sorted(rows, key=lambda r: (r["kind"], r["host"])):
        print(f"{r['kind']}\t{r['host']}\t{r['count']}")


def _print_status(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) AS n FROM linkedin_queue GROUP BY status")
        depth = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute(
            "SELECT linkedin_canary_enabled, linkedin_canary_remaining, spend_cap_usd FROM fleet_config WHERE id=1"
        )
        cfg = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(est_cost_usd),0) AS s FROM linkedin_queue")
        spend = float(cur.fetchone()["s"])
        cur.execute("SELECT halted_until FROM rate_governor WHERE scope_key='account:linkedin'")
        halt_row = cur.fetchone()
        halted_until = str(halt_row["halted_until"]) if halt_row else None
        cur.execute(
            "SELECT count(*) AS n FROM auth_challenge ac "
            "JOIN linkedin_queue lq ON lq.url = ac.url WHERE ac.resolved_at IS NULL"
        )
        open_ch = cur.fetchone()["n"]
        # apply-time channel recorder: how applied jobs actually submitted (easy_apply vs external ATS)
        cur.execute("SELECT COALESCE(apply_channel, '(unrecorded)') AS ch, count(*) AS n "
                    "FROM linkedin_queue WHERE status='applied' GROUP BY ch")
        channels = {r["ch"]: r["n"] for r in cur.fetchall()}
    print({
        "queue": depth,
        "linkedin_canary_enabled": cfg["linkedin_canary_enabled"],
        "linkedin_canary_remaining": cfg["linkedin_canary_remaining"],
        "spend_cap_usd": float(cfg["spend_cap_usd"] or 0),
        "linkedin_spend": spend,
        "halted_until": halted_until,
        "open_challenges": open_ch,
        "apply_channels": channels,
    })


def main(argv=None) -> int:  # pragma: no cover - CLI wiring
    p = argparse.ArgumentParser(prog="applypilot-fleet-linkedin-home")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("push")
    sp.add_argument("--score-floor", type=int, default=7)
    sp.add_argument("--max-age-days", type=int, default=21,
                    help="only push LinkedIn jobs discovered within N days (liveness proxy -- "
                         "LinkedIn can't be network-probed; stale postings are likely dead). "
                         "Pass 0 to disable.")
    sp.add_argument("--limit", type=int, default=None)

    sub.add_parser("pull")

    lc = sub.add_parser("linkedin-canary")
    lc.add_argument("k", type=int)

    sub.add_parser("lift-linkedin-canary")

    ap = sub.add_parser("approve")
    ap.add_argument("--all-pushed", action="store_true")

    chp = sub.add_parser("challenges")
    chp.add_argument("--grouped", action="store_true")

    rc = sub.add_parser("resolve-challenge")
    rc.add_argument("url")
    rc.add_argument("--skip", action="store_true")

    sub.add_parser("clear-halt")
    sub.add_parser("kill")
    sub.add_parser("status")

    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")

    with pgqueue.connect(args.dsn) as conn:
        if args.cmd == "push":
            n = sync.push_linkedin_eligible(pg_conn=conn, score_floor=args.score_floor,
                                            max_age_days=args.max_age_days, limit=args.limit)
            print("pushed", n)
            unscored = sync.count_linkedin_unscored()
            if unscored:
                print(f"note: {unscored} apply-shaped LinkedIn jobs are UNSCORED and held out "
                      f"of the push -- run the scorer to fold them into the candidate pool")
        elif args.cmd == "pull":
            # Ingest terminal linkedin_queue results into the brain and stamp them
            # synced (idempotent; a confirmed apply is never demoted). This used to be
            # a report-only stub -- LinkedIn applies never reached the brain.
            print("pulled", sync.pull_linkedin_results(pg_conn=conn))
        elif args.cmd == "linkedin-canary":
            set_linkedin_canary(conn, args.k)
            print("linkedin canary armed", args.k)
        elif args.cmd == "lift-linkedin-canary":
            lift_linkedin_canary(conn)
            print("linkedin canary lifted")
        elif args.cmd == "approve":
            print("approved batch", approve(conn, all_pushed=args.all_pushed))
        elif args.cmd == "challenges":
            if args.grouped:
                print_challenges_grouped(conn)
            else:
                for c in list_challenges(conn):
                    print(c)
        elif args.cmd == "resolve-challenge":
            print("resolved", resolve_challenge_cmd(conn, args.url, skip=args.skip))
        elif args.cmd == "clear-halt":
            queue.clear_linkedin_halt(conn)
            print("halt cleared")
        elif args.cmd == "kill":
            queue.kill_linkedin(conn)
            print("linkedin killed (halt set to 100yr)")
        elif args.cmd == "status":
            _print_status(conn)

    return 0
