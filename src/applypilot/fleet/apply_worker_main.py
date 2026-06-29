"""applypilot-fleet-apply: an OFFSITE apply worker for owner-controlled machines.
Wraps the proven launcher.run_job into an apply_fn and drives WorkerLoop(role='apply').
Respects fleet_config.paused via should_halt; never leases through a pause/canary-pause."""
from __future__ import annotations

import argparse
import logging
import os
import re
import time

logger = logging.getLogger("applypilot.fleet.apply_worker_main")


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


def _chrome_slot(worker_id, override=None) -> int:
    """Integer Chrome slot (profile + CDP port + per-run log id) for this worker.

    Multiple apply workers on ONE machine MUST use distinct slots or their Chrome
    instances collide in a single shared browser. Auto-derived from the trailing digits
    of --worker-id (home-0 -> 0, home-1 -> 1), capped to 0-9; --chrome-slot overrides.
    A worker-id with no trailing number falls back to slot 0.
    """
    if override is not None:
        return int(override) % 10
    m = re.search(r"(\d+)\s*$", str(worker_id or ""))
    return (int(m.group(1)) % 10) if m else 0


def make_apply_fn(model: str, agent: str, slot: int = 0):
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
        # `slot` keys this worker's Chrome profile + CDP port + per-run logs, so multiple
        # workers on ONE machine (distinct slots) never collide in a shared browser.
        worker_id = slot
        port = BASE_CDP_PORT + worker_id
        proc = chrome.launch_chrome(worker_id)  # returns Popen; port is implicit BASE_CDP_PORT+slot
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


def make_log_tail_fn(slot: int, *, n_lines: int = 40):
    """Return a zero-arg callable yielding the LAST ~n_lines of THIS apply worker's rich
    log (the live agent transcript), or None. The launcher writes that file at
    ``config.LOG_DIR / f"worker-{slot}.log"`` (launcher.py:1141, keyed by the Chrome slot
    int). WorkerLoop scrubs + caps the returned text before it is shipped/stored, so this
    only has to read defensively: a missing file (worker hasn't applied yet) -> None, and
    ANY read error -> None (the heartbeat then ships the in-memory event ring instead)."""
    def _tail():
        try:
            from applypilot import config  # the module launcher.run_job writes through
            path = config.LOG_DIR / f"worker-{slot}.log"
            if not path.exists():
                return None
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
            return "".join(lines[-n_lines:]) if lines else None
        except Exception:
            return None
    return _tail


def build_apply_loop(*, dsn, worker_id, home_ip, model="sonnet", agent="claude", machine_owner=None, slot=0):
    _setup_apply_env()
    from applypilot.apply import pgqueue
    from applypilot.fleet.worker import WorkerLoop
    return WorkerLoop(lambda: pgqueue.connect(dsn), worker_id, home_ip=home_ip, role="apply",
                      apply_fn=make_apply_fn(model, agent, slot), machine_owner=machine_owner,
                      log_tail_fn=make_log_tail_fn(slot))


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
            logger.exception("apply tick failed; backing off")
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
    p.add_argument("--chrome-slot", type=int, default=None,
                   help="Browser slot (Chrome profile + CDP port + logs). Auto-derived from "
                        "--worker-id's trailing digits; set explicitly (0,1,2,...) to run "
                        "multiple workers on ONE machine without browser collisions.")
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")
    slot = _chrome_slot(args.worker_id, args.chrome_slot)
    from applypilot.apply import pgqueue
    loop = build_apply_loop(dsn=args.dsn, worker_id=args.worker_id, home_ip=args.home_ip,
                            model=args.model, agent=args.agent, machine_owner=args.machine_owner,
                            slot=slot)
    run_apply(lambda: pgqueue.connect(args.dsn), loop)
    return 0
