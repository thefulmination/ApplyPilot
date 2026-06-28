"""applypilot-fleet-apply-home: the owner driver for the offsite apply lane.
push (stage UNAPPROVED + backfill applied_set), approve (arm a batch; refuse unless the
canary is armed), pull, canary/lift-canary, challenges + resolve-challenge, status."""
from __future__ import annotations

import argparse
import datetime as _dt
import os
import uuid

from applypilot.apply import pgqueue
from applypilot.fleet import queue, sync


def set_canary(conn, k: int) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET canary_enabled=TRUE, canary_remaining=%s, paused=FALSE WHERE id=1", (k,))
    conn.commit()


def lift_canary(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("UPDATE fleet_config SET canary_enabled=FALSE, canary_remaining=NULL WHERE id=1")
    conn.commit()


def _canary_armed(conn) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT canary_enabled FROM fleet_config WHERE id=1")
        row = cur.fetchone()
    return bool(row and row["canary_enabled"])


def approve(conn, *, urls=None, all_pushed=False) -> str:
    """Stamp a fresh batch token on the given (or all queued-unapproved) rows. REFUSES
    unless the canary is armed (so the runbook's arm-then-approve order can't invert)."""
    if not _canary_armed(conn):
        raise SystemExit("refusing to approve: arm the canary first (apply-home canary <K>)")
    if all_pushed:
        with conn.cursor() as cur:
            cur.execute("SELECT url FROM apply_queue WHERE status='queued' AND approved_batch IS NULL")
            urls = [r["url"] for r in cur.fetchall()]
    token = _dt.datetime.utcnow().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    queue.approve_jobs(conn, urls or [], token)
    return token


def resolve_challenge_cmd(conn, url: str, *, skip: bool) -> bool:
    return queue.resolve_challenge(conn, url, requeue=not skip)


def list_challenges(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT id, url, worker_id, kind, route, raised_at FROM auth_challenge "
                    "WHERE resolved_at IS NULL ORDER BY raised_at DESC")
        return [dict(r) for r in cur.fetchall()]


def main(argv=None) -> int:  # pragma: no cover - CLI wiring
    p = argparse.ArgumentParser(prog="applypilot-fleet-apply-home")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("push"); sp.add_argument("--score-floor", type=int, default=7); sp.add_argument("--limit", type=int, default=None)
    sub.add_parser("pull")
    ca = sub.add_parser("canary"); ca.add_argument("k", type=int)
    sub.add_parser("lift-canary")
    ap = sub.add_parser("approve"); ap.add_argument("--all-pushed", action="store_true")
    sub.add_parser("challenges")
    rc = sub.add_parser("resolve-challenge"); rc.add_argument("url"); rc.add_argument("--skip", action="store_true")
    sub.add_parser("status")
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")
    with pgqueue.connect(args.dsn) as conn:
        if args.cmd == "push":
            print("pushed", sync.push_apply_eligible(pg_conn=conn, score_floor=args.score_floor, limit=args.limit))
        elif args.cmd == "pull":
            print("pulled", sync.pull_apply_results(pg_conn=conn))
        elif args.cmd == "canary":
            set_canary(conn, args.k); print("canary armed", args.k)
        elif args.cmd == "lift-canary":
            lift_canary(conn); print("canary lifted")
        elif args.cmd == "approve":
            print("approved batch", approve(conn, all_pushed=args.all_pushed))
        elif args.cmd == "challenges":
            for c in list_challenges(conn): print(c)
        elif args.cmd == "resolve-challenge":
            print("resolved", resolve_challenge_cmd(conn, args.url, skip=args.skip))
        elif args.cmd == "status":
            _print_status(conn)
    return 0


def _print_status(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT status, count(*) AS n FROM apply_queue GROUP BY status")
        depth = {r["status"]: r["n"] for r in cur.fetchall()}
        cur.execute("SELECT paused, canary_enabled, canary_remaining, spend_cap_usd FROM fleet_config WHERE id=1")
        cfg = cur.fetchone()
        cur.execute("SELECT COALESCE(SUM(est_cost_usd),0) AS s FROM apply_queue")
        spend = float(cur.fetchone()["s"])
        cur.execute("SELECT count(*) AS n FROM auth_challenge WHERE resolved_at IS NULL")
        open_ch = cur.fetchone()["n"]
    print({"queue": depth, "paused": cfg["paused"], "canary_remaining": cfg["canary_remaining"],
           "spend_cap_usd": float(cfg["spend_cap_usd"] or 0), "apply_spend": spend, "open_challenges": open_ch})
