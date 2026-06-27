"""applypilot-fleet-compute-home: fill the compute_queue from the brain backlog
(score/audit) and pull advisory results back. Runs on the home box."""
from __future__ import annotations

import argparse

from applypilot.fleet import sync


def push_backlog(*, sqlite_conn=None, pg_conn=None, task="score", score_floor=7, limit=None) -> int:
    return sync.push_compute_eligible(sqlite_conn=sqlite_conn, pg_conn=pg_conn,
                                      task=task, score_floor=score_floor, limit=limit)


def pull_results(*, sqlite_conn=None, pg_conn=None) -> int:
    return sync.pull_compute_results(sqlite_conn=sqlite_conn, pg_conn=pg_conn)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-compute-home")
    p.add_argument("cmd", choices=["push", "pull"])
    p.add_argument("--task", default="score")
    p.add_argument("--score-floor", type=int, default=7)
    p.add_argument("--limit", type=int, default=None)
    args = p.parse_args(argv)
    if args.cmd == "push":
        print("pushed", push_backlog(task=args.task, score_floor=args.score_floor, limit=args.limit))
    else:
        print("pulled", pull_results())
    return 0
