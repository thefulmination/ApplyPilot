"""applypilot-fleet-linkedin: the HOME-BOX LinkedIn apply worker.

Drives WorkerLoop(role='linkedin') against the LinkedIn queue. A single advisory
lock ('applypilot:linkedin_driver') ensures only one LinkedIn driver runs at a time
-- the owner box is the sole origin for LinkedIn applies; having two drivers would
race on session state and inflate the risk of an account ban.

The advisory lock is held via a DEDICATED long-lived connection for the life of the
process. Releasing the connection releases the lock -- so any crash or clean exit
automatically frees the lock for the supervised launcher (Task 7).
"""
from __future__ import annotations

import argparse
import logging
import os
from pathlib import Path
import sys
import time

logger = logging.getLogger("applypilot.fleet.linkedin_worker_main")


def _setup_apply_env() -> None:
    """Mirror apply_worker_main._setup_apply_env (home-box flavored)."""
    repo_app_dir = Path(__file__).resolve().parents[3] / ".applypilot"
    if (repo_app_dir / "profile.json").exists():
        os.environ.setdefault("APPLYPILOT_DIR", str(repo_app_dir))
        for env_name, filename in (
            ("APPLYPILOT_PROFILE_PATH", "profile.json"),
            ("APPLYPILOT_RESUME_PATH", "resume.txt"),
            ("APPLYPILOT_RESUME_PDF_PATH", "resume.pdf"),
            ("APPLYPILOT_RESUME_STRATEGY_PATH", "resume_strategy.yaml"),
            ("APPLYPILOT_PREFERENCE_PROFILE_PATH", "job_preference_profile.json"),
            ("APPLYPILOT_KNOWLEDGE_GRAPH_PROMPT_PATH", "job_knowledge_graph_prompt.md"),
        ):
            path = repo_app_dir / filename
            if path.exists():
                os.environ.setdefault(env_name, str(path))
    os.environ["APPLYPILOT_BASE_RESUME"] = "1"
    # Fleet row selection is lane-filtered at push time; keep worker-side acquire opt-in.
    os.environ.setdefault("APPLYPILOT_LANE_FILTER", "0")
    os.environ.setdefault("APPLYPILOT_DB_PATH", os.path.join(os.environ.get("TEMP", "/tmp"), "fleet_apply_throwaway.db"))
    os.environ.setdefault("CHROME_WORKER_DIR", os.path.join(os.environ.get("TEMP", "/tmp"), "chrome-workers"))
    os.environ.setdefault("APPLY_WORKER_DIR", os.path.join(os.environ.get("TEMP", "/tmp"), "apply-workers"))
    os.environ.setdefault("APPLYPILOT_AGENT_TIMEOUT", "300")

    # Some callers import pgqueue/config before building the LinkedIn loop. Refresh the
    # path constants so load_profile()/resume lookup still sees the env set above.
    cfg = sys.modules.get("applypilot.config")
    if cfg is not None:
        app_dir = Path(os.environ.get("APPLYPILOT_DIR", str(cfg.APP_DIR)))
        cfg.APP_DIR = app_dir
        cfg.DB_PATH = Path(os.environ.get("APPLYPILOT_DB_PATH", str(cfg.DB_PATH)))
        cfg.PROFILE_PATH = Path(os.environ.get("APPLYPILOT_PROFILE_PATH", str(app_dir / "profile.json")))
        cfg.RESUME_PATH = Path(os.environ.get("APPLYPILOT_RESUME_PATH", str(app_dir / "resume.txt")))
        cfg.RESUME_PDF_PATH = Path(os.environ.get("APPLYPILOT_RESUME_PDF_PATH", str(app_dir / "resume.pdf")))
        cfg.RESUME_STRATEGY_PATH = Path(
            os.environ.get("APPLYPILOT_RESUME_STRATEGY_PATH", str(app_dir / "resume_strategy.yaml"))
        )
        cfg.PREFERENCE_PROFILE_PATH = Path(
            os.environ.get("APPLYPILOT_PREFERENCE_PROFILE_PATH", str(app_dir / "job_preference_profile.json"))
        )
        cfg.KNOWLEDGE_GRAPH_PROMPT_PATH = Path(
            os.environ.get("APPLYPILOT_KNOWLEDGE_GRAPH_PROMPT_PATH", str(app_dir / "job_knowledge_graph_prompt.md"))
        )


