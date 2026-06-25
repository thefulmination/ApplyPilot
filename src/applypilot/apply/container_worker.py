"""Cloud apply-fleet worker (runs in a Railway Linux container).

Loop: check spend cap -> lease the top offsite job from Postgres (SKIP LOCKED) -> launch
headless Chromium -> run the EXISTING apply agent (DeepSeek V3 via the in-container LiteLLM
proxy) -> write the result + real cost back to Postgres -> repeat.

Reuses the home apply path verbatim (`run_job` + `build_prompt` + the Playwright MCP) with
container-local paths and a throwaway SQLite for run_job's cost-record; the REAL per-job cost
is read from `launcher._last_run_stats` and written to Postgres (drives the $200 spend cap).

Offsite-only: no LinkedIn, no cookies, no profile cloning. The agent reads each job's
description from the live page, so the ~6-column queue row is enough to build the prompt.

Env (set by the Dockerfile / Railway):
  DATABASE_URL                 Postgres (Railway-injected)
  DEEPSEEK_API_KEY             metered key (the proxy uses it; sealed in Railway)
  APPLYPILOT_WORKER_ID         0..N (one per replica; default 0)
  APPLYPILOT_APPLY_MODEL       deepseek-chat (V3, the chosen agent model)
  ANTHROPIC_BASE_URL           http://127.0.0.1:4000 (the in-container proxy)
  APPLYPILOT_DIR               /data/applypilot (volume: profile.json + resume.pdf)
"""
from __future__ import annotations

import os
import signal
import sys
import time


def _log(msg: str) -> None:
    print(f"[fleet-worker] {msg}", flush=True)


def _setup_env() -> None:
    """Point config at container-local, writable locations BEFORE importing applypilot."""
    os.environ.setdefault("APPLYPILOT_DIR", "/data/applypilot")          # volume: profile + resume
    os.environ["APPLYPILOT_BASE_RESUME"] = "1"                            # no per-job tailoring
    os.environ.setdefault("CHROME_WORKER_DIR", "/tmp/chrome-workers")
    os.environ.setdefault("APPLY_WORKER_DIR", "/tmp/apply-workers")
    # run_job records the agent cost to the home SQLite; the fleet has none, so sink it to a
    # throwaway file (we read the REAL cost from launcher._last_run_stats and write it to PG).
    os.environ.setdefault("APPLYPILOT_DB_PATH", "/tmp/fleet_throwaway.db")
    os.environ.setdefault("ANTHROPIC_BASE_URL", "http://127.0.0.1:4000")  # in-container proxy
    os.environ.setdefault("ANTHROPIC_AUTH_TOKEN", "sk-litellm")
    os.environ.pop("ANTHROPIC_API_KEY", None)                            # force the proxy path
    os.environ.setdefault("CLAUDE_PATH", "/usr/local/bin/claude")
    os.environ["PYTHONUTF8"] = "1"
    os.environ["APPLYPILOT_PREFLIGHT_LIVENESS"] = "0"                     # queue is pre-filtered
    os.environ["APPLYPILOT_LANE_FILTER"] = "0"                           # offsite-only already


_STOP = {"flag": False}


def _on_term(signum, frame):  # graceful drain: finish the current job, then exit
    _STOP["flag"] = True
    _log(f"signal {signum} received -- will exit after the current job")


def _job_dict(row: dict) -> dict:
    """Build the run_job job dict from a thin queue row (defaults for home-only fields)."""
    score = row.get("score")
    return {
        "url": row["url"],
        "title": row.get("title") or "this role",
        "company": row.get("company"),
        "site": row.get("company") or "",
        "application_url": row["application_url"],
        "audit_score": score,
        "fit_score": int(score) if score is not None else None,
        "tailored_resume_path": None,
        "cover_letter_path": None,
        "full_description": "",
    }


