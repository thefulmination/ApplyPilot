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
    os.environ["APPLYPILOT_PREFLIGHT_LIVENESS"] = "1"   # ON: cheap read-only GET skips DEAD postings before launching the agent (jobs expire after queueing)
    os.environ["APPLYPILOT_LANE_FILTER"] = "0"                           # offsite-only already
    os.environ.setdefault("APPLYPILOT_AGENT_TIMEOUT", "300")             # kill runaways (~5 min)


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


# Repeated agent usage/quota walls: each is re-queued (the job was never touched), but after
# this many CONSECUTIVE walls the worker stops leasing and exits instead of spinning ~30
# instant-fails/min through the whole queue. A wall typically lasts until a reset time, so a
# fresh worker restarting later is the right recovery. 0 disables the backoff. Tunable.
USAGE_LIMIT_MAX_STREAK = int(os.environ.get("APPLYPILOT_USAGE_LIMIT_MAX_STREAK") or 3)
# Brief cooldown (seconds) between consecutive usage-limit re-queues, so a wall can't churn
# the queue at full lease speed before the streak limit trips.
USAGE_LIMIT_COOLDOWN = float(os.environ.get("APPLYPILOT_USAGE_LIMIT_COOLDOWN") or 30)


def _handle_run_status(pg, worker_id, url, status, *, cost, dur_ms, model) -> str:
    """Persist one run_job outcome and release the lease. Returns the action taken:

      'requeued' -- an agent usage/quota wall (provably never touched the page: a turn-1
                    failure with zero browser tool calls) -> back to 'queued' so it is
                    re-leased later. NOT crash_unconfirmed: re-queuing cannot double-submit.
      'applied'  -- a confirmed submit.
      'other'    -- any other terminal outcome (failed / blocked / crash_unconfirmed),
                    written via _map_status. crash_unconfirmed handling is UNCHANGED: a
                    genuine ran-but-no-result crash (no_result_line/timeout/worker_error)
                    still parks crash_unconfirmed and is never re-leased.
    """
    # Lazy import: main() points config at container paths BEFORE importing applypilot, so
    # these must not be imported at module load. By the time this runs they are cached.
    from applypilot.apply import launcher, pgqueue
    if launcher.is_usage_limit_result(status):
        pgqueue.requeue_job(pg, str(worker_id), url, apply_error=(status or "")[:200])
        return "requeued"
    pg_status, apply_error = _map_status(status)
    pgqueue.write_result(pg, str(worker_id), url, status=pg_status,
                         apply_status=status, apply_error=apply_error,
                         est_cost_usd=cost, agent_model=model, apply_duration_ms=dur_ms)
    return "applied" if pg_status == "applied" else "other"


def _hydrate_assets(pg) -> None:
    """Write profile.json + resume.pdf from Postgres to APPLYPILOT_DIR before the first apply.
    PII ships through PG (not a Railway volume/secret); skips a file already on disk."""
    import pathlib
    from applypilot.apply import pgqueue
    appdir = pathlib.Path(os.environ.get("APPLYPILOT_DIR", "/data/applypilot"))
    appdir.mkdir(parents=True, exist_ok=True)
    for fname in ("profile.json", "resume.pdf"):
        dest = appdir / fname
        data = pgqueue.get_asset(pg, fname)
        if data:
            dest.write_bytes(data)
            _log(f"hydrated {fname} ({len(data)} bytes) from Postgres")
        elif not dest.exists():
            _log(f"WARNING: {fname} not in Postgres and not on disk -- applies will fail")


# Compute the REAL DeepSeek cost from token counts. The Claude CLI prices the run via the
# proxy and may not reflect DeepSeek's rates or caching, so the cap would be inaccurate if we
# trusted its total_cost_usd. Standard-tier rates ($/M input, $/M output).
_DEEPSEEK_RATES = {  # ($/M cache-miss input, $/M output, $/M cache-hit input)
    "deepseek-chat": (0.14, 0.28, 0.0028),     # 'deepseek-chat' routes to v4-flash
    "deepseek-v4-flash": (0.14, 0.28, 0.0028),
    "deepseek-v4-pro": (0.435, 0.87, 0.0036),
    "deepseek-reasoner": (0.55, 2.19, 0.07),
}


def _real_cost(stats: dict, model: str) -> float:
    rates = _DEEPSEEK_RATES.get(model)
    if not rates:
        return float(stats.get("cost_usd", 0) or 0)   # non-DeepSeek: trust the CLI
    rin, rout, rcache = rates
    return ((stats.get("input_tokens", 0) or 0) / 1e6 * rin        # cache-miss input
            + (stats.get("cache_read", 0) or 0) / 1e6 * rcache     # cache-hit input (cheap)
            + (stats.get("output_tokens", 0) or 0) / 1e6 * rout)   # output