def acquire_linkedin_interlock(conn) -> bool:
    """Try to acquire the advisory lock for the LinkedIn driver.

    Returns True if acquired (this process is the sole LinkedIn driver), False if
    another session already holds it. The lock is SESSION-level: it is released
    automatically when *conn* is closed, so the caller must keep *conn* open for
    the process lifetime.

    The key string is EXACTLY 'applypilot:linkedin_driver' -- byte-identical with
    the probe in the supervised launcher (Task 7).
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_lock(hashtext('applypilot:linkedin_driver')) AS ok")
        ok = cur.fetchone()["ok"]
    conn.commit()
    return bool(ok)


def build_linkedin_loop(*, dsn, worker_id, owner_ip, model="sonnet", agent="codex", machine_owner=None):
    """Construct a WorkerLoop for the linkedin role.

    public_ip and owner_ip are both set to *owner_ip*: the LinkedIn driver always
    runs on the owner's home box, so the residential egress IP IS the owner IP.
    apply_fn is built via the same make_apply_fn path used by apply_worker_main --
    run_job is URL-agnostic; the LinkedIn-seeded Chrome profile is what
    setup_worker_profile already prefers for li_at sessions. We do NOT force a
    fresh profile here.
    """
    _setup_apply_env()
    from applypilot.apply import pgqueue
    from applypilot.fleet.worker import WorkerLoop
    from applypilot.fleet.apply_worker_main import make_apply_fn

    return WorkerLoop(
        lambda: pgqueue.connect(dsn),
        worker_id,
        home_ip=owner_ip,
        role="linkedin",
        apply_fn=make_apply_fn(model, agent),
        machine_owner=machine_owner,
        public_ip=owner_ip,
        owner_ip=owner_ip,
        on_owner_machine=True,
    )


def run_linkedin(conn_factory, loop, *, max_iterations=None, idle_sleep=5.0) -> dict:
    """Drive the LinkedIn apply loop (mirrors run_apply from apply_worker_main).

    Before each iteration check the LinkedIn-specific shared kill switch and idle when
    halted. Lease-time gates still enforce the LinkedIn canary, account halt, daily cap,
    and mutex. A per-tick error backs off without crashing. Returns a counts dict
    (testable). Production calls with max_iterations=None (forever).
    """
    from applypilot.apply import pgqueue
    counts = {"applied": 0, "halted": 0, "idle": 0, "error": 0}
    it = 0
    while max_iterations is None or it < max_iterations:
        it += 1
        try:
            with conn_factory() as conn:
                if pgqueue.linkedin_should_halt(conn):
                    counts["halted"] += 1
                    if idle_sleep:
                        time.sleep(idle_sleep)
                    continue
            res = loop.run_once()
            action = res.get("action")
            if action == "applied":
                counts["applied"] += 1
            elif action == "stop":
                logger.info("remote %s command: exiting between jobs (supervisor respawns)",
                            res.get("command"))
                break
            elif action in ("idle", "paused", "usage_limit"):
                counts["idle"] += 1
                if idle_sleep:
                    time.sleep(idle_sleep)
        except Exception:  # pragma: no cover - logged, backed off, never fatal
            logger.exception("linkedin tick failed; backing off")
            counts["error"] += 1
            if idle_sleep:
                time.sleep(idle_sleep)
    return counts


def main(argv=None) -> int:  # pragma: no cover - long-running
    p = argparse.ArgumentParser(prog="applypilot-fleet-linkedin")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    p.add_argument("--worker-id", required=True)
    p.add_argument("--owner-ip", default=os.environ.get("FLEET_OWNER_IP", "0.0.0.0"))
    p.add_argument("--model", default="sonnet")
    # Match the apply-lane default: Codex uses the ChatGPT quota pool and avoids
    # burning the Claude Max subscription unless the operator explicitly opts in.
    p.add_argument("--agent", default="codex")
    p.add_argument("--machine-owner", default=os.environ.get("FLEET_MACHINE_OWNER"))
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")

    _setup_apply_env()
    from applypilot.apply import pgqueue

    # Open a DEDICATED long-lived connection solely for holding the advisory lock.
    # This connection is kept open for the process life; releasing it releases the lock.
    interlock_conn = pgqueue.connect(args.dsn)
    if not acquire_linkedin_interlock(interlock_conn):
        interlock_conn.close()
        raise SystemExit("another LinkedIn driver holds the interlock")

    logger.info("advisory lock acquired: applypilot:linkedin_driver")

    loop = build_linkedin_loop(
        dsn=args.dsn,
        worker_id=args.worker_id,
        owner_ip=args.owner_ip,
        model=args.model,
        agent=args.agent,
        machine_owner=args.machine_owner,
    )
    try:
        run_linkedin(lambda: pgqueue.connect(args.dsn), loop)
    finally:
        try:
            interlock_conn.close()  # releases the advisory lock
        except Exception:
            pass
    return 0
