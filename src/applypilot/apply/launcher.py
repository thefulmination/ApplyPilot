"""Apply orchestration: acquire jobs, spawn Claude Code sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + Claude Code for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import json
import logging
import os
import platform
import random
import re
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.live import Live

from applypilot import config
from applypilot.applications import record_application
from applypilot.database import get_connection
from applypilot.apply import prompt as prompt_mod
from applypilot.apply.chrome import (
    launch_chrome, cleanup_worker, kill_all_chrome,
    reset_worker_dir, cleanup_on_exit, _kill_process_tree,
    BASE_CDP_PORT,
)
from applypilot.apply.dashboard import (
    init_worker, update_state, add_event, get_state,
    render_full, get_totals,
)

logger = logging.getLogger(__name__)

AUTH_REQUIRED_REASONS: set[str] = {
    "auth_required",
    "login_issue",
    "sso_required",
    "account_required",
    "email_verification_required",
    "two_factor_required",
    "2fa_required",
    "mfa_required",
    "linkedin_challenge",
}

# Blocked sites loaded from config/sites.yaml
def _load_blocked():
    from applypilot.config import load_blocked_sites
    return load_blocked_sites()

# How often to poll the DB when the queue is empty (seconds). Tunable via
# APPLYPILOT_POLL_INTERVAL / --poll-interval (lower = a worker idles less between
# empty polls; only matters once the queue drains).
POLL_INTERVAL = int(os.environ.get("APPLYPILOT_POLL_INTERVAL") or config.DEFAULTS["poll_interval"])

# Wall-clock budget for a single apply agent run. The stdout read loop runs in a
# daemon thread; if it does not finish within this many seconds the Claude
# process is killed and the job is marked failed:timeout. This bounds a hung
# session (which would otherwise block the worker forever, since the old
# proc.wait() only ran AFTER stdout reached EOF).
AGENT_TIMEOUT_SECONDS = int(os.environ.get("APPLYPILOT_AGENT_TIMEOUT") or 900)

# A worker claims a job by setting apply_status='in_progress'. If the process is
# hard-killed before writing a terminal result the lease is never released.
# Leases older than this (which must exceed AGENT_TIMEOUT_SECONDS) are reclaimed
# at startup AND periodically during the run (periodic reclaim thread). 1200s
# (was 1800) tightens the crash dead-window while staying clear of the 900s job
# timeout. Tunable via APPLYPILOT_STALE_LEASE_SECONDS.
STALE_LEASE_SECONDS = max(
    AGENT_TIMEOUT_SECONDS + 120,
    int(os.environ.get("APPLYPILOT_STALE_LEASE_SECONDS") or 1200),
)

# Thread-safe shutdown coordination
_stop_event = threading.Event()

# Track active Claude Code processes for skip (Ctrl+C) handling
_claude_procs: dict[int, subprocess.Popen] = {}
_claude_lock = threading.Lock()

# Register cleanup on exit
atexit.register(cleanup_on_exit)
if platform.system() != "Windows":
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


# ---------------------------------------------------------------------------
# MCP config
# ---------------------------------------------------------------------------

def _make_mcp_config(cdp_port: int) -> dict:
    """Build MCP config dict for a specific CDP port."""
    mcp_config = {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": [
                    # Pinned (NOT @latest): a silent Playwright-MCP upgrade could
                    # change CDP behavior or the automation surface under a live run.
                    # Bump deliberately after testing. (npm i -g for cache warmth.)
                    "@playwright/mcp@0.0.76",
                    f"--cdp-endpoint=http://localhost:{cdp_port}",
                    f"--viewport-size={config.DEFAULTS['viewport']}",
                ],
            }
        }
    }
    if os.environ.get("APPLYPILOT_ENABLE_GMAIL_MCP", "").lower() in {"1", "true", "yes", "on"}:
        mcp_config["mcpServers"]["gmail"] = {
            "command": "npx",
            "args": ["-y", "@gongrzhe/server-gmail-autoauth-mcp"],
        }
    return mcp_config


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def acquire_job(target_url: str | None = None, min_score: int = 7,
                worker_id: int = 0, exclude_linkedin: bool = False,
                exclude_urls: set[str] | None = None,
                exclude_hosts: set[str] | None = None) -> dict | None:
    """Atomically acquire the next job to apply to.

    Args:
        target_url: Apply to a specific URL instead of picking from queue.
        min_score: Minimum audited score / fit_score threshold.
        worker_id: Worker claiming this job (for tracking).
        exclude_linkedin: When True, skip LinkedIn-lane (Easy-Apply) jobs in queue
            selection so the run keeps flowing on the offsite ATS lane. Set by the
            worker loop when the LinkedIn daily cap or same-day halt is in effect.
        exclude_urls: URLs already attempted in THIS run; excluded from selection so
            a fast-failing job (e.g. no_result) isn't re-acquired immediately and
            doesn't burn the run's --limit on a single job. Cross-run retry still
            works (the set is per-run/in-memory; failed jobs stay eligible later).

    Returns:
        Job dict or None if the queue is empty.
    """
    conn = get_connection()
    from applypilot.config import is_manual_ats

    # --base-resume mode drops the per-job tailored-resume requirement so jobs
    # become applyable with the base resume (build_prompt/run_job fall back to
    # RESUME_PATH/RESUME_PDF_PATH; no AI tailoring).
    tailored_clause = "" if config.base_resume_enabled() else "AND tailored_resume_path IS NOT NULL"
    blocked_sites: list = []
    blocked_patterns: list = []
    if not target_url:
        blocked_sites, blocked_patterns = _load_blocked()

    # Loop so that hitting a manual-ATS row SKIPS it (marks it + moves on) rather
    # than returning None: worker_loop treats None as "queue empty" and would
    # otherwise abandon the entire rest of the queue the instant a manual-ATS job
    # (~25% of the eligible queue) sorted to the top.
    while True:
        try:
            conn.execute("BEGIN IMMEDIATE")

            if target_url:
                like = f"%{target_url.split('?')[0].rstrip('/')}%"
                row = conn.execute(f"""
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, audit_score, audit_label, location, full_description, cover_letter_path
                    FROM jobs
                    WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                      {tailored_clause}
                      AND duplicate_of_url IS NULL
                      AND COALESCE(liveness_status, '') != 'dead'
                      AND COALESCE(apply_status, '') != 'applied'
                      AND apply_status != 'in_progress'
                    ORDER BY CASE WHEN url = ? OR application_url = ? THEN 0 ELSE 1 END
                    LIMIT 1
                """, (target_url, target_url, like, like, target_url, target_url)).fetchone()
            else:
                # Build parameterized filters to avoid SQL injection
                params: list = [min_score]
                site_clause = ""
                if blocked_sites:
                    placeholders = ",".join("?" * len(blocked_sites))
                    site_clause = f"AND site NOT IN ({placeholders})"
                    params.extend(blocked_sites)
                url_clauses = ""
                if blocked_patterns:
                    url_clauses = " ".join("AND url NOT LIKE ?" for _ in blocked_patterns)
                    params.extend(blocked_patterns)
                # LinkedIn lane gating: when the daily cap / same-day halt is in
                # effect, skip Easy-Apply jobs so the run keeps flowing offsite.
                li_clause = f"AND NOT {_LINKEDIN_LANE_SQL}" if exclude_linkedin else ""
                # Per-run exclusion: don't re-acquire a job already attempted this
                # run, so one fast-failing job can't consume the whole --limit.
                seen_clause = ""
                if exclude_urls:
                    seen_clause = f"AND url NOT IN ({','.join('?' * len(exclude_urls))})"
                    params.extend(exclude_urls)
                # Offsite circuit breaker: skip jobs whose effective apply host has
                # been halted this run (repeated host-faults), so the worker keeps
                # flowing on healthy hosts instead of re-picking the flaky one.
                host_clause = ""
                if exclude_hosts:
                    eff = "(CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END)"
                    host_clause = " ".join(f"AND {eff} NOT LIKE ?" for _ in exclude_hosts)
                    params.extend(f"%{h}%" for h in exclude_hosts)
                row = conn.execute(f"""
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, audit_score, audit_label, location, full_description, cover_letter_path
                    FROM jobs
                    WHERE duplicate_of_url IS NULL
                      {tailored_clause}
                      AND COALESCE(liveness_status, '') != 'dead'
                      AND COALESCE(application_url, url) NOT IN (
                            SELECT COALESCE(application_url, url) FROM jobs WHERE apply_status = 'applied')
                      AND (apply_status IS NULL OR apply_status = 'failed')
                      AND (apply_attempts IS NULL OR apply_attempts < ?)
                      AND COALESCE(audit_score, fit_score) >= ?
                      {site_clause}
                      {url_clauses}
                      {li_clause}
                      {seen_clause}
                      {host_clause}
                    ORDER BY COALESCE(audit_score, fit_score) DESC,
                             (audit_flags LIKE '%"chief_of_staff"%') DESC,
                             (audit_flags LIKE '%"strategy_ops"%'
                               OR audit_flags LIKE '%"gtm_ops"%'
                               OR audit_flags LIKE '%"operations_leadership"%') DESC,
                             role_fit_score DESC,
                             (COALESCE(liveness_status, '') = 'live') DESC,
                             fit_score DESC, url
                    LIMIT 1
                """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchone()

            if not row:
                conn.rollback()
                return None

            # Skip manual ATS sites (unsolvable CAPTCHAs): mark + continue to the
            # next candidate rather than returning None.
            apply_url = row["application_url"] or row["url"]
            if is_manual_ats(apply_url):
                conn.execute(
                    "UPDATE jobs SET apply_status = 'manual', apply_error = 'manual ATS' WHERE url = ?",
                    (row["url"],),
                )
                conn.commit()
                logger.info("Skipping manual ATS: %s", row["url"][:80])
                if target_url:
                    return None  # the explicitly targeted URL is manual; nothing to apply
                continue  # re-select the next candidate (this row is now excluded)

            now = datetime.now(timezone.utc).isoformat()
            conn.execute("""
                UPDATE jobs SET apply_status = 'in_progress',
                               agent_id = ?,
                               last_attempted_at = ?
                WHERE url = ?
            """, (f"worker-{worker_id}", now, row["url"]))
            conn.commit()

            return dict(row)
        except Exception:
            conn.rollback()
            raise


def mark_result(url: str, status: str, error: str | None = None,
                permanent: bool = False, duration_ms: int | None = None,
                task_id: str | None = None) -> None:
    """Update a job's apply status in the database."""
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (now, duration_ms, task_id, url))
    else:
        db_status = "auth_required" if _is_auth_required_result(error or status) else status
        # A timeout (hung agent/page) is one-shot: a retry just burns another
        # ~15 min on the same hang, so mark attempts exhausted on the first timeout.
        _r = (error or status or "").lower()
        attempts = 99 if (permanent or "timeout" in _r) else "COALESCE(apply_attempts, 0) + 1"
        conn.execute(f"""
            UPDATE jobs SET apply_status = ?, apply_error = ?,
                           apply_attempts = {attempts}, agent_id = NULL,
                           apply_duration_ms = ?, apply_task_id = ?
            WHERE url = ?
        """, (db_status, error or "unknown", duration_ms, task_id, url))
    conn.commit()

    # Tag the channel by source so LinkedIn (and Indeed) applies are tracked
    # distinctly (channel='linkedin') instead of lumped into a generic
    # 'applypilot' -- lets us report/cap/diagnose LinkedIn applies on their own.
    from urllib.parse import urlparse
    _host = (urlparse(url or "").hostname or "").lower()
    _src_channel = "linkedin" if "linkedin" in _host else (
        "indeed" if "indeed" in _host else "applypilot")
    if status == "applied":
        tracker_status = "applied"
        tracker_channel = _src_channel
    elif _is_auth_required_result(error or status):
        tracker_status = "auth_required"
        tracker_channel = "assisted"
    else:
        tracker_status = "failed"
        tracker_channel = _src_channel
    tracker_notes = error
    if task_id:
        tracker_notes = f"{tracker_notes or status} | task_id={task_id}"
    try:
        record_application(
            url,
            status=tracker_status,
            channel=tracker_channel,
            notes=tracker_notes,
            update_job=False,
        )
    except Exception:
        logger.debug("Application tracker update failed for %s", url, exc_info=True)


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


