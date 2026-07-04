"""applypilot-fleet-compute-home: fill the compute_queue from the brain backlog
(score/audit) and pull advisory results back. Runs on the home box."""
from __future__ import annotations

import argparse

from applypilot.fleet import sync


def push_backlog(*, sqlite_conn=None, pg_conn=None, task="score", score_floor=7, limit=None) -> int:
    # score_floor=7 here is intentional: the home driver applies a quality floor
    # so only jobs worth a second LLM pass are queued for compute.  The underlying
    # sync.push_compute_eligible defaults to score_floor=0 (score everything) — that
    # default is intentional for the raw sync function, which callers may use with
    # their own floor.  Do not change sync.py to match this default.
    return sync.push_compute_eligible(sqlite_conn=sqlite_conn, pg_conn=pg_conn,
                                      task=task, score_floor=score_floor, limit=limit)


def pull_results(*, sqlite_conn=None, pg_conn=None) -> int:
    return sync.pull_compute_results(sqlite_conn=sqlite_conn, pg_conn=pg_conn)


def reopen_results(*, pg_conn=None) -> int:
    return sync.reopen_compute_results(pg_conn=pg_conn)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-compute-home")
    p.add_argument("cmd", choices=["push", "pull", "reopen"])
    p.add_argument("--task", default="score")
    p.add_argument("--score-floor", type=int, default=7)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)
    if args.cmd == "push":
        print("pushed", push_backlog(task=args.task, score_floor=args.score_floor, limit=args.limit))
    elif args.cmd == "pull":
        print("pulled", pull_results())
    else:
        print("reopened", reopen_results())
    return 0
