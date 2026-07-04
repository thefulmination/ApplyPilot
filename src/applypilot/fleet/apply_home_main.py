"""applypilot-fleet-apply-home: the owner driver for the offsite apply lane.
push (stage UNAPPROVED + backfill applied_set + push email-outcome summaries), approve
(arm a batch; refuse unless the canary is armed), pull, canary/lift-canary,
challenges + resolve-challenge, status."""
from __future__ import annotations

import argparse
import datetime as _dt
import logging
import os
import uuid

from applypilot import config
from applypilot.apply import pgqueue
from applypilot.fleet import queue, sync

logger = logging.getLogger("applypilot.fleet.apply_home_main")


def push_home(conn, *, sqlite_conn=None, score_floor: int = 7, limit: int | None = None) -> int:
    """The home 'push' cadence: stage apply-eligible jobs (+ backfill applied_set,
    inside push_apply_eligible), and best-effort push the brain's email_events outcome
    summaries into PG inbox_outcomes (R8 feedback loop). The outcomes push is advisory
    reporting only -- a transient/UndefinedTable failure must never block staging the
    apply queue, so it is logged and swallowed rather than raised."""
    pushed = sync.push_apply_eligible(sqlite_conn=sqlite_conn, pg_conn=conn,
                                       score_floor=score_floor, limit=limit)
    try:
        sync.push_inbox_outcomes(sqlite_conn=sqlite_conn, pg_conn=conn)
    except Exception:
        logger.warning("push_inbox_outcomes failed (best-effort, non-fatal)", exc_info=True)
    return pushed


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
    token = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    queue.approve_jobs(conn, urls or [], token)
    return token


def arm_canary_if_safe(conn, k: int) -> bool:
    """Guarded ApplyCycle arm: re-open canary leasing only when safety flags allow it."""
    if queue._cost_cap_exceeded(conn):
        return False
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config "
            "SET canary_enabled=TRUE, canary_remaining=%s, paused=FALSE, updated_at=now() "
            "WHERE id=1 AND ats_paused=FALSE",
            (k,),
        )
        n = cur.rowcount
    conn.commit()
    return n > 0


def resume_if_safe(conn) -> bool:
    """Guarded self-resume: clears ONLY a plain `paused` flag so the autonomous
    ApplyCycle can self-resume after a cap window frees capacity. SAFETY-CRITICAL --
    must NEVER override a Doctor/LinkedIn safety pause (ats_paused) and must never
    resume into an exceeded cost cap.

    The `AND ats_paused=FALSE` in the WHERE clause is the catastrophe guard: it is
    mandatory so a Doctor safety pause is never overridden by this self-resume path.
    """
    if queue._cost_cap_exceeded(conn):
        return False
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET paused=FALSE, updated_at=now() "
            "WHERE id=1 AND paused=TRUE AND ats_paused=FALSE"
        )
        n = cur.rowcount
    conn.commit()
    return n > 0


def resolve_challenge_cmd(conn, url: str, *, skip: bool) -> bool:
    return queue.resolve_challenge(conn, url, requeue=not skip)


def list_challenges(conn) -> list:
    with conn.cursor() as cur:
        cur.execute("SELECT id, url, worker_id, kind, route, raised_at FROM auth_challenge "
                    "WHERE resolved_at IS NULL ORDER BY raised_at DESC")
        return [dict(r) for r in cur.fetchall()]


def print_challenges_grouped(conn) -> None:
    """Plain kind x host -> count table for the apply lane only, sourced from the
    SHARED queue.challenge_summary (same helper the console's build_challenges
    detail view and the linkedin-home CLI use, lane='apply' here)."""
    rows = queue.challenge_summary(conn, "apply")
    if not rows:
        print("no open/parked challenges")
        return
    for r in sorted(rows, key=lambda r: (r["kind"], r["host"])):
        print(f"{r['kind']}\t{r['host']}\t{r['count']}")


def _blocklist_match_sql(*, pg: bool) -> tuple[str, dict | list]:
    names, patterns = config.load_blocked_companies()
    if pg:
        return (
            "(LOWER(TRIM(COALESCE(company,''))) = ANY(%(blocked_names)s) "
            "OR url ILIKE ANY(%(blocked_pats)s) "
            "OR COALESCE(application_url,'') ILIKE ANY(%(blocked_pats)s))",
            {"blocked_names": list(names), "blocked_pats": patterns},
        )
    clauses: list[str] = []
    params: list[str] = []
    if names:
        placeholders = ",".join("?" * len(names))
        clauses.append(f"LOWER(TRIM(COALESCE(company,''))) IN ({placeholders})")
        params.extend(sorted(names))
    for pattern in patterns:
        clauses.append("url LIKE ?")
        clauses.append("COALESCE(application_url,'') LIKE ?")
        params.extend([pattern, pattern])
    if not clauses:
        return "0", []
    return "(" + " OR ".join(clauses) + ")", params