def reclaim_stale_leases(ttl_seconds: int = STALE_LEASE_SECONDS) -> int:
    """Reset apply leases stranded 'in_progress' by workers that died mid-job.

    If an apply process is hard-killed (or the machine reboots) between claiming
    a job and writing a terminal result, the job stays 'in_progress' forever and
    is never retried. At startup we reclaim leases older than ttl_seconds, which
    must exceed the per-job agent timeout so a live, in-flight job is never
    stolen.

    Returns:
        Number of leases reclaimed.
    """
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat()
    cursor = conn.execute(
        """
        UPDATE jobs SET apply_status = NULL, agent_id = NULL
        WHERE apply_status = 'in_progress'
          AND (last_attempted_at IS NULL OR last_attempted_at < ?)
        """,
        (cutoff,),
    )
    conn.commit()
    return cursor.rowcount


def _redact_secrets(text: str) -> str:
    """Replace secret values with placeholders before persisting text to disk.

    Used for the --gen debug prompt file, which otherwise writes the CapSolver
    API key (and any other configured keys interpolated into the prompt) to a
    plaintext file under the logs directory.
    """
    config.load_env()
    redacted = text
    for var in ("CAPSOLVER_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY"):
        val = os.environ.get(var)
        if val and len(val) >= 6:
            redacted = redacted.replace(val, f"***{var}_REDACTED***")
    return redacted


