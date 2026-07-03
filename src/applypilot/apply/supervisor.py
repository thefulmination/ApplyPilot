"""Crash/stall supervisor for the apply pipeline.

The apply run is heavy (Chrome + a spawned agent + a Playwright MCP node server per
job). On a resource-contended machine it can be OOM-killed by the OS -- a SILENT death
(no Python traceback), which the run itself cannot recover from. This supervisor is a
SEPARATE, lightweight process that:

  * launches `applypilot apply` as a subprocess and watches it,
  * detects a CRASH (subprocess exits) within one poll (~30s) and a STALL (no output
    for `stall_minutes`, longer than the per-job agent timeout) and kills it,
  * cleans up orphaned Chrome / Playwright-MCP node servers between attempts (a hard
    kill bypasses the run's own atexit cleanup),
  * restarts automatically until the total cost budget is (estimated) spent, or a
    wall-clock / attempt cap is hit,
  * logs every event to `<LOG_DIR>/supervisor.log` so a failure is diagnosable fast.

Run via `applypilot supervise-apply` (see cli.py). The supervisor stays alive when the
apply subprocess dies, so recovery is automatic instead of a 40-minute manual catch.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from applypilot import config
from applypilot.apply.chrome import _kill_on_port, BASE_CDP_PORT


def _applied_count() -> int:
    """DB count of applied jobs -- the cross-restart progress signal (survives crashes)."""
    import sqlite3
    try:
        c = sqlite3.connect(str(config.DB_PATH), timeout=10)
        n = c.execute("SELECT COUNT(*) FROM jobs WHERE apply_status = 'applied'").fetchone()[0]
        c.close()
        return n
    except Exception:
        return -1


def _apply_cost_total() -> float:
    """Durable cumulative apply-AGENT cost (USD) across all runs (llm_usage, stage=
    'apply_agent'). Each apply subprocess persists its real per-job agent cost there, so
    this survives crashes/restarts and lets the supervisor track ACTUAL session spend
    (snapshot-at-start delta) instead of estimating from applied-count. Returns -1.0 if
    unavailable (old DB / no rows yet) so callers fall back to the estimate."""
    import sqlite3
    try:
        c = sqlite3.connect(str(config.DB_PATH), timeout=10)
        try:
            row = c.execute(
                "SELECT COALESCE(SUM(est_cost_usd), 0) FROM llm_usage "
                "WHERE stage = 'apply_agent'").fetchone()
            return float(row[0] or 0.0)
        finally:
            c.close()
    except Exception:
        return -1.0


_ORPHAN_PATTERN = "_npx|playwright|modelcontextprotocol|@playwright"


def _orphan_kill_cmd() -> list[str]:
    """Platform command to kill orphaned Playwright-MCP node servers. Matched by command
    line so the desktop app / unrelated node processes are never touched."""
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                f"Where-Object {{ $_.CommandLine -match '{_ORPHAN_PATTERN}' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"]
    return ["pkill", "-f", _ORPHAN_PATTERN]


def _cleanup_orphans(log) -> None:
    """Between attempts: free the CDP port (kill any leftover Chrome) and kill orphaned
    Playwright-MCP node servers so a fresh agent can't be hijacked. A hard-killed run
    leaves these behind. Best-effort; never raises."""
    try:
        _kill_on_port(BASE_CDP_PORT)
    except Exception:
        pass
    # Kill orphaned Playwright MCP node servers (apply's browser automation). Matched by
    # command line so we never touch the desktop app or unrelated node processes.
    try:
        subprocess.run(
            _orphan_kill_cmd(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
    except Exception:
        pass


def supervise(
    total_cost_usd: float,
    *,
    model: str = "sonnet",
    linkedin_daily_cap: int = 20,
    base_resume: bool = True,
    max_job_age_days: int = 0,
    lane_filter: bool = True,
    preflight_liveness: bool = True,
    workers: int = 1,
    stall_minutes: float = 20.0,
    max_attempts: int = 30,
    max_hours: float = 14.0,
    est_cost_per_apply: float = 1.5,
    poll_seconds: float = 30.0,
    target_applied: int = 0,
) -> None:
    """Run `applypilot apply` under crash/stall auto-restart until the budget is spent (or
    an ABSOLUTE applied target is reached) or a safety bound is hit. target_applied > 0
    uses an absolute "stop when COUNT(applied) >= target" -- this composes across
    restarts (an outer keep-alive task can relaunch this and it picks up where it left
    off), and on reaching it writes a done-marker the task watches to stop relaunching."""
    log_path = config.LOG_DIR / "supervisor.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    done_marker = config.DB_PATH.parent / "keepalive.done"

    def log(msg: str) -> None:
        line = f"{datetime.now(timezone.utc).isoformat()}  {msg}"
        print(line, flush=True)
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def write_done(reason: str) -> None:
        try:
            done_marker.write_text(f"{datetime.now(timezone.utc).isoformat()}  {reason}", encoding="utf-8")
        except Exception:
            pass

    # Off-machine DB backup. The keep-alive runs this supervisor DIRECTLY, and the
    # authoritative DB + its rolling backup both live on the LOCAL disk -- a local-disk
    # loss would take both. The supervisor is the durable process (it outlives every
    # apply-subprocess crash), so it mirrors the DB into the OneDrive-synced dir on a
    # cadence that survives crash-looping (an apply subprocess that dies before its own
    # 10-min backup never produces an offsite copy). Time-gated to APPLYPILOT_BACKUP_INTERVAL.
    _last_offsite = [0.0]

    def offsite_backup(force: bool = False) -> None:
        interval = float(os.environ.get("APPLYPILOT_BACKUP_INTERVAL") or 600)
        if interval <= 0:
            return
        now = time.monotonic()
        if not force and (now - _last_offsite[0]) < interval:
            return
        _last_offsite[0] = now
        try:
            from applypilot.apply.launcher import mirror_db_offsite
            if mirror_db_offsite(log):
                log("OFFSITE-BACKUP: OneDrive-synced DB copy refreshed")
        except Exception:
            pass

    baseline = _applied_count()
    baseline_cost = _apply_cost_total()  # durable spend snapshot for ACTUAL session cost

    def session_spend(applied_n: int) -> float:
        """USD spent THIS session. Prefers ACTUAL apply-agent cost (durable, includes
        failed/expired launches the applied-count estimate misses); falls back to the
        applied-count estimate and takes the MAX so a budget run never overspends while
        actual cost is still ramping (or an old apply build isn't recording it yet)."""
        cn = _apply_cost_total()
        actual = max(0.0, cn - baseline_cost) if (cn >= 0 and baseline_cost >= 0) else 0.0
        est = max(0, applied_n - baseline) * est_cost_per_apply
        return max(actual, est)

    start = time.monotonic()
    attempt = 0
    log(f"SUPERVISOR start: total_budget=${total_cost_usd:.0f}, baseline_applied={baseline}, "
        f"baseline_cost=${max(0.0, baseline_cost):.2f}, stall={stall_minutes}m, "
        f"max_attempts={max_attempts}, max_hours={max_hours}, est_cost_per_apply=${est_cost_per_apply}")
    offsite_backup(force=True)  # capture an off-machine copy before anything runs

    while True:
        elapsed_h = (time.monotonic() - start) / 3600.0
        if attempt >= max_attempts:
            log(f"STOP: hit max_attempts={max_attempts}"); break
        if elapsed_h >= max_hours:
            log(f"STOP: hit max_hours={max_hours:.1f}"); break

        applied_now = _applied_count()
        spent = session_spend(applied_now)
        if target_applied > 0:
            if applied_now >= target_applied:
                log(f"STOP: applied target {target_applied} reached (applied={applied_now})")
                write_done(f"target {target_applied} reached"); break
            remaining = max(est_cost_per_apply, (target_applied - applied_now) * est_cost_per_apply)
        else:
            remaining = total_cost_usd - spent
            if remaining <= max(0.5, est_cost_per_apply):
                log(f"STOP: budget ~${total_cost_usd:.0f} reached "
                    f"({applied_now - baseline} applies, spent ${spent:.2f})")
                write_done("budget reached"); break

        attempt += 1
        _cleanup_orphans(log)
        offsite_backup()  # periodic off-machine backup at each restart boundary
        # Reclaim any lease stranded by the previous crash so its job is retryable.
        try:
            from applypilot.apply.launcher import reclaim_stale_leases
            reclaim_stale_leases()
        except Exception:
            pass

        cmd = [sys.executable, "-m", "applypilot.cli", "apply", "--continuous",
               "--workers", str(workers), "--model", model,
               "--linkedin-daily-cap", str(linkedin_daily_cap),
               "--max-cost-usd", f"{remaining:.2f}"]
        if base_resume:
            cmd.append("--base-resume")
        child_env = dict(os.environ)
        if max_job_age_days > 0:
            child_env["APPLYPILOT_MAX_JOB_AGE_DAYS"] = str(max_job_age_days)
        if lane_filter:
            # Off-lane drift guard (see launcher.acquire_job / config.load_lane_filter):
            # keep a drained on-lane queue from drifting into IC-sales/AE postings.
            child_env["APPLYPILOT_LANE_FILTER"] = "1"
        if preflight_liveness:
            # Pre-launch closure probe (see launcher.worker_loop): skip dead-on-visit
            # postings before they burn a Chrome launch.
            child_env["APPLYPILOT_PREFLIGHT_LIVENESS"] = "1"

        log(f"ATTEMPT {attempt}: launching apply (spent ${spent:.2f}, "
            f"per-attempt cap ${remaining:.2f}, applied={applied_now})")
        out_path = config.LOG_DIR / f"supervised_attempt_{attempt}.out"
        with open(out_path, "w", encoding="utf-8") as out:
            proc = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT, env=child_env)

        last_applied = applied_now
        last_progress = time.monotonic()
        while True:
            time.sleep(poll_seconds)
            rc = proc.poll()
            if rc is not None:
                log(f"ATTEMPT {attempt} EXITED rc={rc} "
                    f"({'clean' if rc == 0 else 'CRASH/kill'}); applied={_applied_count()}")
                break
            cur = _applied_count()
            if cur > last_applied:
                last_applied = cur
                last_progress = time.monotonic()
            # Mid-attempt stop (absolute applied target, or budget spent -- actual cost
            # when available, estimate as a floor).
            done = (cur >= target_applied) if target_applied > 0 else \
                   (session_spend(cur) >= total_cost_usd)
            if done:
                log(f"STOP reached mid-attempt (applied={cur}) -- stopping run")
                _kill_tree(proc)
                write_done("target/budget reached mid-attempt")
                offsite_backup(force=True)  # final off-machine backup on stop
                log("SUPERVISOR done.")
                return
            # Stall: no output for stall_minutes AND no new apply -> stuck, kill + restart.
            try:
                quiet = (time.time() - out_path.stat().st_mtime) / 60.0
            except OSError:
                quiet = 0.0
            stuck = (time.monotonic() - last_progress) / 60.0
            if quiet >= stall_minutes and stuck >= stall_minutes:
                log(f"ATTEMPT {attempt} STALLED (no output {quiet:.0f}m, no apply {stuck:.0f}m) "
                    f"-- killing to restart")
                _kill_tree(proc)
                break

    offsite_backup(force=True)  # final off-machine backup on stop
    log(f"SUPERVISOR done: {_applied_count() - baseline} applies this session "
        f"over {attempt} attempt(s), {elapsed_h:.1f}h.")


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the apply subprocess and its children (Chrome/agent/MCP)."""
    if proc.poll() is not None:
        return
    try:
        import platform
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        else:
            proc.kill()
        proc.wait(timeout=20)
    except Exception:
        pass
