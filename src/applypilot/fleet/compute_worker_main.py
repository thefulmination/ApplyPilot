"""applypilot-fleet-compute: a compute worker (score + audit) for owner-controlled
machines. Reads PG DSN + LLM key/provider from the local env, loads the shared
context, and runs the WorkerLoop. Compute is IP-free (no browser, no site traffic)."""
from __future__ import annotations

import argparse
import os

from applypilot.apply import pgqueue
from applypilot.fleet import compute_context as cc
from applypilot.fleet.compute_adapters import make_audit_fn, make_score_fn
from applypilot.fleet.worker import WorkerLoop


def build_compute_loop(conn, *, dsn, worker_id, home_ip, providers, fallback, ensemble,
                       machine_owner=None) -> WorkerLoop:
    ctx, _version = cc.load_context(conn, providers=providers, fallback=fallback, ensemble=ensemble)
    fns = {"score": make_score_fn(ctx), "audit": make_audit_fn(ctx)}
    return WorkerLoop(lambda: pgqueue.connect(dsn), worker_id, home_ip=home_ip, role="compute",
                      compute_fns=fns, machine_owner=machine_owner)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-compute")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    p.add_argument("--worker-id", required=True)
    p.add_argument("--home-ip", default=os.environ.get("FLEET_HOME_IP", "0.0.0.0"))
    p.add_argument("--providers", default=os.environ.get("LLM_SCORE_PROVIDER", "deepseek"))
    p.add_argument("--fallback", default=os.environ.get("LLM_SCORE_FALLBACK", ""))
    p.add_argument("--ensemble", action="store_true", default=bool(os.environ.get("FLEET_ENSEMBLE")))
    p.add_argument("--machine-owner", default=os.environ.get("FLEET_MACHINE_OWNER"))
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")
    providers = [s for s in args.providers.split(",") if s]
    fallback = [s for s in args.fallback.split(",") if s]
    with pgqueue.connect(args.dsn) as conn:
        loop = build_compute_loop(conn, dsn=args.dsn, worker_id=args.worker_id, home_ip=args.home_ip,
                                  providers=providers, fallback=fallback, ensemble=args.ensemble,
                                  machine_owner=args.machine_owner)
    loop.run_forever()
    return 0