def _map_status(status: str) -> tuple[str, str | None]:
    """Map run_job's status string -> (apply_queue status, apply_error).

    SAFETY: an agent that RAN but produced no clean result (no_result_line/timeout/worker
    error) MAY have clicked submit -> park crash_unconfirmed, NEVER re-leased. A confirmed
    'expired' or a wall (captcha/login/auth) provably did not submit -> failed/blocked.
    """
    s = status or ""
    if s == "applied":
        return "applied", None
    if s in ("captcha", "login_issue", "auth_required"):
        return "blocked", s
    if s == "expired":
        return "failed", "expired"
    if s in ("failed:no_result_line", "failed:timeout") or s.startswith("failed:worker_error"):
        return "crash_unconfirmed", s.split(":", 1)[-1][:200]
    if s.startswith("failed:"):
        return "failed", s.split("failed:", 1)[1][:200]
    if s == "dry_run":
        return "failed", "dry_run_in_production"
    return "failed", (s[:200] or "unknown")


def main() -> int:
    _setup_env()
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    from applypilot.apply import launcher, pgqueue
    from applypilot.apply.chrome import launch_chrome, cleanup_worker

    worker_id = int(os.environ.get("APPLYPILOT_WORKER_ID", "0"))
    model = os.environ.get("APPLYPILOT_APPLY_MODEL", "deepseek-chat")
    agent = os.environ.get("APPLYPILOT_APPLY_AGENT", "claude")
    poll = float(os.environ.get("APPLYPILOT_POLL_SECONDS", "15"))
    idle_exit = float(os.environ.get("APPLYPILOT_IDLE_EXIT_SECONDS", "300"))
    lease_ttl = int(os.environ.get("APPLYPILOT_LEASE_TTL", "1500"))
    port = launcher.BASE_CDP_PORT + worker_id

    pg = pgqueue.connect()
    pgqueue.ensure_schema(pg)                          # idempotent; safe under concurrency
    reclaimed = pgqueue.reclaim_stale_leases(pg)       # startup crash sweep
    _log(f"worker {worker_id} up | model={model} agent={agent} | reclaimed {len(reclaimed)} stale leases")

    idle_seconds = 0.0
    applied = failed = 0
    while not _STOP["flag"]:
        if pgqueue.should_halt(pg):
            _log("HALT: paused or spend cap reached -- exiting"); break
        row = pgqueue.lease_one(pg, worker_id=str(worker_id), ttl_seconds=lease_ttl)
        if not row:
            idle_seconds += poll
            if idle_seconds >= idle_exit:
                _log("queue empty -- exiting"); break
            time.sleep(poll); continue
        idle_seconds = 0.0
        job = _job_dict(row)
        _log(f"lease {row['url']} | {job['title'][:48]} @ {job.get('company')}")

        proc = launch_chrome(worker_id, port=port, headless=True)
        status, dur_ms, cost = "failed:worker_error", 0, 0.0
        try:
            status, dur_ms = launcher.run_job(job, port, worker_id=worker_id,
                                              model=model, agent=agent, dry_run=False)
            cost = float(launcher._last_run_stats.get(worker_id, {}).get("cost_usd", 0) or 0)
        except Exception as e:  # never let one job kill the worker
            status = f"failed:worker_error:{type(e).__name__}:{str(e)[:80]}"
        finally:
            cleanup_worker(worker_id, proc)

        pg_status, apply_error = _map_status(status)
        pgqueue.write_result(pg, str(worker_id), row["url"], status=pg_status,
                             apply_status=status, apply_error=apply_error,
                             est_cost_usd=cost, agent_model=model, apply_duration_ms=dur_ms)
        applied += (pg_status == "applied")
        failed += (pg_status != "applied")
        _log(f"result {row['url']} -> {pg_status} ({status}) ${cost:.4f} {dur_ms}ms "
             f"| applied={applied} other={failed}")
        launcher._throttle_after_apply(job["application_url"])  # natural inter-job pacing

    _log(f"worker {worker_id} done | applied={applied} other={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