# ---------------------------------------------------------------------------
# Utility modes (--gen, --mark-applied, --mark-failed, --reset-failed)
# ---------------------------------------------------------------------------

def gen_prompt(target_url: str, min_score: int = 7,
               model: str = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and print the Claude CLI command for manual debugging.

    Returns:
        Path to the generated prompt file, or None if no job found.
    """
    job = acquire_job(target_url=target_url, min_score=min_score, worker_id=worker_id)
    if not job:
        return None

    # Read resume text
    resume_path = config.resolve_resume_stem(job.get("tailored_resume_path"))
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    prompt = prompt_mod.build_prompt(job=job, tailored_resume=resume_text)

    # Release the lock so the job stays available
    release_lock(job["url"])

    # Write prompt file
    config.ensure_dirs()
    site_slug = (job.get("site") or "unknown")[:20].replace(" ", "_")
    prompt_file = config.LOG_DIR / f"prompt_{site_slug}_{job['title'][:30].replace(' ', '_')}.txt"
    prompt_file.write_text(_redact_secrets(prompt), encoding="utf-8")

    # Write MCP config for reference
    port = BASE_CDP_PORT + worker_id
    mcp_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    return prompt_file


def mark_job(url: str, status: str, reason: str | None = None) -> None:
    """Manually mark a job's apply status in the database.

    Args:
        url: Job URL to mark.
        status: Either 'applied' or 'failed'.
        reason: Failure reason (only for status='failed').
    """
    conn = get_connection()
    now = datetime.now(timezone.utc).isoformat()
    if status == "applied":
        conn.execute("""
            UPDATE jobs SET apply_status = 'applied', applied_at = ?,
                           apply_error = NULL, agent_id = NULL
            WHERE url = ?
        """, (now, url))
    else:
        conn.execute("""
            UPDATE jobs SET apply_status = 'failed', apply_error = ?,
                           apply_attempts = 99, agent_id = NULL
            WHERE url = ?
        """, (reason or "manual", url))
    conn.commit()
    try:
        record_application(
            url,
            status="applied" if status == "applied" else "failed",
            channel="manual",
            notes=reason,
            applied_at=now if status == "applied" else None,
            update_job=False,
        )
    except Exception:
        logger.debug("Application tracker update failed for %s", url, exc_info=True)


def reset_failed() -> int:
    """Reset all failed jobs so they can be retried.

    Returns:
        Number of jobs reset.
    """
    conn = get_connection()
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE apply_status = 'failed'
          OR (apply_status IS NOT NULL AND apply_status != 'applied'
              AND apply_status != 'in_progress')
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def run_job(job: dict, port: int, worker_id: int = 0,
            model: str = "sonnet", dry_run: bool = False) -> tuple[str, int]:
    """Spawn a Claude Code session for one job application.

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    # Read tailored resume text
    resume_path = config.resolve_resume_stem(job.get("tailored_resume_path"))
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Reset the worker's isolated working directory FIRST. build_prompt stages
    # upload files under worker-{id}/current and reset_worker_dir wipes
    # worker-{id}, so the reset must happen before the prompt is built (else the
    # freshly-copied resume/cover-letter would be deleted out from under it).
    worker_dir = reset_worker_dir(worker_id)

    # Build the prompt
    agent_prompt = prompt_mod.build_prompt(
        job=job,
        tailored_resume=resume_text,
        dry_run=dry_run,
        worker_id=worker_id,
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    # Build claude command
    cmd = [
        config.get_claude_path(),
        "--model", model,
        "-p",
        "--mcp-config", str(mcp_config_path),
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
        "--disallowedTools", (
            # Lock the agent out of the host. It runs with bypassPermissions and
            # reads attacker-influenceable page content, so a prompt-injected
            # page must NOT be able to read local files (exfil profile.json/.env),
            # write/execute, or browse outside the Playwright browser it drives.
            # disallowedTools is honored even under bypassPermissions. The agent
            # only needs the Playwright MCP tools (and optionally gmail) -- it
            # never legitimately uses these built-ins.
            "Bash,BashOutput,KillShell,Read,Write,Edit,MultiEdit,NotebookEdit,"
            "WebFetch,WebSearch,Glob,Grep,Task,"
            "mcp__gmail__draft_email,mcp__gmail__modify_email,"
            "mcp__gmail__delete_email,mcp__gmail__download_attachment,"
            "mcp__gmail__batch_modify_emails,mcp__gmail__batch_delete_emails,"
            "mcp__gmail__create_label,mcp__gmail__update_label,"
            "mcp__gmail__delete_label,mcp__gmail__get_or_create_label,"
            "mcp__gmail__list_email_labels,mcp__gmail__create_filter,"
            "mcp__gmail__list_filters,mcp__gmail__get_filter,"
            "mcp__gmail__delete_filter"
        ),
        "--output-format", "stream-json",
        "--verbose", "-",
    ]

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    display_score = job.get("audit_score") if job.get("audit_score") is not None else job.get("fit_score", 0)
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=display_score,
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    ts_header = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    llm_score_note = f" (LLM {job.get('fit_score')}/10)" if job.get("audit_score") is not None else ""
    log_header = (
        f"\n{'=' * 60}\n"
        f"[{ts_header}] {job['title']} @ {job.get('site', '')}\n"
        f"URL: {job.get('application_url') or job['url']}\n"
        f"Score: {display_score}/10{llm_score_note}\n"
        f"{'=' * 60}\n"
    )

    start = time.time()
    stats: dict = {}
    proc = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            cwd=str(worker_dir),
        )
        with _claude_lock:
            _claude_procs[worker_id] = proc

        text_parts: list[str] = []
        final_result_text: list[str] = []  # text from the final 'result' message
        stats_holder: dict = {}

        def _consume_stream() -> None:
            """Read the agent's stream-json stdout to EOF.

            Runs in a daemon thread so the parent can bound a hung session with a
            wall-clock join() timeout instead of blocking on ``for line in
            proc.stdout`` forever.
            """
            with open(worker_log, "a", encoding="utf-8") as lf:
                lf.write(log_header)
                for line in proc.stdout:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                        msg_type = msg.get("type")
                        if msg_type == "assistant":
                            for block in msg.get("message", {}).get("content", []):
                                bt = block.get("type")
                                if bt == "text":
                                    text_parts.append(block["text"])
                                    lf.write(block["text"] + "\n")
                                elif bt == "tool_use":
                                    name = (
                                        block.get("name", "")
                                        .replace("mcp__playwright__", "")
                                        .replace("mcp__gmail__", "gmail:")
                                    )
                                    inp = block.get("input", {})
                                    if "url" in inp:
                                        desc = f"{name} {inp['url'][:60]}"
                                    elif "ref" in inp:
                                        desc = f"{name} {inp.get('element', inp.get('text', ''))}"[:50]
                                    elif "fields" in inp:
                                        desc = f"{name} ({len(inp['fields'])} fields)"
                                    elif "paths" in inp:
                                        desc = f"{name} upload"
                                    else:
                                        desc = name

                                    lf.write(f"  >> {desc}\n")
                                    ws = get_state(worker_id)
                                    cur_actions = ws.actions if ws else 0
                                    update_state(worker_id,
                                                 actions=cur_actions + 1,
                                                 last_action=desc[:35])
                        elif msg_type == "result":
                            stats_holder.update({
                                "input_tokens": msg.get("usage", {}).get("input_tokens", 0),
                                "output_tokens": msg.get("usage", {}).get("output_tokens", 0),
                                "cache_read": msg.get("usage", {}).get("cache_read_input_tokens", 0),
                                "cache_create": msg.get("usage", {}).get("cache_creation_input_tokens", 0),
                                "cost_usd": msg.get("total_cost_usd", 0),
                                "turns": msg.get("num_turns", 0),
                            })
                            rt = msg.get("result", "")
                            text_parts.append(rt)
                            final_result_text.append(rt)
                    except json.JSONDecodeError:
                        text_parts.append(line)
                        lf.write(line + "\n")

        # Start reading BEFORE writing the prompt so a large prompt can't deadlock
        # against a full stdout pipe.
        reader = threading.Thread(target=_consume_stream,
                                  name=f"apply-reader-{worker_id}", daemon=True)
        reader.start()

        try:
            proc.stdin.write(agent_prompt)
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            # Claude exited before reading the prompt (e.g. a launch error). Let
            # the reader drain whatever was emitted and fall through to result
            # parsing, which will classify the (likely missing) RESULT line.
            try:
                proc.stdin.close()
            except Exception:
                pass

        # Bound the whole run by a wall-clock timeout. The old code only timed out
        # the post-EOF wait(), so a session that stopped emitting output but never
        # exited would hang the worker forever.
        reader.join(timeout=AGENT_TIMEOUT_SECONDS)
        if reader.is_alive():
            _kill_process_tree(proc.pid)
            reader.join(timeout=15)
            elapsed = int(time.time() - start)
            add_event(f"[W{worker_id}] TIMEOUT/hung ({elapsed}s)")
            update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
            return "failed:timeout", int((time.time() - start) * 1000)

        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _kill_process_tree(proc.pid)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                pass
        returncode = proc.returncode
        stats = stats_holder
        proc = None

        if returncode and returncode < 0:
            return "skipped", int((time.time() - start) * 1000)

        output = "\n".join(text_parts)
        # Prefer the agent's FINAL result message for the RESULT code; only fall
        # back to scanning the full transcript when the final message has none.
        # This stops a page that merely contains the literal text
        # "RESULT:APPLIED" from spoofing the outcome.
        final_text = "\n".join(t for t in final_result_text if t).strip()
        result_source = final_text if "RESULT:" in final_text else output
        elapsed = int(time.time() - start)
        duration_ms = int((time.time() - start) * 1000)

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        job_log = config.LOG_DIR / f"claude_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)

        def _clean_reason(s: str) -> str:
            return re.sub(r'[*`"]+$', '', s).strip()

        # Dry run: the agent reviewed/filled the form but intentionally did NOT
        # submit. Never let this be recorded as 'applied'.
        if "RESULT:DRY_RUN" in result_source:
            add_event(f"[W{worker_id}] DRY-RUN OK ({elapsed}s): {job['title'][:30]}")
            update_state(worker_id, status="dry_run", last_action=f"DRY-RUN ({elapsed}s)")
            return "dry_run", duration_ms

        for result_status in ["APPLIED", "EXPIRED", "CAPTCHA", "LOGIN_ISSUE", "AUTH_REQUIRED"]:
            if f"RESULT:{result_status}" in result_source:
                add_event(f"[W{worker_id}] {result_status} ({elapsed}s): {job['title'][:30]}")
                update_state(worker_id, status=result_status.lower(),
                             last_action=f"{result_status} ({elapsed}s)")
                return result_status.lower(), duration_ms

        if "RESULT:FAILED" in result_source:
            for out_line in result_source.split("\n"):
                if "RESULT:FAILED" in out_line:
                    reason = (
                        out_line.split("RESULT:FAILED:")[-1].strip()
                        if ":" in out_line[out_line.index("FAILED") + 6:]
                        else "unknown"
                    )
                    reason = _clean_reason(reason)
                    PROMOTE_TO_STATUS = {"captcha", "expired", "login_issue", "auth_required"}
                    if reason in PROMOTE_TO_STATUS:
                        add_event(f"[W{worker_id}] {reason.upper()} ({elapsed}s): {job['title'][:30]}")
                        update_state(worker_id, status=reason,
                                     last_action=f"{reason.upper()} ({elapsed}s)")
                        return reason, duration_ms
                    add_event(f"[W{worker_id}] FAILED ({elapsed}s): {reason[:30]}")
                    update_state(worker_id, status="failed",
                                 last_action=f"FAILED: {reason[:25]}")
                    return f"failed:{reason}", duration_ms
            return "failed:unknown", duration_ms

        add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return "failed:no_result_line", duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        elapsed = int(time.time() - start)
        add_event(f"[W{worker_id}] TIMEOUT ({elapsed}s)")
        update_state(worker_id, status="failed", last_action=f"TIMEOUT ({elapsed}s)")
        return "failed:timeout", duration_ms
    except Exception as e:
        duration_ms = int((time.time() - start) * 1000)
        add_event(f"[W{worker_id}] ERROR: {str(e)[:40]}")
        update_state(worker_id, status="failed", last_action=f"ERROR: {str(e)[:25]}")
        return f"failed:{str(e)[:100]}", duration_ms
    finally:
        with _claude_lock:
            _claude_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

PERMANENT_FAILURES: set[str] = {
    "expired", "captcha", "login_issue", "auth_required",
    "not_eligible_location", "not_eligible_salary",
    "already_applied", "account_required",
    "not_a_job_application", "unsafe_permissions",
    "unsafe_verification", "sso_required",
    "site_blocked", "cloudflare_blocked", "blocked_by_cloudflare",
    # A dead/404/removed posting won't come back on retry -> don't burn 3x900s.
    "page_error",
    # An unfillable/injected page or a hung agent won't fix on retry either.
    "stuck", "suspicious_page",
    # LinkedIn (or any site) reporting an application/Easy-Apply limit -> permanent.
    "linkedin_rate_limited", "rate_limited",
    # A missing RESULT: line means the agent crashed/hung/exited without doing the
    # job (e.g. auth dead, CDP not ready). It won't fix itself on retry, and the
    # auth pre-flight + CDP-readiness poll + sonnet default remove the transient
    # causes -> mark permanent so it can't burn the retry budget cross-run.
    "no_result_line",
    # No valid http(s) apply URL -> can't be applied; don't waste a launch retrying.
    "bad_application_url",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_auth_required_result(result: str | None) -> bool:
    """Return True when a result should be routed to assisted/manual login."""
    if not result:
        return False
    reason = result.split(":", 1)[-1] if ":" in result else result
    normalized = reason.strip().lower()
    return normalized in AUTH_REQUIRED_REASONS


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


# ---------------------------------------------------------------------------
# Worker loop
# ---------------------------------------------------------------------------

# Inter-job apply throttle (account safety): space out real submissions so a
# bulk run doesn't hammer one host (esp. LinkedIn) from a single browser profile.
# Tunable via env; set APPLYPILOT_APPLY_MAX_DELAY=0 to disable the inter-job delay.
APPLY_MIN_DELAY = float(os.environ.get("APPLYPILOT_APPLY_MIN_DELAY", "15"))
APPLY_MAX_DELAY = float(os.environ.get("APPLYPILOT_APPLY_MAX_DELAY", "40"))
APPLY_HOST_GAP = float(os.environ.get("APPLYPILOT_APPLY_HOST_GAP", "90"))
# LinkedIn is the owner's real account AND ~44% of the queue -> a wider gap than
# a tiny board (balanced default 120s; tune via APPLYPILOT_LINKEDIN_HOST_GAP).
LINKEDIN_HOST_GAP = float(os.environ.get("APPLYPILOT_LINKEDIN_HOST_GAP", "120"))
# Per-host gap JITTER: a fixed 90s/120s cadence is itself a machine signature, so
# multiply the gap by a random factor on every wait. Default band [0.7, 1.4] (mean
# ~1.05 -> slightly conservative). Set HI <= LO to disable.
GAP_JITTER_LO = float(os.environ.get("APPLYPILOT_GAP_JITTER_LO", "0.7"))
GAP_JITTER_HI = float(os.environ.get("APPLYPILOT_GAP_JITTER_HI", "1.4"))
_last_apply_by_host: dict[str, float] = {}
_throttle_lock = threading.Lock()

# --- LinkedIn lane gating (account-safety) -------------------------------------
# LinkedIn Easy-Apply daily cap, ROLLING 24h, derived from the DB so it is durable
# AND process-global: running the apply twice in a day can't blow the real ceiling.
# Counts only submissions whose EFFECTIVE apply host is LinkedIn (offsite redirects
# do NOT count). 0 = no cap. Conservative default 20 -- well under LinkedIn's ~30/day
# soft throttle; raise once a first real run shows headroom.
LINKEDIN_DAILY_CAP = int(os.environ.get("APPLYPILOT_LINKEDIN_DAILY_CAP", "20"))
# Same-day hard stop for the LinkedIn lane after a pause/challenge/auth failure:
# stop ACQUIRING LinkedIn-lane jobs (no retry-storm) while the offsite lane keeps
# flowing. Tripped in worker_loop, read in acquire_job. Per-process (one run).
_linkedin_halt = threading.Event()
_li_cap_announced = threading.Event()
LINKEDIN_PAUSE_REASONS = {
    "linkedin_rate_limited", "linkedin_challenge", "linkedin_pause",
    "rate_limited", "captcha", "login_issue", "auth_required",
}
# Effective-apply-host LinkedIn test, shared by the cap COUNT and the acquire
# exclusion clause: a real http application_url (offsite ATS) wins over the url.
_LINKEDIN_LANE_SQL = (
    "(CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END) "
    "LIKE '%linkedin.com%'"
)


def _linkedin_today(conn) -> int:
    """Rolling-24h count of applied jobs whose effective apply host is LinkedIn."""
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    return conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE apply_status = 'applied' "
        f"AND applied_at >= ? AND {_LINKEDIN_LANE_SQL}",
        (since,),
    ).fetchone()[0]


# --- Offsite per-host circuit breaker -----------------------------------------
from applypilot.discovery.resilience import get_board_breaker  # noqa: E402

# After this many CONSECUTIVE host-fault failures on one offsite host, trip it out
# of the run: the host is excluded from acquisition for the rest of the run so the
# worker stops wasting launches on a flaky/blocking ATS. This is the offsite
# analogue of the LinkedIn same-day halt; LinkedIn is EXEMPT (it has its own halt).
# Failure counting is shared across workers via the resilience registry.
_HOST_BREAKER_THRESHOLD = int(os.environ.get("APPLYPILOT_HOST_BREAKER_THRESHOLD") or 3)
_HOST_BREAKER_RESET = 120.0
HOST_FAULT_REASONS = {
    "page_error", "timeout", "stuck", "suspicious_page",
    "cloudflare_blocked", "blocked_by_cloudflare", "site_blocked",
    "rate_limited", "auth_required", "login_issue",
}
_offsite_halted_hosts: set[str] = set()
_host_halt_lock = threading.Lock()


def _throttle_host(url: str) -> str:
    from urllib.parse import urlparse
    h = (urlparse(url or "").hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _host_gap(host: str) -> float:
    """Per-host minimum gap; LinkedIn gets a wider one for account safety."""
    return LINKEDIN_HOST_GAP if "linkedin" in host else APPLY_HOST_GAP


def _apply_target(job: dict) -> str:
    """The URL the apply actually hits: the external application_url when it is a
    real http link, else the source url. Pacing keys on this so a LinkedIn job
    whose apply redirects OFFSITE throttles by the external ATS host (fast,
    unrestricted), while only true Easy-Apply / unresolved LinkedIn jobs get the
    wider LinkedIn gap. Activates the Easy-Apply-vs-offsite split as soon as the
    extractor backfills companyApplyUrl into application_url."""
    au = job.get("application_url") or ""
    return au if au.startswith("http") else (job.get("url") or "")


def _throttle_before_apply(url: str) -> None:
    """Wait out the per-host minimum gap before hitting the same host again."""
    host = _throttle_host(url)
    gap = _host_gap(host)
    if not host or gap <= 0:
        return
    if GAP_JITTER_HI > GAP_JITTER_LO > 0:
        gap *= random.uniform(GAP_JITTER_LO, GAP_JITTER_HI)
    with _throttle_lock:
        last = _last_apply_by_host.get(host, 0.0)
    wait = gap - (time.monotonic() - last)
    if wait > 0:
        _stop_event.wait(timeout=wait)


def _throttle_after_apply(url: str) -> None:
    """Record the apply time and sleep a randomized inter-job delay so the
    submission cadence isn't robotic. Interruptible via the stop event."""
    host = _throttle_host(url)
    with _throttle_lock:
        _last_apply_by_host[host] = time.monotonic()
    hi = max(APPLY_MIN_DELAY, APPLY_MAX_DELAY)
    if hi > 0:
        _stop_event.wait(timeout=random.uniform(min(APPLY_MIN_DELAY, APPLY_MAX_DELAY), hi))


def _update_host_breaker(job: dict, ok: bool, reason: str | None, worker_id: int) -> None:
    """Offsite per-host circuit breaker. A clean apply (or a job-specific failure
    that proves the host responded, e.g. not_eligible_salary) resets the host's
    breaker; a HOST-fault failure (page_error/timeout/cloudflare/...) increments it.
    After _HOST_BREAKER_THRESHOLD consecutive host-faults the host is halted for the
    rest of the run. No-op for LinkedIn (it has the dedicated same-day halt)."""
    host = _throttle_host(_apply_target(job))
    if not host or "linkedin" in host:
        return
    breaker = get_board_breaker(
        f"apply:{host}",
        failure_threshold=_HOST_BREAKER_THRESHOLD,
        reset_timeout=_HOST_BREAKER_RESET,
    )
    if ok or (reason not in HOST_FAULT_REASONS):
        breaker.record_success()
        return
    breaker.record_failure()
    if breaker.is_open():
        with _host_halt_lock:
            newly = host not in _offsite_halted_hosts
            _offsite_halted_hosts.add(host)
        if newly:
            add_event(f"[W{worker_id}] Host {host} faulting ({reason}) -- skipping it for the rest of this run")


def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str = "sonnet", dry_run: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Claude model name.
        dry_run: Don't click Submit.

    Returns:
        Tuple of (applied_count, failed_count).
    """
    applied = 0
    failed = 0
    continuous = limit == 0
    jobs_done = 0
    empty_polls = 0
    port = BASE_CDP_PORT + worker_id
    # LinkedIn daily cap read at runtime so a --linkedin-daily-cap CLI flag (env set
    # after import) is honored; falls back to the module default. 0 = no cap.
    li_cap = int(os.environ.get("APPLYPILOT_LINKEDIN_DAILY_CAP") or LINKEDIN_DAILY_CAP)
    # URLs attempted this run -> excluded from re-acquisition so a fast-failing job
    # can't be re-picked and burn the whole --limit on one job (canary surfaced this).
    attempted_urls: set[str] = set()

    while not _stop_event.is_set():
        if not continuous and jobs_done >= limit:
            break

        update_state(worker_id, status="idle", job_title="", company="",
                     last_action="waiting for job", actions=0)

        # LinkedIn lane gating: stop ACQUIRING LinkedIn Easy-Apply jobs once a
        # pause/challenge tripped the halt or the rolling-24h cap is hit -- the
        # offsite ATS lane keeps flowing either way. Skipped for single-URL applies.
        exclude_li = False
        if not target_url:
            exclude_li = _linkedin_halt.is_set()
            if not exclude_li and li_cap > 0:
                try:
                    if _linkedin_today(get_connection()) >= li_cap:
                        exclude_li = True
                        if not _li_cap_announced.is_set():
                            _li_cap_announced.set()
                            add_event(f"[W{worker_id}] LinkedIn daily cap "
                                      f"{li_cap} reached -- continuing offsite lane only")
                except Exception:
                    logger.debug("LinkedIn cap check failed", exc_info=True)

        _excl_hosts = None
        if not target_url:
            with _host_halt_lock:
                _excl_hosts = set(_offsite_halted_hosts) or None

        job = acquire_job(target_url=target_url, min_score=min_score,
                          worker_id=worker_id, exclude_linkedin=exclude_li,
                          exclude_urls=attempted_urls if not target_url else None,
                          exclude_hosts=_excl_hosts)
        if not job:
            if not continuous:
                add_event(f"[W{worker_id}] Queue empty")
                update_state(worker_id, status="done", last_action="queue empty")
                break
            empty_polls += 1
            update_state(worker_id, status="idle",
                         last_action=f"polling ({empty_polls})")
            if empty_polls == 1:
                add_event(f"[W{worker_id}] Queue empty, polling every {POLL_INTERVAL}s...")
            # Use Event.wait for interruptible sleep
            if _stop_event.wait(timeout=POLL_INTERVAL):
                break  # Stop was requested during wait
            continue

        empty_polls = 0
        attempted_urls.add(job["url"])

        # Fail-fast pre-launch guard: never spend a Chrome launch + agent run on a
        # job with no valid http(s) apply target. Mark permanent so it's not re-acquired.
        if not dry_run:
            from urllib.parse import urlparse as _urlparse
            _pp = _urlparse(_apply_target(job))
            if _pp.scheme not in ("http", "https") or not _pp.netloc:
                mark_result(job["url"], "failed", "bad_application_url", permanent=True)
                failed += 1
                update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed)
                add_event(f"[W{worker_id}] Skip (no valid apply URL): {job['title'][:30]}")
                jobs_done += 1
                continue

        # Account-safety throttle: respect the per-host gap before launching
        # (skipped in dry-run so canary tests stay fast).
        if not dry_run:
            _throttle_before_apply(_apply_target(job))
            if _stop_event.is_set():
                release_lock(job["url"])
                break

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run)

            if result == "skipped":
                release_lock(job["url"])
                add_event(f"[W{worker_id}] Skipped: {job['title'][:30]}")
                continue
            elif dry_run:
                # Dry run NEVER writes apply_status -- just release the lease so
                # the job stays available for a real apply later. This is the
                # load-bearing guard: even if the agent mistakenly emitted
                # RESULT:APPLIED, we do not record it as applied.
                release_lock(job["url"])
                if result in ("dry_run", "applied"):
                    applied += 1
                    add_event(f"[W{worker_id}] DRY-RUN OK: {job['title'][:30]}")
                else:
                    failed += 1
                update_state(worker_id, jobs_applied=applied, jobs_failed=failed,
                             jobs_done=applied + failed)
            elif result == "applied":
                mark_result(job["url"], "applied", duration_ms=duration_ms)
                applied += 1
                update_state(worker_id, jobs_applied=applied,
                             jobs_done=applied + failed)
                _update_host_breaker(job, ok=True, reason=None, worker_id=worker_id)
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)
                # Same-day LinkedIn halt: a pause/challenge/auth failure on a
                # LinkedIn-lane job stops the LinkedIn lane for the rest of this run
                # (no retry-storm); the offsite lane keeps going.
                if (reason in LINKEDIN_PAUSE_REASONS
                        and "linkedin" in _throttle_host(_apply_target(job))
                        and not _linkedin_halt.is_set()):
                    _linkedin_halt.set()
                    add_event(f"[W{worker_id}] LinkedIn {reason} -- halting LinkedIn "
                              f"lane for this run; offsite continues")
                # Offsite per-host circuit breaker (no-op for LinkedIn).
                _update_host_breaker(job, ok=False, reason=reason, worker_id=worker_id)

        except KeyboardInterrupt:
            release_lock(job["url"])
            if _stop_event.is_set():
                break
            add_event(f"[W{worker_id}] Job skipped (Ctrl+C)")
            continue
        except Exception as e:
            logger.exception("Worker %d launcher error", worker_id)
            add_event(f"[W{worker_id}] Launcher error: {str(e)[:40]}")
            release_lock(job["url"])
            failed += 1
            update_state(worker_id, jobs_failed=failed)
        finally:
            if chrome_proc:
                cleanup_worker(worker_id, chrome_proc)

        # Cost-budget guard: stop the whole run once accumulated apply cost hits
        # the cap (APPLYPILOT_APPLY_MAX_COST / --max-cost-usd; 0 = no cap). Read at
        # runtime so the CLI flag (set after import) is honored.
        _maxc = float(os.environ.get("APPLYPILOT_APPLY_MAX_COST") or 0)
        if _maxc > 0 and get_totals().get("cost", 0) >= _maxc:
            add_event(f"[W{worker_id}] Cost budget ${_maxc:.2f} reached -- stopping run")
            _stop_event.set()
            break

        # Randomized inter-job delay after a real submission (account safety).
        if not dry_run and not target_url:
            _throttle_after_apply(_apply_target(job))

        jobs_done += 1
        if target_url:
            break

    update_state(worker_id, status="done", last_action="finished")
    return applied, failed


# ---------------------------------------------------------------------------
# Main entry point (called from cli.py)
# ---------------------------------------------------------------------------

def main(limit: int = 1, target_url: str | None = None,
         min_score: int = 7, headless: bool = False, model: str = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Claude model name.
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
    """
    global POLL_INTERVAL
    POLL_INTERVAL = int(os.environ.get("APPLYPILOT_POLL_INTERVAL") or poll_interval)
    _stop_event.clear()
    # Reset per-run lane-gating state (these module-level objects persist across
    # main() calls within one process, e.g. tests / a wrapper that re-invokes).
    _linkedin_halt.clear()
    _li_cap_announced.clear()
    with _host_halt_lock:
        _offsite_halted_hosts.clear()

    config.ensure_dirs()
    console = Console()

    # Reclaim any leases stranded 'in_progress' by a previous run that was
    # hard-killed mid-job, so those jobs become eligible again.
    try:
        reclaimed = reclaim_stale_leases()
        if reclaimed:
            console.print(f"[dim]Reclaimed {reclaimed} stale in-progress lease(s) from a previous run[/dim]")
    except Exception:
        logger.debug("Stale lease reclaim failed", exc_info=True)

    # Periodic lease reclaim DURING the run: a worker that hard-crashes mid-job
    # leaves its job 'in_progress' (invisible to acquire) until the next startup.
    # Reclaim every 5 min so an orphan recovers within one TTL window, not 24h later.
    def _periodic_reclaim() -> None:
        while not _stop_event.wait(timeout=300):
            try:
                n = reclaim_stale_leases()
                if n:
                    add_event(f"[reclaim] recovered {n} stale in-progress lease(s) mid-run")
            except Exception:
                logger.debug("Periodic lease reclaim failed", exc_info=True)
    threading.Thread(target=_periodic_reclaim, daemon=True, name="lease-reclaim").start()

    if continuous:
        effective_limit = 0
        mode_label = "continuous"
    else:
        effective_limit = limit
        mode_label = f"{limit} jobs"

    # Initialize dashboard for all workers
    for i in range(workers):
        init_worker(i)

    worker_label = f"{workers} worker{'s' if workers > 1 else ''}"
    console.print(f"Launching apply pipeline ({mode_label}, {worker_label}, poll every {POLL_INTERVAL}s)...")
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active Claude processes to skip current jobs
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _claude_lock:
                for wid, cproc in list(_claude_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
            kill_all_chrome()
            raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint_handler)

    try:
        with Live(render_full(), console=console, refresh_per_second=2) as live:
            # Daemon thread for display refresh only (no business logic)
            _dashboard_running = True

            def _refresh():
                while _dashboard_running:
                    live.update(render_full())
                    time.sleep(0.5)

            refresh_thread = threading.Thread(target=_refresh, daemon=True)
            refresh_thread.start()

            if workers == 1:
                # Single worker — run directly in main thread
                total_applied, total_failed = worker_loop(
                    worker_id=0,
                    limit=effective_limit,
                    target_url=target_url,
                    min_score=min_score,
                    headless=headless,
                    model=model,
                    dry_run=dry_run,
                )
            else:
                # Multi-worker — distribute limit across workers
                if effective_limit:
                    base = effective_limit // workers
                    extra = effective_limit % workers
                    limits = [base + (1 if i < extra else 0)
                              for i in range(workers)]
                else:
                    limits = [0] * workers  # continuous mode

                with ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="apply-worker") as executor:
                    futures = {
                        executor.submit(
                            worker_loop,
                            worker_id=i,
                            limit=limits[i],
                            target_url=target_url,
                            min_score=min_score,
                            headless=headless,
                            model=model,
                            dry_run=dry_run,
                        ): i
                        for i in range(workers)
                    }

                    results: list[tuple[int, int]] = []
                    for future in as_completed(futures):
                        wid = futures[future]
                        try:
                            results.append(future.result())
                        except Exception:
                            logger.exception("Worker %d crashed", wid)
                            results.append((0, 0))

                total_applied = sum(r[0] for r in results)
                total_failed = sum(r[1] for r in results)

            _dashboard_running = False
            refresh_thread.join(timeout=2)
            live.update(render_full())

        totals = get_totals()
        console.print(
            f"\n[bold]Done: {total_applied} applied, {total_failed} failed "
            f"(${totals['cost']:.3f})[/bold]"
        )
        # Auto-surfaced run summary: funnel + applied-by-channel + top hosts +
        # fail reasons, so a large run ends with a real report, not just "Done".
        try:
            from applypilot.database import get_apply_analytics, get_connection
            a = get_apply_analytics()
            sr = a.get("success_rate")
            console.print(
                f"[bold]Apply funnel:[/bold] {a['applied']} applied / {a['attempted']} attempted"
                + (f" ({sr * 100:.0f}% success)" if sr is not None else "")
            )
            ch = get_connection().execute(
                "SELECT channel, COUNT(*) FROM applications WHERE status='applied' "
                "GROUP BY channel ORDER BY 2 DESC"
            ).fetchall()
            if ch:
                console.print("  applied by channel: " + ", ".join(f"{c[0] or '?'}={c[1]}" for c in ch))
            if a.get("by_site"):
                console.print("  top hosts: " + ", ".join(
                    f"{s['site']}({s['applied']}a/{s['failed']}f)" for s in a["by_site"][:6]))
            if a.get("fail_reasons"):
                console.print("  top fail reasons: " + ", ".join(
                    f"{r['reason']}={r['count']}" for r in a["fail_reasons"][:6]))
        except Exception:
            logger.debug("Run-summary analytics failed", exc_info=True)
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    finally:
        _stop_event.set()
        kill_all_chrome()
