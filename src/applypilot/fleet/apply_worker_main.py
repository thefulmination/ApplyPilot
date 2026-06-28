"""applypilot-fleet-apply: an OFFSITE apply worker for owner-controlled machines.
Wraps the proven launcher.run_job into an apply_fn and drives WorkerLoop(role='apply').
Respects fleet_config.paused via should_halt; never leases through a pause/canary-pause."""
from __future__ import annotations

import argparse
import os
import time


def _setup_apply_env() -> None:
    """Point config at writable locations + base-resume BEFORE importing applypilot
    (ports container_worker._setup_env, home-box flavored). run_job records agent cost
    to a home SQLite; the fleet has none, so sink it to a throwaway DB and read the REAL
    cost from launcher._last_run_stats."""
    os.environ["APPLYPILOT_BASE_RESUME"] = "1"
    os.environ["APPLYPILOT_LANE_FILTER"] = "0"
    os.environ.setdefault("APPLYPILOT_DB_PATH", os.path.join(os.environ.get("TEMP", "/tmp"), "fleet_apply_throwaway.db"))
    os.environ.setdefault("CHROME_WORKER_DIR", os.path.join(os.environ.get("TEMP", "/tmp"), "chrome-workers"))
    os.environ.setdefault("APPLY_WORKER_DIR", os.path.join(os.environ.get("TEMP", "/tmp"), "apply-workers"))
    os.environ.setdefault("APPLYPILOT_AGENT_TIMEOUT", "300")


def make_apply_fn(model: str, agent: str):
    """Return apply_fn(job) -> {"run_status", "est_cost_usd"} wrapping launcher.run_job.
    Imports launcher LAZILY (after _setup_apply_env).

    Note on chrome API:
      - launch_chrome(worker_id, port=None, ...) -> subprocess.Popen
        (port defaults to BASE_CDP_PORT + worker_id; returns the process, NOT the port)
      - cleanup_worker(worker_id, process) -> None  (process is the Popen returned above)
    """
    from applypilot.apply import launcher, chrome
    from applypilot.apply.chrome import BASE_CDP_PORT
    from applypilot.apply.container_worker import _real_cost

    def apply_fn(job: dict) -> dict:
        worker_id = 0
        port = BASE_CDP_PORT + worker_id
        proc = chrome.launch_chrome(worker_id)  # returns Popen; port is implicit BASE_CDP_PORT+0
        try:
            status, _dur = launcher.run_job(job, port, worker_id, model=model, agent=agent)
            stats = (getattr(launcher, "_last_run_stats", {}) or {}).get(worker_id, {})
            return {"run_status": status, "est_cost_usd": _real_cost(stats, model)}
        finally:
            try:
                chrome.cleanup_worker(worker_id, proc)
            except Exception:
                pass
    return apply_fn


def build_apply_loop(*, dsn, worker_id, home_ip, model="sonnet", agent="claude", machine_owner=None):
    _setup_apply_env()
    from applypilot.apply import pgqueue
    from applypilot.fleet.worker import WorkerLoop
    return WorkerLoop(lambda: pgqueue.connect(dsn), worker_id, home_ip=home_ip, role="apply",
                      apply_fn=make_apply_fn(model, agent), machine_owner=machine_owner)


def run_apply(conn_factory, loop, *, max_iterations=None, idle_sleep=5.0) -> dict:
    """Drive the apply loop. Before each iteration check should_halt (paused/spend cap)
    and idle when halted. A per-tick error backs off (no hot crash loop). Returns a
    counts dict (testable). Production calls with max_iterations=None (forever)."""
    from applypilot.apply import pgqueue
    counts = {"applied": 0, "halted": 0, "idle": 0, "error": 0}
    it = 0
    while max_iterations is None or it < max_iterations:
        it += 1
        try:
            with conn_factory() as conn:
                if pgqueue.should_halt(conn):
                    counts["halted"] += 1
                    if idle_sleep:
                        time.sleep(idle_sleep)
                    continue
            res = loop.run_once()
            action = res.get("action")
            if action == "applied":
                counts["applied"] += 1
            elif action == "idle":
                counts["idle"] += 1
                if idle_sleep:
                    time.sleep(idle_sleep)
        except Exception:  # pragma: no cover - logged, backed off, never fatal
            counts["error"] += 1
            if idle_sleep:
                time.sleep(idle_sleep)
    return counts


def main(argv=None) -> int:  # pragma: no cover - long-running
    p = argparse.ArgumentParser(prog="applypilot-fleet-apply")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    p.add_argument("--worker-id", required=True)
    p.add_argument("--home-ip", default=os.environ.get("FLEET_HOME_IP", "0.0.0.0"))
    p.add_argument("--model", default="sonnet")
    p.add_argument("--agent", default="claude")
    p.add_argument("--machine-owner", default=os.environ.get("FLEET_MACHINE_OWNER"))
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")
    from applypilot.apply import pgqueue
    loop = build_apply_loop(dsn=args.dsn, worker_id=args.worker_id, home_ip=args.home_ip,
                            model=args.model, agent=args.agent, machine_owner=args.machine_owner)
    run_apply(lambda: pgqueue.connect(args.dsn), loop)
    return 0