def _run_diag(worker_id, model, port, launcher, config) -> None:
    """One-shot proxy diagnostic (APPLYPILOT_FLEET_DIAG=1): the apply agent reaches the model
    but HANGS, so probe the in-container LiteLLM proxy DIRECTLY (bypassing claude) to localize
    the hang: is the DeepSeek key set, does /health answer, does count_tokens answer, does a
    real /v1/messages completion return or hang/error? Touches NO job in the queue."""
    import subprocess
    cp = config.get_claude_path()
    v = subprocess.run([cp, "--version"], capture_output=True, text=True)
    _log(f"DIAG claude={v.stdout.strip()!r}")
    try:
        import litellm
        _log(f"DIAG litellm version={getattr(litellm, '__version__', '?')}")
    except Exception as e:
        _log(f"DIAG litellm import EXC {type(e).__name__}: {e}")

    key = os.environ.get("DEEPSEEK_API_KEY", "") or ""
    _log(f"DIAG DEEPSEEK_API_KEY set={bool(key)} len={len(key)} "
         f"stripped_len={len(key.strip())} prefix={(key[:3] + '...') if key else '<empty>'}")

    import httpx
    base = "http://127.0.0.1:4000"
    hdr = {"Authorization": "Bearer sk-litellm", "x-api-key": "sk-litellm",
           "anthropic-version": "2023-06-01", "content-type": "application/json"}
    for path, body, tmo in [
        ("/health/liveliness", None, 10),
        ("/v1/messages/count_tokens",
         {"model": model, "messages": [{"role": "user", "content": "hi"}]}, 30),
        ("/v1/messages",
         {"model": model, "max_tokens": 16,
          "messages": [{"role": "user", "content": "Reply with the single word READY."}]}, 45),
    ]:
        try:
            if body is None:
                r = httpx.get(base + path, timeout=tmo)
            else:
                r = httpx.post(base + path, json=body, headers=hdr, timeout=tmo)
            _log(f"DIAG proxy {path} rc={r.status_code} body={r.text[:450]!r}")
        except Exception as e:
            _log(f"DIAG proxy {path} EXC {type(e).__name__}: {str(e)[:200]}")
    _log("=== DIAG DONE ===")


def _run_crash_diag(config, launcher, pgqueue, launch_chrome, cleanup_worker,
                    worker_id, model, port) -> None:
    """Reproduce specific applies (DRY-RUN, submits NOTHING) and dump the full agent transcript
    to STDOUT so railway logs capture WHY a clean ATS crashed (no_result_line / timeout) in the
    container when it applies fine locally. Default targets the Cursor Ashby job that returned
    no_result at $0; override with APPLYPILOT_DIAG_JOB_URL (comma-separated job urls)."""
    import pathlib
    urls = [u.strip() for u in os.environ.get(
        "APPLYPILOT_DIAG_JOB_URL",
        "https://www.linkedin.com/jobs/view/4422701219,https://www.indeed.com/viewjob?jk=3b98044c179cd985").split(",") if u.strip()]
    models = [m.strip() for m in os.environ.get(
        "APPLYPILOT_DIAG_MODELS", "deepseek-chat,deepseek-v4-pro").split(",") if m.strip()]
    pg = pgqueue.connect()
    for url in urls:
        cur = pg.cursor()
        cur.execute("SELECT url, title, company, application_url, score FROM apply_queue WHERE url = %s", (url,))
        row = cur.fetchone()
        if not row:
            _log(f"DIAG-CRASH job not found in queue: {url}"); continue
        d = dict(row)
        for mdl in models:
            job = {
                "url": d["url"], "title": d.get("title") or "this role", "company": d.get("company"),
                "site": d.get("company") or "", "application_url": d["application_url"],
                "audit_score": d.get("score"), "fit_score": None,
                "tailored_resume_path": None, "cover_letter_path": None, "full_description": "",
            }
            _log(f"DIAG-CRASH [{mdl}] (dry_run) {job['title'][:48]} -> {job['application_url']}")
            proc = launch_chrome(worker_id, port=port, headless=True)
            try:
                status, dur = launcher.run_job(job, port, worker_id=worker_id, model=mdl,
                                               agent="claude", dry_run=True)
                stats = launcher._last_run_stats.get(worker_id, {})
                _log(f"DIAG-CRASH [{mdl}] result status={status} dur={dur}ms in={stats.get('input_tokens')} "
                     f"out={stats.get('output_tokens')} cache={stats.get('cache_read')}")
            except Exception as e:
                _log(f"DIAG-CRASH [{mdl}] run_job EXC {type(e).__name__}: {str(e)[:300]}")
            finally:
                cleanup_worker(worker_id, proc)
            wl = pathlib.Path(config.LOG_DIR) / f"worker-{worker_id}.log"
            if wl.exists():
                t = wl.read_text(errors="replace")
                _log(f"DIAG-CRASH [{mdl}] worker_log tail ({len(t)}b):\n{t[-8000:]}")
                wl.write_text("")  # isolate the next model's transcript
            else:
                _log(f"DIAG-CRASH [{mdl}] no worker_log at {wl}")
    _log("=== DIAG-CRASH DONE ===")


