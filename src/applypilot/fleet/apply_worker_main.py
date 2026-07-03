"""applypilot-fleet-apply: an OFFSITE apply worker for owner-controlled machines.
Wraps the proven launcher.run_job into an apply_fn and drives WorkerLoop(role='apply').
Respects the shared kill switch AND the Fleet Doctor's ATS-only pause via ats_should_halt
(H1); never leases through a pause/canary-pause. The LinkedIn lane uses plain should_halt so
a Doctor ATS pause can never halt it."""
from __future__ import annotations

import argparse
import logging
import os
import re
import signal
import threading
import time

logger = logging.getLogger("applypilot.fleet.apply_worker_main")

# --- graceful stop (SIGTERM) -------------------------------------------------
# The macOS launchd wrapper (run-worker-mac.sh) and `launchctl unload` send SIGTERM to
# restart the worker for a code update. Mid-apply death parks the job crash_unconfirmed
# ("may-have-submitted"), so instead: SIGTERM sets a flag, the CURRENT job finishes, and
# run_apply exits before the next lease. SIGINT (Ctrl+C) keeps default abort behavior.
_STOP_REQUESTED = threading.Event()


def request_stop(signum=None, frame=None) -> None:
    _STOP_REQUESTED.set()


def stop_requested() -> bool:
    return _STOP_REQUESTED.is_set()


def install_stop_handler() -> None:
    try:
        signal.signal(signal.SIGTERM, request_stop)
    except (ValueError, OSError):  # pragma: no cover - non-main thread / exotic platform
        pass


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


def classify_apply_channel(tab_urls) -> dict:
    """Record HOW a LinkedIn apply happened, from the browser tabs the agent ended on
    (ZERO LinkedIn scraping -- these tabs were already opened by the apply itself):
      easy_apply -> stayed on linkedin.com (the catastrophe-class on-LinkedIn submit)
      external   -> redirected to an off-LinkedIn ATS (first-party submit, ~no ban risk)
    Returns {'apply_channel': 'easy_apply'|'external'|None, 'apply_external_host': base-host|None}.
    None = no informative tab (can't tell -- record nothing rather than guess)."""
    from urllib.parse import urlparse
    hosts = []
    for u in tab_urls or []:
        p = urlparse(u or "")
        if p.scheme in ("http", "https") and p.hostname:
            h = p.hostname.lower()
            hosts.append(h[4:] if h.startswith("www.") else h)
    if not hosts:
        return {"apply_channel": None, "apply_external_host": None}
    external = [h for h in hosts if not h.endswith("linkedin.com")]
    if external:
        parts = external[0].split(".")
        base = ".".join(parts[-2:]) if len(parts) >= 2 else external[0]
        return {"apply_channel": "external", "apply_external_host": base}
    return {"apply_channel": "easy_apply", "apply_external_host": None}