def blocklist_backfill(*, sqlite_conn=None, pg_conn=None, execute: bool = False) -> dict[str, int]:
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or sync._home_conn()
    pg = pg_conn or pgqueue.connect()
    counts: dict[str, int] = {}
    brain_match, brain_params = _blocklist_match_sql(pg=False)
    pg_match, pg_params = _blocklist_match_sql(pg=True)
    try:
        brain_where = (
            f"{brain_match} "
            "AND COALESCE(apply_status,'') NOT IN ('applied','in_progress','crash_unconfirmed')"
        )
        counts["brain_matches"] = sq.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE " + brain_where,
            brain_params,
        ).fetchone()["n"]
        with pg.cursor() as cur:
            for table in ("apply_queue", "linkedin_queue"):
                key = f"{table}_matches"
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {table} WHERE status='queued' AND {pg_match}",
                    pg_params,
                )
                counts[key] = cur.fetchone()["n"]
        if not execute:
            pg.rollback()
            return counts

        cur = sq.execute(
            "UPDATE jobs SET apply_status='blocked', apply_error='company_blocklist' "
            "WHERE " + brain_where,
            brain_params,
        )
        counts["brain_blocked"] = cur.rowcount
        sq.commit()
        with pg.cursor() as cur:
            for table in ("apply_queue", "linkedin_queue"):
                cur.execute(
                    f"UPDATE {table} SET status='blocked', apply_error='company_blocklist', updated_at=now() "
                    f"WHERE status='queued' AND {pg_match}",
                    pg_params,
                )
                counts[f"{table}_blocked"] = cur.rowcount
        pg.commit()
        return counts
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


def main(argv=None) -> int:  # pragma: no cover - CLI wiring
    p = argparse.ArgumentParser(prog="applypilot-fleet-apply-home")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("push"); sp.add_argument("--score-floor", type=int, default=7); sp.add_argument("--limit", type=int, default=None)
    sub.add_parser("pull")
    ca = sub.add_parser("canary"); ca.add_argument("k", type=int)
    sub.add_parser("lift-canary")
    ac = sub.add_parser("arm-canary-if-safe"); ac.add_argument("k", type=int)
    ap = sub.add_parser("approve"); ap.add_argument("--all-pushed", action="store_true")
    chp = sub.add_parser("challenges"); chp.add_argument("--grouped", action="store_true")
    rc = sub.add_parser("resolve-challenge"); rc.add_argument("url"); rc.add_argument("--skip", action="store_true")
    sub.add_parser("status")
    sub.add_parser("resume-if-safe")
    bf = sub.add_parser("blocklist-backfill")
    mode = bf.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--execute", action="store_true")
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")
    with pgqueue.connect(args.dsn) as conn:
        if args.cmd == "push":
            print("pushed", push_home(conn, score_floor=args.score_floor, limit=args.limit))
        elif args.cmd == "pull":
            print("pulled", sync.pull_apply_results(pg_conn=conn))
        elif args.cmd == "canary":
            set_canary(conn, args.k); print("canary armed", args.k)
        elif args.cmd == "lift-canary":
            lift_canary(conn); print("canary lifted")
        elif args.cmd == "arm-canary-if-safe":
            if arm_canary_if_safe(conn, args.k):
                print("canary armed", args.k)
            else:
                print("left-disarmed (ats_paused or cost cap exceeded)")
                return 2
        elif args.cmd == "approve":
            print("approved batch", approve(conn, all_pushed=args.all_pushed))
        elif args.cmd == "challenges":
            if args.grouped:
                print_challenges_grouped(conn)
            else:
                for c in list_challenges(conn): print(c)
        elif args.cmd == "resolve-challenge":
            print("resolved", resolve_challenge_cmd(conn, args.url, skip=args.skip))
        elif args.cmd == "status":
            _print_status(conn)
        elif args.cmd == "resume-if-safe":
            if queue._cost_cap_exceeded(conn):
                print("left-paused (cap exceeded)")
            elif resume_if_safe(conn):
                print("resumed")
            else:
                print("left-paused (ats_paused or already running)")
        elif args.cmd == "blocklist-backfill":
            print(blocklist_backfill(pg_conn=conn, execute=bool(args.execute)))
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