def main() -> int:
    _setup_env()
    signal.signal(signal.SIGTERM, _on_term)
    signal.signal(signal.SIGINT, _on_term)

    from applypilot import config
    from applypilot.apply import launcher, pgqueue
    from applypilot.apply.chrome import launch_chrome, cleanup_worker

    # Create LOG_DIR + the other APPLYPILOT_DIR subdirs. The home box does this at CLI init
    # via ensure_dirs(); the worker bypasses the CLI, so without this the first apply crashes
    # opening /data/applypilot/logs/worker-0.log (the dir never existed in the fresh container).
    config.ensure_dirs()

    worker_id = int(os.environ.get("APPLYPILOT_WORKER_ID", "0"))
    model = os.environ.get("APPLYPILOT_APPLY_MODEL", "deepseek-chat")
    agent = os.environ.get("APPLYPILOT_APPLY_AGENT", "claude")
    poll = float(os.environ.get("APPLYPILOT_POLL_SECONDS", "15"))
    idle_exit = float(os.environ.get("APPLYPILOT_IDLE_EXIT_SECONDS", "300"))
    lease_ttl = int(os.environ.get("APPLYPILOT_LEASE_TTL", "1500"))
    port = launcher.BASE_CDP_PORT + worker_id

    pg = pgqueue.connect()
    pgqueue.ensure_schema(pg)                          # idempotent; safe under concurrency
    _hydrate_assets(pg)                                # write profile.json + resume.pdf from PG
    reclaimed = pgqueue.reclaim_stale_leases(pg)       # startup crash sweep
    _log(f"worker {worker_id} up | model={model} agent={agent} | reclaimed {len(reclaimed)} stale leases")

    _diag_mode = os.environ.get("APPLYPILOT_FLEET_DIAG", "")
    if _diag_mode == "1":
        _run_diag(worker_id, model, port, launcher, config)
        return 0
    if _diag_mode == "crash":
        _run_crash_diag(config, launcher, pgqueue, launch_chrome, cleanup_worker, worker_id, model, port)
        return 0

    idle_seconds = 0.0
    applied = failed = requeued = 0
    consecutive_usage_limit = 0
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

        # Preflight liveness: one read-only GET skips DEAD postings BEFORE the expensive agent
        # launch. The fleet runs its own loop and bypasses launcher's supervised preflight, so
        # we probe here. Conservative -- only a strong DEAD signal skips; 401/403/429/5xx/live
        # proceed (a wall never false-skips). Costs ~$0 vs a ~$0.004-0.02 agent run on a 404.
        try:
            from applypilot.apply.liveness import probe_url as _probe, DEAD as _DEAD
            _ls, _lr = _probe(job["application_url"])
        except Exception as _e:
            _ls, _lr = "uncertain", f"probe_err:{type(_e).__name__}"
        if _ls == _DEAD:
            pgqueue.write_result(pg, str(worker_id), row["url"], status="failed",
                                 apply_status="expired", apply_error=f"preflight_{_lr}"[:200],
                                 est_cost_usd=0.0, agent_model=model, apply_duration_ms=0)
            failed += 1
            _log(f"preflight DEAD skip ({_lr}) -- saved a launch | applied={applied} other={failed}")
            continue

        proc = launch_chrome(worker_id, port=port, headless=True)
        status, dur_ms, cost = "failed:worker_error", 0, 0.0
        try:
            status, dur_ms = launcher.run_job(job, port, worker_id=worker_id,
                                              model=model, agent=agent, dry_run=False)
            cost = _real_cost(launcher._last_run_stats.get(worker_id, {}), model)
        except Exception as e:  # never let one job kill the worker
            status = f"failed:worker_error:{type(e).__name__}:{str(e)[:80]}"
        finally:
            cleanup_worker(worker_id, proc)

        action = _handle_run_status(pg, worker_id, row["url"], status,
                                    cost=cost, dur_ms=dur_ms, model=model)

        # Usage/quota wall (the agent never touched the page): re-queued, not parked. Back
        # off after a streak so a wall doesn't churn the whole queue at lease speed -- the
        # wall lasts until a reset time, so a fresh worker restarting later is the recovery.
        if action == "requeued":
            consecutive_usage_limit += 1
            requeued += 1
            _log(f"usage-limit wall (page never touched) -> re-queued {row['url']} "
                 f"| streak={consecutive_usage_limit}/{USAGE_LIMIT_MAX_STREAK} "
                 f"applied={applied} other={failed} requeued={requeued}")
            if USAGE_LIMIT_MAX_STREAK > 0 and consecutive_usage_limit >= USAGE_LIMIT_MAX_STREAK:
                _log(f"{consecutive_usage_limit} consecutive usage-limit walls -- backing off "
                     f"(stop leasing); worker exiting so it isn't spinning the queue")
                break
            if USAGE_LIMIT_COOLDOWN > 0:
                time.sleep(USAGE_LIMIT_COOLDOWN)
            continue

        consecutive_usage_limit = 0  # any non-wall outcome resets the streak
        applied += (action == "applied")
        failed += (action != "applied")
        _log(f"result {row['url']} -> {action} ({status}) ${cost:.4f} {dur_ms}ms "
             f"| applied={applied} other={failed} requeued={requeued}")
        launcher._throttle_after_apply(job["application_url"])  # natural inter-job pacing

    _log(f"worker {worker_id} done | applied={applied} other={failed} requeued={requeued}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