def _cdp_page_urls(port: int) -> list[str]:
    """Best-effort read-only list of open page-tab URLs from Chrome's CDP /json endpoint."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8", "ignore"))
        return [t.get("url", "") for t in data if isinstance(t, dict) and t.get("type") == "page"]
    except Exception:
        return []


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
            out = {"run_status": status, "est_cost_usd": _real_cost(stats, model)}
            if status == "applied":
                # Record the apply channel from the STILL-OPEN tabs (the finally below kills
                # Chrome). Best-effort: never let recording break a confirmed apply.
                out.update(classify_apply_channel(_cdp_page_urls(port)))
            return out
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


def resolve_agent_timeout(conn, *, env_default=None) -> int:
    """Effective apply-agent wall-clock timeout for THIS worker. The FLEET DOCTOR may RAISE
    a bounded override (fleet_config.agent_timeout_override) when a host clusters timeouts; a
    worker PREFERS that override when set, else the env/default (APPLYPILOT_AGENT_TIMEOUT, 300).

    Read-only + best-effort: any DB error falls back to the env/default so a transient blip
    never changes the worker's bound. The override is the Doctor's ONLY conservative timeout
    lever -- it can only ever lengthen the timeout within a ceiling (see doctor._assert_conservative)."""
    default = env_default
    if default is None:
        try:
            default = int(os.environ.get("APPLYPILOT_AGENT_TIMEOUT") or 300)
        except (TypeError, ValueError):
            default = 300
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT agent_timeout_override FROM fleet_config WHERE id=1")
            row = cur.fetchone()
        try:
            conn.rollback()  # read-only
        except Exception:
            pass
        if row is not None:
            ov = row.get("agent_timeout_override") if hasattr(row, "get") else row["agent_timeout_override"]
            if ov is not None:
                # Defensive clamp: never let a bad override SHORTEN the timeout below the
                # default (the Doctor only ever raises it; this guards manual edits too).
                return max(int(default), int(ov))
    except Exception:
        pass
    return int(default)


def _apply_timeout_override(dsn=None, *, conn=None) -> None:
    """If the Doctor set fleet_config.agent_timeout_override, prefer it: assign the launcher's
    module-level AGENT_TIMEOUT_SECONDS (which it reads from APPLYPILOT_AGENT_TIMEOUT at import)
    to the resolved value. No override -> leave the env/default untouched.

    Called BOTH once at startup (with a ``dsn`` -- opens its own short-lived connection) AND on
    EVERY apply tick (with an already-open ``conn`` -- avoids a second connection per tick). The
    per-tick call is what makes a live Doctor timeout_bump actually take effect on a long-lived
    worker: launcher.run_job reads AGENT_TIMEOUT_SECONDS as a module global per-job, so the next
    job after a bump sees the raised value. The 'only reassign if changed' guard keeps it cheap
    and best-effort (a transient DB blip never changes the bound)."""
    try:
        from applypilot.apply import launcher

        def _set_from(c):
            eff = resolve_agent_timeout(c)
            if int(getattr(launcher, "AGENT_TIMEOUT_SECONDS", 0)) != int(eff):
                launcher.AGENT_TIMEOUT_SECONDS = int(eff)

        if conn is not None:
            _set_from(conn)
        else:
            from applypilot.apply import pgqueue
            with pgqueue.connect(dsn) as own:
                _set_from(own)
    except Exception:  # pragma: no cover - best-effort; never block the worker
        logger.debug("could not resolve agent_timeout_override; using env/default", exc_info=True)


def build_apply_loop(*, dsn, worker_id, home_ip, model="sonnet", agent="claude", machine_owner=None, slot=0):
    _setup_apply_env()
    from applypilot.apply import pgqueue
    from applypilot.fleet.worker import WorkerLoop
    # Prefer the Doctor's bounded agent_timeout_override when present (else env/default).
    _apply_timeout_override(dsn)
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
    while not _STOP_REQUESTED.is_set() and (max_iterations is None or it < max_iterations):
        it += 1
        try:
            with conn_factory() as conn:
                # Re-resolve the Doctor's agent_timeout_override on EVERY tick (not just at
                # startup): the Doctor sets the override mid-flight while this worker is already
                # running, so a startup-only read would never see a live timeout_bump. The
                # 'only reassign if changed' guard inside makes this cheap, and launcher reads
                # AGENT_TIMEOUT_SECONDS as a module global per-job, so the next job picks it up.
                _apply_timeout_override(conn=conn)
                # H1: the APPLY lane honors the Doctor's ATS-only pause (ats_paused) in addition
                # to the shared kill switch; ats_should_halt OR-s it in. The LinkedIn worker keeps
                # plain should_halt(), so a Doctor ATS pause never halts the LinkedIn lane.
                if pgqueue.ats_should_halt(conn):
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
    if _STOP_REQUESTED.is_set():
        logger.info("stop requested (SIGTERM); exiting after current job, before next lease")
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
    # AFTER build_apply_loop: the launcher import inside it installs its own SIGTERM
    # handler (launcher.py); ours must be the LAST writer or the drain kills mid-apply.
    install_stop_handler()
    run_apply(lambda: pgqueue.connect(args.dsn), loop)
    return 0
