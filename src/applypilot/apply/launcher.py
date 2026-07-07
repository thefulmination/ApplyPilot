"""Apply orchestration: acquire jobs, spawn AI agent sessions, track results.

This is the main entry point for the apply pipeline. It pulls jobs from
the database, launches Chrome + an apply agent for each one, parses the
result, and updates the database. Supports parallel workers via --workers.
"""

import atexit
import hashlib
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
from applypilot import tenants as tenants_mod
from applypilot.applications import record_application
from applypilot.database import get_connection
from applypilot.apply import prompt as prompt_mod
from applypilot import inbox_auth
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

_FLEET_LI_KEY = "applypilot:linkedin_driver"


def fleet_linkedin_active(pg_dsn: str | None) -> bool:
    """Probe whether the fleet LinkedIn driver holds the advisory interlock.

    Opens a transient connection to the fleet PG and attempts
    ``pg_try_advisory_lock(hashtext('applypilot:linkedin_driver'))``.

    * If the try-lock returns FALSE (we could NOT acquire because the fleet
      holds it) → returns **True** (fleet is active; supervised must defer).
    * If the try-lock returns TRUE (we acquired it) → immediately unlocks
      (non-destructive probe, never leaves the lock held) and returns **False**.
    * Any error (missing/unreachable fleet PG, connect failure, etc.) →
      returns **False** so a probe failure never crashes the supervised run.
      The runbook is the backstop for that edge.
    """
    if not pg_dsn:
        return False
    conn = None
    try:
        import psycopg  # lazy import — fleet PG may not be present
        conn = psycopg.connect(pg_dsn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_try_advisory_lock(hashtext(%s)) AS acquired",
                (_FLEET_LI_KEY,),
            )
            row = cur.fetchone()
            acquired = row[0] if row else True
        if acquired:
            # We grabbed it — fleet does NOT hold it; release and report inactive.
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT pg_advisory_unlock(hashtext(%s))",
                    (_FLEET_LI_KEY,),
                )
            conn.commit()
            return False
        else:
            # Could not acquire — fleet IS holding the lock.
            return True
    except Exception:
        logger.debug("fleet_linkedin_active probe failed", exc_info=True)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


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


def _load_blocked_companies():
    from applypilot.config import load_blocked_companies
    return load_blocked_companies()


def _company_blocked(row, names: set[str], patterns: list[str]) -> bool:
    company = (row["company"] or "").strip().lower()
    if company and company in names:
        return True
    url = (row["url"] or "").lower()
    application_url = (row["application_url"] or "").lower()
    for pattern in patterns:
        needle = pattern.strip("%").lower()
        if needle and (needle in url or needle in application_url):
            return True
    return False


def _company_blocklist_clause(names: set[str], patterns: list[str]) -> tuple[str, list]:
    clauses: list[str] = []
    params: list = []
    if names:
        placeholders = ",".join("?" * len(names))
        clauses.append(f"LOWER(TRIM(COALESCE(company,''))) NOT IN ({placeholders})")
        params.extend(sorted(names))
    for pattern in patterns:
        clauses.append("url NOT LIKE ?")
        clauses.append("COALESCE(application_url,'') NOT LIKE ?")
        params.extend([pattern, pattern])
    if not clauses:
        return "", []
    return "AND " + "\n                      AND ".join(clauses), params


# How often to poll the DB when the queue is empty (seconds). Tunable via
# APPLYPILOT_POLL_INTERVAL / --poll-interval (lower = a worker idles less between
# empty polls; only matters once the queue drains).
POLL_INTERVAL = int(os.environ.get("APPLYPILOT_POLL_INTERVAL") or config.DEFAULTS["poll_interval"])

# Wall-clock budget for a single apply agent run. The stdout read loop runs in a
# daemon thread; if it does not finish within this many seconds the agent
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

# Track active apply-agent processes for skip (Ctrl+C) handling
_agent_procs: dict[int, subprocess.Popen] = {}
# Last apply-agent run stats per worker (cost_usd / tokens). run_job records cost to the
# home SQLite (llm_usage); the cloud fleet has no SQLite, so the container worker reads the
# real per-job cost from here to write into Postgres (drives the spend cap). Home unaffected.
_last_run_stats: dict[int, dict] = {}
_agent_lock = threading.Lock()

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


def _normalize_agent(agent: str | None) -> str:
    normalized = (agent or "claude").strip().lower()
    if normalized not in {"claude", "codex"}:
        raise ValueError("agent must be either 'claude' or 'codex'")
    return normalized


# Claude model TIER names. The fleet/CLI default --model is "sonnet" (a Claude tier),
# and it flows into build_apply_agent_command for BOTH agents. Codex does not know
# these names: `codex exec --model sonnet` fails the turn with "Model metadata for
# `sonnet` not found" / "'sonnet' model is not supported", so Codex emits an error +
# turn.failed and never prints a RESULT: line -> the parser returns no_result_line.
# Strip them so Codex uses its own default model (the documented intent: "codex uses
# its own default"). A genuine Codex model (e.g. "gpt-5-codex") is still passed through.
_CLAUDE_MODEL_TIERS = {"sonnet", "opus", "haiku"}
_CODEX_REASONING_EFFORTS = {"low", "medium", "high", "xhigh"}


def _codex_effective_model(model: str | None) -> str | None:
    override = (os.environ.get("APPLYPILOT_CODEX_MODEL") or "").strip()
    if override:
        return override
    if model and model.strip().lower() not in _CLAUDE_MODEL_TIERS:
        return model
    return None


def _codex_model_args(model: str | None) -> list[str]:
    """--model args for Codex, dropping Claude tier names so Codex uses its default."""
    effective = _codex_effective_model(model)
    if effective:
        return ["--model", effective]
    return []


def _codex_reasoning_args() -> list[str]:
    effort = (os.environ.get("APPLYPILOT_CODEX_REASONING_EFFORT") or "").strip().lower()
    if not effort:
        return []
    if effort not in _CODEX_REASONING_EFFORTS:
        raise ValueError(
            "APPLYPILOT_CODEX_REASONING_EFFORT must be one of "
            f"{', '.join(sorted(_CODEX_REASONING_EFFORTS))}"
        )
    return ["-c", f"model_reasoning_effort={json.dumps(effort)}"]


def _codex_mcp_config_args(cdp_port: int) -> list[str]:
    """Return Codex -c overrides for the per-worker Playwright MCP server."""
    playwright_args = [
        "@playwright/mcp@0.0.76",
        f"--cdp-endpoint=http://localhost:{cdp_port}",
        f"--viewport-size={config.DEFAULTS['viewport']}",
    ]
    overrides = [
        "approval_policy=\"never\"",
        "mcp_servers.playwright.command=\"npx\"",
        f"mcp_servers.playwright.args={json.dumps(playwright_args)}",
        "mcp_servers.playwright.default_tools_approval_mode=\"approve\"",
        "mcp_servers.playwright.required=true",
    ]
    if os.environ.get("APPLYPILOT_ENABLE_GMAIL_MCP", "").lower() in {"1", "true", "yes", "on"}:
        overrides.extend([
            "mcp_servers.gmail.command=\"npx\"",
            f"mcp_servers.gmail.args={json.dumps(['-y', '@gongrzhe/server-gmail-autoauth-mcp'])}",
            "mcp_servers.gmail.default_tools_approval_mode=\"approve\"",
        ])

    args: list[str] = []
    for override in overrides:
        args.extend(["-c", override])
    return args


def _codex_skill_config_args() -> list[str]:
    """Disable always-on local workflow skills for this narrow browser agent."""
    skill_paths = [
        Path.home() / ".agents/skills/using-superpowers/SKILL.md",
        Path.home() / ".codex/skills/using-superpowers/SKILL.md",
    ]
    entries = ",".join(
        f"{{path={json.dumps(str(path))},enabled=false}}"
        for path in skill_paths
    )
    return ["-c", f"skills.config=[{entries}]"]


def _codex_isolation_args() -> list[str]:
    """Keep Codex apply sessions narrow and avoid loading unrelated local state."""
    return [
        "--ignore-user-config",
        "--ignore-rules",
        "--disable", "plugins",
        "--disable", "apps",
        "--disable", "memories",
        *_codex_skill_config_args(),
    ]


def build_apply_agent_command(
    *,
    agent: str = "claude",
    model: str | None = "sonnet",
    mcp_config_path: Path,
    cdp_port: int,
) -> list[str]:
    """Build the non-interactive command for one apply-agent run."""
    agent = _normalize_agent(agent)
    if agent == "claude":
        model = model or "sonnet"
        return [
            config.get_claude_path(),
            "--model", model,
            "-p",
            # Hard per-apply $ ceiling: kills runaway sessions (a captcha flail measured 84
            # turns / 8.5 min / $3.70 self-reported). EEO-heavy forms were failing mid-submit
            # at the old $2.00 cap; $3.50 covers them while staying under the flail threshold.
            "--max-budget-usd", os.environ.get("APPLYPILOT_MAX_BUDGET_USD", "3.5"),
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
                "Bash,BashOutput,KillShell,Read,Write,Edit,NotebookEdit,"
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

    model_args = _codex_model_args(model)
    return [
        config.get_codex_path(),
        "exec",
        *model_args,
        *_codex_reasoning_args(),
        "--json",
        *_codex_isolation_args(),
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        *_codex_mcp_config_args(cdp_port),
        "-",
    ]


def build_agent_canary_command(agent: str, model: str | None) -> list[str]:
    """Build a cheap auth canary command for the selected apply agent."""
    agent = _normalize_agent(agent)
    prompt = "Reply with the single word READY."
    if agent == "claude":
        model = model or "sonnet"
        return [
            config.get_claude_path(),
            "--model", model,
            "-p",
            "--no-session-persistence",
            prompt,
        ]
    model_args = _codex_model_args(model)
    return [
        config.get_codex_path(),
        "exec",
        *model_args,
        *_codex_reasoning_args(),
        *_codex_isolation_args(),
        "--ephemeral",
        "--sandbox", "read-only",
        "--skip-git-repo-check",
        prompt,
    ]


# ---------------------------------------------------------------------------
# Near-duplicate reposting guard
# ---------------------------------------------------------------------------
# A company sometimes posts the SAME role twice with slightly different titles AND
# different ATS job IDs -- e.g. Amae Health "Founder Associate, Growth & Partnership
# Operations" (greenhouse job 4259921009) and "Business Development Associate, Growth
# & Partnership Operations" (greenhouse job 4288094009), both on greenhouse.io/
# amaehealth -> the applicant gets two confirmation emails for what's effectively one
# role. Exact (company,title) + effective-url dedup MISS this: both the title and the
# job ID differ. The reliable shared signal is the EMPLOYER's ATS board slug
# (greenhouse.io/amaehealth), which both postings share and which generic aggregators
# (chiefofstaffjob.com, hiring.cafe, linkedin) do NOT encode. So: if a candidate is on
# the same employer board as an already-applied role AND their titles share enough
# significant tokens, treat it as a re-post and skip it.
NEAR_DUP_MIN_SHARED = int(os.environ.get("APPLYPILOT_NEAR_DUP_MIN_SHARED_TOKENS") or 3)
# Two titles at the SAME employer are the same role re-listed (a duplicate) only when
# their significant-token sets are >= this Jaccard-similar. 0.55 catches Amae ("Founder
# Associate, Growth & Partnership Operations" vs "Business Development Associate, ..."
# -> 0.57) and identical re-lists (1.0), while RELEASING genuinely DIFFERENT roles at
# one company ("Pricing Strategy & Operations" vs "GTM Strategy & Operations" -> 0.50;
# "BD Associate" vs "BD Representative" -> 0.50) -- different roles are NOT duplicates,
# so apply to both. A token COUNT can't separate these (a 3-word identical title and a
# 5-word different-role title can share the same count); the ratio can. 0 disables it.
NEAR_DUP_JACCARD = float(os.environ.get("APPLYPILOT_NEAR_DUP_JACCARD") or 0.55)
# ATS hosts whose FIRST PATH segment reliably identifies the employer
# (greenhouse.io/<company>/jobs/<id>). Restricted on purpose: hosts that put the
# employer in a SUBDOMAIN (workday/bamboohr/recruitee/teamtailor/pinpoint) or in an
# inconsistent path (workable /view) are EXCLUDED -- a wrong slug there could falsely
# merge two different companies. Those are covered by the company field instead (see
# _same_employer). Generic boards (linkedin/indeed/hiring.cafe/...) never encode an
# employer in the URL, so a slug match there can't falsely merge distinct companies.
_EMPLOYER_BOARD_HOSTS = (
    "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
    "jobvite.com", "rippling.com",
)
# Path segments that are board routing words, NOT the employer -> skipped when
# extracting the employer slug (so we never read 'jobs'/'view'/'apply' as a company).
_BOARD_PATH_NOISE = frozenset({
    "jobs", "job", "careers", "career", "o", "embed", "view", "j", "p", "apply",
    "en", "en-us", "us", "search", "listing", "listings", "opening", "openings",
    "position", "positions", "role", "roles", "vacancy", "vacancies",
})
# Stopwords + employment-type/location boilerplate that aggregator titles tack on, so
# token overlap reflects the actual ROLE, not "Full Time United States" noise.
_TITLE_STOP = frozenset({
    "of", "the", "to", "a", "an", "and", "for", "in", "at", "with", "or", "on", "by",
    "full", "part", "time", "remote", "hybrid", "onsite", "contract", "intern",
    "internship", "united", "states", "us", "usa", "new", "york", "san", "ny", "ca",
    "sf", "senior", "sr", "junior", "jr",
    # Region / locale tokens are pure noise -- they must not bridge two different roles
    # (Asana "Marketing Operations Manager, AMER" vs "Head of Revenue Strategy & Ops AMER").
    "amer", "emea", "apac", "anz", "latam", "americas", "america", "north", "global",
    "international", "worldwide", "ww",
})
# The aggregator pseudo-company / generic-board names are NOT real employers, so they
# must never be used as an employer identity (they'd merge many distinct companies).
_NON_EMPLOYER_COMPANIES = frozenset({"chiefofstaffjob.com", "hiringcafe", "linkedin"})


def _employer_board_slug(url: str | None) -> str | None:
    """'baseATS/employer' for boards that encode the employer in the FIRST path segment
    (greenhouse.io/amaehealth). None for subdomain-employer boards, generic aggregators,
    or unknown hosts -- so a board-slug match never falsely merges distinct companies."""
    from urllib.parse import urlparse
    u = urlparse(url or "")
    host = (u.hostname or "").lower()
    host = host[4:] if host.startswith("www.") else host
    # Normalize to the base ATS domain so subdomain variants (boards. vs job-boards.
    # greenhouse.io) collapse to one key.
    base = next((h for h in _EMPLOYER_BOARD_HOSTS if host == h or host.endswith("." + h)), None)
    if not base:
        return None
    parts = [s for s in (u.path or "").split("/") if s and s.lower() not in _BOARD_PATH_NOISE]
    return f"{base}/{parts[0].lower()}" if parts else None


def _norm_company(company: str | None) -> str:
    """Normalized employer name, or '' for empty / the aggregator pseudo-company."""
    co = (company or "").strip().lower()
    return "" if co in _NON_EMPLOYER_COMPANIES else co


def _same_employer(url_a, co_a, url_b, co_b) -> bool:
    """True if two postings are from the same EMPLOYER -- matched by real company name
    (robust, no URL parsing) OR by a reliable path-employer ATS board slug. Either
    signal alone suffices, so e.g. a greenhouse row and a LinkedIn row for one company
    still match via the company field."""
    ca, cb = _norm_company(co_a), _norm_company(co_b)
    if ca and ca == cb:
        return True
    sa, sb = _employer_board_slug(url_a), _employer_board_slug(url_b)
    return bool(sa and sa == sb)


def _sig_title_tokens(title: str | None) -> set[str]:
    """Significant role tokens of a title (drop stopwords, boilerplate, digits)."""
    return {
        w for w in re.split(r"[^a-z0-9]+", (title or "").lower())
        if w and len(w) > 1 and not w.isdigit() and w not in _TITLE_STOP
    }


def _title_jaccard(a: str | None, b: str | None) -> float:
    """Jaccard similarity (0..1) of two titles' significant role tokens. 1.0 = same
    role words; ~0.55+ = a re-list of the same role; <0.5 = a different role."""
    ta, tb = _sig_title_tokens(a), _sig_title_tokens(b)
    return len(ta & tb) / len(ta | tb) if (ta | tb) else 0.0


def _find_near_duplicate_applied(conn, apply_url: str, title: str,
                                 company: str | None = None) -> str | None:
    """If an already-applied role is the SAME ROLE re-listed -- same EMPLOYER (company OR
    reliable board slug) AND title Jaccard >= NEAR_DUP_JACCARD -- return its title, else
    None. Different roles at one company (low similarity) are NOT duplicates and are
    never flagged; an unidentifiable employer or a 1-word generic title never triggers
    it either."""
    if NEAR_DUP_JACCARD <= 0:
        return None
    if not (_norm_company(company) or _employer_board_slug(apply_url)):
        return None  # can't identify the employer -> don't risk a false near-dup skip
    if len(_sig_title_tokens(title)) < 2:
        return None  # too generic a title to judge
    for r in conn.execute(
        "SELECT title, company, COALESCE(application_url, url) AS tgt "
        "FROM jobs WHERE apply_status = 'applied'"
    ):
        if not _same_employer(apply_url, company, r["tgt"], r["company"]):
            continue
        if _title_jaccard(title, r["title"]) >= NEAR_DUP_JACCARD:
            return r["title"]
    return None


def audit_duplicate_applications(conn=None) -> list[dict]:
    """Report likely DUPLICATE applications already in the DB: pairs of applied jobs
    that are the same role re-listed -- same employer (ATS board slug, or same real
    non-aggregator company) with the same effective target OR overlapping titles. This
    surfaces near-duplicate company repostings (different job ID + tweaked title) that
    exact dedup can't catch -- the monitor for the 'why is it double-applying' question.
    Read-only."""
    conn = conn or get_connection()
    # Build dicts from cursor.description so this works whether or not the caller's
    # connection has row_factory=sqlite3.Row, and without mutating it (zip() handles both
    # plain tuples and sqlite3.Row, which is iterable).
    cur = conn.execute(
        "SELECT url, title, company, COALESCE(application_url, url) AS tgt, applied_at "
        "FROM jobs WHERE apply_status = 'applied'")
    cols = [d[0] for d in cur.description]
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    out: list[dict] = []
    for i in range(len(rows)):
        for j in range(i + 1, len(rows)):
            a, b = rows[i], rows[j]
            if not _same_employer(a["tgt"], a["company"], b["tgt"], b["company"]):
                continue
            aco = _norm_company(a["company"])
            if a["tgt"] == b["tgt"]:
                kind, shared = "exact-target", []
            else:
                if _title_jaccard(a["title"], b["title"]) < NEAR_DUP_JACCARD:
                    continue
                shared = sorted(_sig_title_tokens(a["title"]) & _sig_title_tokens(b["title"]))
                kind = "near-duplicate"
            out.append({
                "kind": kind, "employer": _employer_board_slug(a["tgt"]) or aco,
                "title_a": a["title"], "title_b": b["title"],
                "url_a": a["url"], "url_b": b["url"],
                "applied_a": a["applied_at"], "applied_b": b["applied_at"],
                "shared_tokens": shared,
            })
    return out


# ---------------------------------------------------------------------------
# Database operations
# ---------------------------------------------------------------------------

def _tenant_bypasses_auth_gate(conn, apply_url: str) -> bool:
    """True if `apply_url`'s host is an eligible ats_tenants tenant for THIS
    run and should be let through the auth-gate skip instead of parked.

    HOME LANE ONLY -- this is consulted solely from acquire_job's auth-gated
    skip block. apply/fleet_sync.py's auth-gated exclusion is untouched by
    this helper and must never call it: fleet workers must never receive
    auth-gated jobs regardless of tenant status.

    Mode (APPLYPILOT_AUTH_GATED_MODE) scopes which tenant statuses are
    eligible this run:
      - "supervised" -> {supervised, trusted} (a human-supervised run)
      - "trusted" or unset/empty -> {trusted} only (the safe default for a
        normal unattended home run: supervised tenants NEVER apply
        unattended)

    Beyond status+mode eligibility, the tenant must not be halted and must
    be under its configured daily_cap.
    """
    host = tenants_mod._host_of(apply_url)
    if not host:
        return False

    mode = os.environ.get("APPLYPILOT_AUTH_GATED_MODE", "").strip().lower()
    eligible_statuses = {"supervised", "trusted"} if mode == "supervised" else {"trusted"}

    status = tenants_mod.tenant_status(conn, host)
    if status not in eligible_statuses:
        return False

    now_iso = datetime.now(timezone.utc).isoformat()
    if tenants_mod.is_halted(conn, host, now_iso):
        return False

    if tenants_mod.submits_today(conn, host) >= tenants_mod.daily_cap(conn, host):
        return False

    return True


def record_tenant_outcome(conn, apply_url: str, status: str) -> None:
    """Record the REAL terminal outcome of a supervised apply run against the
    tenant registry, exactly once, from the run's actual terminal status.

    Owner decision 2026-07-03 (spec amendment 0b2fead): supervised mode is a
    full headed apply -- the agent submits and the owner watches + can
    Ctrl-C -- there is no pause-and-confirm checkpoint anymore. This replaces
    the old confirm-before-submit gate (commit 6565936), which had a CRITICAL
    bug: it called tenants.record_submit(ok=True) on the owner's "y"
    keystroke BEFORE any real submit happened, inflating clean_submits toward
    "trusted" without a real apply ever occurring. This function fixes that
    by construction -- it is only ever called AFTER the run's real terminal
    result is known, and ok is derived from that real status.

    Args:
        conn: Database connection to pass through to tenants.record_submit.
        apply_url: The job's apply URL; the tenant host is derived from it.
        status: The run's terminal status string (e.g. "applied",
            "expired", "failed:no_confirmation", "failed:timeout").
            ok=True only when status == "applied".

    Exception-guarded: a registry write failure must never crash the apply
    run, so any error here is swallowed (and logged at debug level).
    """
    try:
        host = tenants_mod._host_of(apply_url)
        tenants_mod.record_submit(conn, host, ok=(status == "applied"), result=status)
    except Exception:
        logger.debug("record_tenant_outcome failed for %s (status=%s)",
                     (apply_url or "")[:80], status, exc_info=True)


# Challenge-class terminal statuses run_job can return: a wall the agent
# cannot solve unattended. During an --auth-gated (supervised) run, hitting
# one of these means the tenant needs a same-day halt (see
# handle_auth_gated_result below) rather than a retry-storm against a wall
# that won't clear itself within the run.
AUTH_GATED_CHALLENGE_STATUSES: frozenset[str] = frozenset({
    "captcha", "login_issue", "auth_required",
})


def handle_auth_gated_result(conn, host: str, status: str) -> bool:
    """Halt `host` for the rest of the UTC day if `status` is a challenge-class
    terminal result (captcha / login_issue / auth_required) from an
    --auth-gated (supervised) apply run.

    Uses the SAME end-of-UTC-day ISO format as the `tenants halt` CLI
    (datetime.combine(today, time(23,59,59), tzinfo=utc).isoformat()) so
    tenants.is_halted's lexicographic ISO comparison is correct.

    Returns:
        True if the tenant was halted (status was challenge-class), else False.
    """
    if status not in AUTH_GATED_CHALLENGE_STATUSES:
        return False

    from datetime import time as _time

    until_iso = datetime.combine(
        datetime.now(timezone.utc).date(), _time(23, 59, 59), tzinfo=timezone.utc
    ).isoformat()
    tenants_mod.halt_tenant(conn, host, until_iso)
    return True


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
    blocked_company_names, blocked_company_patterns = _load_blocked_companies()
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
                           fit_score, audit_score, audit_label, location, full_description, cover_letter_path, company
                    FROM jobs
                    WHERE (url = ? OR application_url = ? OR application_url LIKE ? OR url LIKE ?)
                      {tailored_clause}
                      AND duplicate_of_url IS NULL
                      AND COALESCE(liveness_status, '') != 'dead'
                      AND COALESCE(apply_status, '') != 'applied'
                      AND COALESCE(apply_status, '') != 'in_progress'
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
                company_block_clause, company_block_params = _company_blocklist_clause(
                    blocked_company_names, blocked_company_patterns)
                params.extend(company_block_params)
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
                # --auth-gated --tenant <host>: scope candidate selection to a single
                # tenant host (positive filter). Env-driven like the other run-scoped
                # filters above so it doesn't need a new acquire_job parameter.
                tenant_scope = (os.environ.get("APPLYPILOT_AUTH_GATED_TENANT_HOST") or "").strip().lower()
                tenant_clause = ""
                if tenant_scope:
                    eff2 = "(CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END)"
                    tenant_clause = f"AND {eff2} LIKE ?"
                    params.append(f"%{tenant_scope}%")
                # Optional freshness filter: skip stale postings (posted/discovered timestamp)
                # older than APPLYPILOT_MAX_JOB_AGE_DAYS that aren't liveness-confirmed.
                # The value is now default-on for safety; 0 explicitly disables.
                fresh_clause = ""
                _raw_age = os.environ.get("APPLYPILOT_MAX_JOB_AGE_DAYS")
                _max_age = 45 if not _raw_age else int(_raw_age)
                if _max_age > 0:
                    _cut = (datetime.now(timezone.utc) - timedelta(days=_max_age)).isoformat()
                    fresh_clause = ("AND (COALESCE(posted_at, discovered_at) IS NULL OR "
                                    "COALESCE(posted_at, discovered_at) >= ? "
                                    "OR COALESCE(liveness_status, '') = 'live')")
                    params.append(_cut)
                # Lane filter (off-lane drift guard): the ORDER BY ranks on-lane roles
                # first but never EXCLUDES off-lane ones, so a drained on-lane queue
                # drifts into pure IC-sales/AE postings that still score >=7 (the user
                # saw "Sales Engineer-Flooring", "Enterprise AE") -- wrong lane for a
                # finance/operator candidate. When APPLYPILOT_LANE_FILTER is on, drop a
                # candidate if it was LLM-diagnosed wrong-lane/ignore, OR its TITLE matches
                # an off-lane needle AND it has NO on-lane audit flag (the flag is a
                # positive override). Off by default; the supervised/production run turns
                # it on. See config.load_lane_filter for the (tunable) needle list.
                lane_clause = ""
                if os.environ.get("APPLYPILOT_LANE_FILTER", "").strip().lower() in (
                        "1", "true", "yes", "on"):
                    off_needles, on_tags = config.load_lane_filter()
                    lane_parts = [
                        "AND COALESCE(fit_gap_category, '') != 'wrong_role_lane'",
                        "AND COALESCE(recommended_action, '') != 'ignore'",
                    ]
                    if off_needles:
                        tnorm = "LOWER(' ' || COALESCE(title, '') || ' ')"
                        title_or = " OR ".join(f"{tnorm} LIKE ?" for _ in off_needles)
                        params.extend(f"%{n}%" for n in off_needles)
                        flag_guard = ""
                        if on_tags:
                            flag_guard = " AND " + " AND ".join(
                                "COALESCE(audit_flags, '') NOT LIKE ?" for _ in on_tags)
                            params.extend(f'%"{t}"%' for t in on_tags)
                        lane_parts.append(f"AND NOT (({title_or}){flag_guard})")
                    lane_clause = "\n                      ".join(lane_parts)
                row = conn.execute(f"""
                    SELECT url, title, site, application_url, tailored_resume_path,
                           fit_score, audit_score, audit_label, location, full_description, cover_letter_path, company
                    FROM jobs
                    WHERE duplicate_of_url IS NULL
                      {tailored_clause}
                      AND COALESCE(liveness_status, '') != 'dead'
                      -- Posting-level dedup (anti-double-submit). One company role often
                      -- exists as SEVERAL job rows -- e.g. two hiring.cafe listings + a
                      -- LinkedIn row that all resolve to the same ATS form. The old guard
                      -- only excluded rows matching an *applied* effective-url, so applying
                      -- one row left its siblings acquirable -> double submit. Now exclude a
                      -- candidate if the SAME posting is already applied, currently
                      -- in_progress (stops two parallel workers grabbing different rows of
                      -- one posting at once), or was submitted-but-unconfirmed
                      -- (no_confirmation = the agent did click submit). Matched two ways:
                      -- (a) effective apply target -- also cross-checked against the durable
                      --     applications ledger so a lost jobs.apply_status can't re-open it;
                      -- (b) company+title -- catches siblings whose application_url differs
                      --     or isn't resolved yet (only when company is known).
                      AND COALESCE(application_url, url) NOT IN (
                            SELECT COALESCE(application_url, url) FROM jobs
                            WHERE apply_status IN ('applied', 'in_progress')
                               OR apply_error IN ('no_confirmation', 'crash_unconfirmed')
                            UNION
                            SELECT COALESCE(NULLIF(application_url, ''), job_url)
                            FROM applications WHERE status = 'applied')
                      AND (
                            TRIM(COALESCE(company, '')) = ''
                            OR LOWER(TRIM(company)) || '|' || LOWER(TRIM(title)) NOT IN (
                                  SELECT LOWER(TRIM(company)) || '|' || LOWER(TRIM(title))
                                  FROM jobs
                                  WHERE TRIM(COALESCE(company, '')) != ''
                                    AND (apply_status IN ('applied', 'in_progress')
                                         OR apply_error IN ('no_confirmation', 'crash_unconfirmed')))
                      )
                      AND (apply_status IS NULL OR apply_status = 'failed')
                      AND (apply_attempts IS NULL OR apply_attempts < ?)
                      AND COALESCE(audit_score, fit_score) >= ?
                      {site_clause}
                      {url_clauses}
                      {company_block_clause}
                      {li_clause}
                      {seen_clause}
                      {host_clause}
                      {tenant_clause}
                      {fresh_clause}
                      {lane_clause}
                    ORDER BY COALESCE(audit_score, fit_score) DESC,
                             (audit_flags LIKE '%"chief_of_staff"%') DESC,
                             (audit_flags LIKE '%"strategy_ops"%'
                               OR audit_flags LIKE '%"gtm_ops"%'
                               OR audit_flags LIKE '%"operations_leadership"%') DESC,
                             role_fit_score DESC,
                             (COALESCE(liveness_status, '') = 'live') DESC,
                             COALESCE(discovered_at, posted_at) DESC,
                             COALESCE(posted_at, discovered_at) DESC,
                             fit_score DESC, url
                    LIMIT 1
                """, [config.DEFAULTS["max_apply_attempts"]] + params).fetchone()

            if not row:
                if not target_url and (blocked_company_names or blocked_company_patterns):
                    company_block_clause, company_block_params = _company_blocklist_clause(
                        blocked_company_names, blocked_company_patterns)
                    if company_block_clause:
                        cur = conn.execute(
                            f"""
                            UPDATE jobs
                            SET apply_status='blocked', apply_error='company_blocklist'
                            WHERE url IN (
                                SELECT url FROM jobs
                                WHERE duplicate_of_url IS NULL
                                  {tailored_clause}
                                  AND COALESCE(liveness_status, '') != 'dead'
                                  AND (apply_status IS NULL OR apply_status = 'failed')
                                  AND COALESCE(audit_score, fit_score) >= ?
                                  AND NOT ({company_block_clause[4:]})
                            )
                            """,
                            [min_score] + company_block_params,
                        )
                        if cur.rowcount:
                            conn.commit()
                            return None
                conn.rollback()
                return None

            # Skip manual ATS sites (unsolvable CAPTCHAs): mark + continue to the
            # next candidate rather than returning None.
            apply_url = row["application_url"] or row["url"]
            if _company_blocked(row, blocked_company_names, blocked_company_patterns):
                conn.execute(
                    "UPDATE jobs SET apply_status='blocked', apply_error='company_blocklist' WHERE url = ?",
                    (row["url"],),
                )
                conn.commit()
                logger.info("Skipping blocked company: %s", row["url"][:80])
                if target_url:
                    return None
                continue
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

            # Pre-filter auth-gated applications (login/account/2FA the agent won't do):
            # skip them at acquire time so the apply never wastes a Chrome launch + agent
            # run reaching a login wall it can only bounce. Mark auth_required (permanent)
            # so they surface in `apply-failures --manual` for a manual pass. The run then
            # spends its launches on jobs that are actually applyable.
            #
            # Assisted inbox auth is opt-in and handles common verification-style walls.
            # When enabled, allow auth-gated rows to flow to the runtime retry path.
            # Off via APPLYPILOT_SKIP_AUTH_GATED=1 (default) or use --no-inbox-auth.
            assisted = os.environ.get("APPLYPILOT_INBOX_AUTH", "").strip().lower() in {"1", "true", "yes", "on"}
            if (os.environ.get("APPLYPILOT_SKIP_AUTH_GATED", "1").strip().lower()
                    not in ("0", "false", "no", "off")
                    and not assisted
                    and config.is_auth_gated_application(apply_url)
                    and not _tenant_bypasses_auth_gate(conn, apply_url)):
                conn.execute(
                    "UPDATE jobs SET apply_status = 'auth_required', apply_error = 'auth_gate', "
                    "apply_attempts = 99 WHERE url = ?",
                    (row["url"],),
                )
                conn.commit()
                if target_url:
                    return None  # the explicitly targeted URL is auth-gated; skip
                continue  # re-select the next candidate (this row is now excluded)

            # Defer UNRESOLVED-AGGREGATOR rows (e.g. chiefofstaffjob.com): their apply
            # target is the aggregator's own page, so the real ATS + company are only
            # revealed at runtime. The posting-level dedup can't tell if such a row
            # duplicates a job already applied elsewhere -> double-submit risk (a real
            # one: an aggregator Picogrid CoS listing for a Picogrid role already applied
            # via Ashby). Park it (retained, reversible) until enrichment resolves the
            # real target; then the effective host changes and it becomes applyable.
            if config.is_unresolved_aggregator(apply_url):
                conn.execute(
                    "UPDATE jobs SET apply_status = 'deferred', "
                    "apply_error = 'aggregator_unresolved_target' WHERE url = ?",
                    (row["url"],),
                )
                conn.commit()
                if target_url:
                    return None  # the explicitly targeted URL is an unresolved aggregator
                continue  # re-select the next candidate (this row is now excluded)

            # Near-duplicate reposting guard: same employer ATS board + a highly similar
            # title to an already-applied role means the company re-posted one role
            # (different job ID + tweaked title) -- exact dedup can't see it, and applying
            # again sends a SECOND application to the same role (the Amae Health case: a
            # "Founder Associate" and a "Business Development Associate" listing, both
            # ".../Growth & Partnership Operations" on greenhouse.io/amaehealth). Skip it.
            # Off via APPLYPILOT_NEAR_DUP_MIN_SHARED_TOKENS=0.
            if not target_url and NEAR_DUP_JACCARD > 0:
                _dup_of = _find_near_duplicate_applied(
                    conn, apply_url, row["title"] or "", row["company"])
                if _dup_of:
                    conn.execute(
                        "UPDATE jobs SET apply_status = 'deferred', "
                        "apply_error = 'near_duplicate_role' WHERE url = ?",
                        (row["url"],),
                    )
                    conn.commit()
                    logger.info("Skipping near-duplicate of applied '%s': %s",
                                _dup_of[:40], (row["title"] or "")[:40])
                    continue  # re-select the next candidate

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


def _preflight_liveness_enabled() -> bool:
    """Whether to HTTP-probe each candidate for closure before launching Chrome.
    On by default; callers can set APPLYPILOT_PREFLIGHT_LIVENESS to 0/false/no/off
    to disable the additional network probe."""
    return os.environ.get("APPLYPILOT_PREFLIGHT_LIVENESS", "").strip().lower() not in (
        "0", "false", "no", "off")


def _stamp_liveness_dead(url: str, reason: str) -> None:
    """Record a confirmed-closed posting as a LIVENESS fact (liveness_status='dead'),
    not just an apply_error -- so the closure is authoritative for the acquire liveness
    filter, the freshness filter, reporting, and the separate liveness tooling. Retains
    the row (never deletes) per the training-retention rule. Best-effort; never raises."""
    try:
        conn = get_connection()
        conn.execute(
            "UPDATE jobs SET liveness_status = 'dead', liveness_reason = ? WHERE url = ?",
            (str(reason)[:120], url))
        conn.commit()
    except Exception:
        logger.debug("liveness dead-stamp failed for %s", (url or "")[:60], exc_info=True)


def mirror_db_offsite(log=None) -> bool:
    """Copy the local authoritative DB to the OneDrive-synced APP_DIR (OFF-MACHINE backup).

    The authoritative DB lives in %LOCALAPPDATA% (APPLYPILOT_DB_PATH) and the periodic
    rolling backup is written next to it -- BOTH on the same local disk. If that disk /
    the LOCALAPPDATA dir is lost, neither survives. This mirrors the DB into the
    OneDrive-synced config.APP_DIR so an off-machine copy exists. Integrity-checked first
    so a corrupt DB never overwrites a good offsite copy. No-op when the DB already IS the
    synced copy (no APPLYPILOT_DB_PATH override) or backups are disabled. Uses the SQLite
    online-backup API, so it's safe while the apply process writes (WAL reader snapshot).
    Best-effort; never raises. Returns True only when a fresh offsite copy was written."""
    if (os.environ.get("APPLYPILOT_BACKUP_INTERVAL") or "600").strip() == "0":
        return False
    import sqlite3

    def _log(m: str) -> None:
        if log:
            try:
                log(m)
            except Exception:
                pass

    try:
        dest_path = config.APP_DIR / "applypilot.db"
        if not config.DB_PATH.exists() or dest_path.resolve() == config.DB_PATH.resolve():
            return False  # DB already lives in the synced dir -> nothing to mirror
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(str(config.DB_PATH), timeout=30)
        try:
            if src.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                _log("[offsite-backup] live DB integrity not OK -- offsite copy left intact")
                return False
            tmp = dest_path.with_suffix(".db.offsite.tmp")
            dest = sqlite3.connect(str(tmp))
            try:
                src.backup(dest)
            finally:
                dest.close()
            tmp.replace(dest_path)
            return True
        finally:
            src.close()
    except Exception:
        logger.debug("offsite DB mirror failed", exc_info=True)
        _log("[offsite-backup] failed (will retry next cycle)")
        return False


def release_lock(url: str) -> None:
    """Release the in_progress lock without changing status."""
    conn = get_connection()
    conn.execute(
        "UPDATE jobs SET apply_status = NULL, agent_id = NULL WHERE url = ? AND apply_status = 'in_progress'",
        (url,),
    )
    conn.commit()


def reclaim_stale_leases(ttl_seconds: int = STALE_LEASE_SECONDS) -> int:
    """Park leases stranded 'in_progress' by a HARD-killed worker (OOM / crash / reboot).

    A clean stop releases the in-flight lease (release_lock / mark_result), so a job left
    'in_progress' past ttl_seconds means the process was hard-killed mid-job -- and the
    agent may already have CLICKED SUBMIT before dying (a submit happens near the end of
    the run). Silently re-offering it would risk a DOUBLE submission, which is exactly
    what the posting-level dedup CANNOT see (the row isn't applied / no_confirmation / in
    the applications ledger -- mark_result never ran). So instead of clearing the lease,
    park it as failed/crash_unconfirmed (attempts=99 so it is NOT auto-retried): it
    surfaces in `apply-failures` for a manual decision, and the dedup treats it as
    possibly-submitted so its siblings aren't applied either. Only fires past ttl_seconds
    (> the agent timeout) so a live in-flight job is never touched. NOTE: this trades a
    few unneeded holds (a job killed in the brief pre-launch window never submitted) for a
    hard guarantee against crash-induced double-submits, per the owner's priority.

    Returns:
        Number of leases parked for review.
    """
    conn = get_connection()
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)).isoformat()
    cursor = conn.execute(
        """
        UPDATE jobs SET apply_status = 'failed', apply_error = 'crash_unconfirmed',
                        apply_attempts = 99, agent_id = NULL
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
               model: str | None = "sonnet", worker_id: int = 0) -> Path | None:
    """Generate a prompt file and per-worker MCP config for manual debugging.

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
    # Anti double-submit: NEVER un-park a possibly-submitted job. reclaim_stale_leases
    # parks hard-killed leases as failed/crash_unconfirmed, and the agent records a
    # submitted-but-unconfirmed apply as failed/no_confirmation (= it DID click submit).
    # Both carry apply_status='failed', so a naive reset would clear them back to
    # retryable -> acquire_job re-offers them -> a SECOND application under the user's
    # name. Exclude these error codes here, mirroring the dedup exclusion in acquire_job
    # (apply_error IN ('no_confirmation', 'crash_unconfirmed') = do not re-apply). The
    # outer OR is parenthesized so the exclusion applies to BOTH match branches.
    cursor = conn.execute("""
        UPDATE jobs SET apply_status = NULL, apply_error = NULL,
                       apply_attempts = 0, agent_id = NULL
        WHERE (apply_status = 'failed'
               OR (apply_status IS NOT NULL AND apply_status != 'applied'
                   AND apply_status != 'in_progress'))
          AND (apply_error IS NULL
               OR apply_error NOT IN ('no_confirmation', 'crash_unconfirmed'))
    """)
    conn.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# Usage-limit / quota wall detection (RETRYABLE -- the agent never touched the page)
# ---------------------------------------------------------------------------
# An agent (Codex or Claude) that hits its OWN account usage/quota wall fails on the FIRST
# turn -- before calling a single browser/MCP tool -- and exits WITHOUT printing a RESULT:
# line. The generic fallback classifies that as `no_result_line`, which the fleet worker
# parks as `crash_unconfirmed` ("may have submitted, never re-lease"). But a wall hit with
# ZERO tool calls in the transcript PROVABLY never touched the application form, so the job
# was never submitted and is safe to RE-QUEUE. Detect the wall here and emit a distinct,
# retryable status; everything else keeps the conservative no_result_line -> crash_unconfirmed
# treatment (a genuine mid-apply crash always shows tool calls). Live incident 2026-06-29:
# a Codex-Spark wall poisoned ~283 good, never-touched jobs into crash_unconfirmed in minutes.
USAGE_LIMIT_STATUS = "failed:usage_limit"

# Phrases that identify an AGENT-side usage/quota wall (NOT a site's application limit --
# that surfaces as RESULT:FAILED:rate_limited and never reaches this fallback). Matched
# case-insensitively as substrings. "hit your usage limit" deliberately avoids the
# you've/you’ve apostrophe variants. Covers Codex-Spark ("You've hit your usage limit / Try
# again at <time> / Switch to another model") and Claude (usage/weekly/5-hour limit).
# 2026-07-03: Claude CLI switched wording to "hit your SESSION limit ... resets <time>" --
# the old "usage limit"-only signatures missed it entirely and a worker hung silently for 4h
# because the wall was never classified. Both wordings are matched below.
_USAGE_LIMIT_SIGNATURES = (
    "hit your usage limit",
    "hit your session limit",
    "usage limit reached",
    "session limit reached",
    "reached your usage limit",
    "reached your session limit",
    "usage limit exceeded",
    "session limit exceeded",
    "exceeded your usage limit",
    "hit your weekly limit",
    "weekly limit reached",
    "5-hour limit reached",
    "switch to another model",
    "switch to a different model",
    "try again at",
    "quota exceeded",
    "exceeded your quota",
    "insufficient quota",
    "out of credits",
    "upgrade to continue",
)


def _is_usage_limit_signature(text: str | None) -> bool:
    """True if the transcript carries an agent usage/quota-wall signature."""
    if not text:
        return False
    low = text.lower()
    return any(sig in low for sig in _USAGE_LIMIT_SIGNATURES)


_SAFE_PREPAGE_TOOL_NAMES = {
    "toolsearch",
    "tool_search",
}


def _tool_call_touches_application(name: str | None) -> bool:
    """False only for agent/meta tools that cannot inspect or modify the application.

    Unknown tool names stay conservative: count them as page-touching so a later missing
    RESULT line remains crash_unconfirmed instead of being re-queued.
    """
    normalized = (name or "").strip().lower()
    if not normalized:
        return True
    return normalized not in _SAFE_PREPAGE_TOOL_NAMES


def _no_result_status(transcript: str | None, tool_calls: int) -> str:
    """Classify a run that printed NO RESULT: line.

    A usage/quota wall hit before any application-touching tool call (tool_calls == 0)
    provably never submitted -> RETRYABLE USAGE_LIMIT_STATUS (re-queued upstream).
    Any browser/MCP/form tool call means the agent may have reached/filled the form (a
    real mid-apply crash) -> conservative no_result_line, parked crash_unconfirmed and
    NEVER re-leased.
    """
    if tool_calls == 0 and _is_usage_limit_signature(transcript):
        return USAGE_LIMIT_STATUS
    return "failed:no_result_line"


def is_usage_limit_result(status: str | None) -> bool:
    """True if a run_job status is the retryable usage/quota-wall outcome. Consumed by the
    fleet worker to RE-QUEUE (vs crash_unconfirmed) and to back off when walls repeat."""
    if not status:
        return False
    return status.split(":", 1)[-1].strip().lower() == "usage_limit"


# ---------------------------------------------------------------------------
# Per-job execution
# ---------------------------------------------------------------------------

def _maybe_greenhouse_apply(job: dict, port: int, *, dry_run: bool,
                            resume_text: str, resume_path) -> tuple[str, int] | None:
    """Opt-in deterministic Greenhouse path. Returns (status, duration_ms) if it
    OWNED the application, else None so the apply agent proceeds. Never raises.

    Two independent gates:
      * APPLYPILOT_GREENHOUSE_ADAPTER  -> SHADOW: deterministically fill the form
        in a scratch tab in DRY-RUN, log the plan, then fall through (agent still
        submits). Proves the adapter works on live forms.
      * + APPLYPILOT_GREENHOUSE_ADAPTER_SUBMIT -> OWN: for a ready plan on a real
        (non-dry) run, fill + submit + confirm, and return the resulting status;
        the agent is skipped. An incomplete plan (agent_fallback) always defers.

    This runs INSIDE _run_job_impl, so the job has already cleared the apply
    loop's lease / canary / cost gates before we get here.
    """
    try:
        from applypilot.apply.greenhouse_adapter import parse_greenhouse_url
        from applypilot.apply.greenhouse_submit import (
            adapter_enabled,
            apply_greenhouse,
            submit_enabled,
        )
    except Exception:
        return None
    if not adapter_enabled():
        return None
    url = job.get("application_url") or job.get("url") or ""
    if not parse_greenhouse_url(url):
        return None

    own = submit_enabled() and not dry_run  # may we actually submit and own the outcome?
    t0 = time.time()
    res = None
    try:
        from playwright.sync_api import sync_playwright
        profile = config.load_profile()
        pdf = str(Path(resume_path).with_suffix(".pdf")) if resume_path else None
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                res = apply_greenhouse(url, profile=profile, resume_text=resume_text,
                                       resume_path=pdf, page=page, dry_run=not own)
            finally:
                page.close()
    except Exception:
        logger.debug("greenhouse adapter failed (non-fatal); agent proceeds", exc_info=True)
        return None

    if not res or res.get("route") != "deterministic":
        logger.info("greenhouse adapter: deferring to agent (route=%s unmapped=%s)",
                    (res or {}).get("route"), (res or {}).get("unmapped"))
        return None
    if not own:
        plan = res.get("plan")
        logger.info("greenhouse shadow OK: ready=%s free_text=%s",
                    res.get("ready"), list(plan.free_text) if plan else [])
        return None  # shadow: agent still owns the submission

    status = res.get("status", "failed:no_confirmation")
    duration_ms = int((time.time() - t0) * 1000)
    logger.info("greenhouse adapter OWNED submit for %s -> %s", url, status)
    return (status, duration_ms)


def _maybe_lever_shadow(job: dict, port: int, *, resume_text: str, resume_path) -> None:
    """Opt-in SHADOW validation of the deterministic Lever adapter.

    Lever's submit is hCaptcha-gated, so the adapter never owns submission: this
    discovers + deterministically fills the form in a scratch tab (no submit
    action is ever emitted), logs the plan, then closes the tab. The apply agent
    still owns the real submission. Gated by APPLYPILOT_GREENHOUSE_ADAPTER (the
    shared adapter flag). Never raises.
    """
    try:
        from applypilot.apply.greenhouse_submit import adapter_enabled, execute_form
        from applypilot.apply.lever_adapter import (
            build_lever_plan,
            discover_fields,
            parse_lever_url,
            plan_lever_form_actions,
        )
    except Exception:
        return
    if not adapter_enabled():
        return
    url = job.get("application_url") or job.get("url") or ""
    if not parse_lever_url(url):
        return
    try:
        from playwright.sync_api import sync_playwright
        profile = config.load_profile()
        pdf = str(Path(resume_path).with_suffix(".pdf")) if resume_path else None
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://localhost:{port}")
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                fields = discover_fields(page)
                plan = build_lever_plan(fields, profile=profile, resume_text=resume_text,
                                        job={"site": job.get("site", "")})
                execute_form(plan_lever_form_actions(plan, fields, resume_path=pdf),
                             page, dry_run=True)
                logger.info("lever shadow: fields=%d ready=%s free_text=%s unmapped=%s",
                            len(fields), plan.ready, list(plan.free_text), plan.unmapped_required)
            finally:
                page.close()
    except Exception:
        logger.debug("lever shadow failed (non-fatal); agent proceeds", exc_info=True)


def run_job(job: dict, port: int, worker_id: int = 0,
            model: str | None = "sonnet", dry_run: bool = False,
            agent: str = "claude", inbox_auth_hint: str | None = None,
            supervised: bool = False) -> tuple[str, int]:
    """Spawn an apply-agent session for one job application.

    Args:
        supervised: When True, the browser session runs in full headed mode
            so the owner can watch (headed-ness itself is Task 5's
            `apply --auth-gated` concern, not this function's). The agent
            fills the form and submits exactly as in the unattended path --
            the PROMPT is byte-identical for supervised True/False. The
            ONLY effect of supervised here is accounting: once the run's
            REAL terminal status is known, record_tenant_outcome() is
            called exactly once against the tenant registry (owner decision
            2026-07-03, amendment 0b2fead -- replaces the dropped
            pause-and-confirm gate from commit 6565936, which had a
            premature-ok=True bug).

    Returns:
        Tuple of (status_string, duration_ms). Status is one of:
        'applied', 'expired', 'captcha', 'login_issue',
        'failed:reason', or 'skipped'.
    """
    status, duration_ms = _run_job_impl(
        job, port, worker_id=worker_id, model=model, dry_run=dry_run,
        agent=agent, inbox_auth_hint=inbox_auth_hint,
    )
    if supervised:
        apply_url = job.get("application_url") or job.get("url") or ""
        try:
            conn = get_connection()
            record_tenant_outcome(conn, apply_url, status)
        except Exception:
            logger.debug("supervised record_tenant_outcome failed", exc_info=True)
    return status, duration_ms


def _run_job_impl(job: dict, port: int, worker_id: int = 0,
                   model: str | None = "sonnet", dry_run: bool = False,
                   agent: str = "claude",
                   inbox_auth_hint: str | None = None) -> tuple[str, int]:
    """Actual apply-agent run. See run_job for the public contract; this has
    no `supervised` parameter -- the prompt built here is IDENTICAL
    regardless of supervised mode (accounting is layered on by the run_job
    wrapper, not the run itself)."""
    # Read tailored resume text
    resume_path = config.resolve_resume_stem(job.get("tailored_resume_path"))
    txt_path = Path(resume_path).with_suffix(".txt") if resume_path else None
    resume_text = ""
    if txt_path and txt_path.exists():
        resume_text = txt_path.read_text(encoding="utf-8")

    # Opt-in deterministic Greenhouse adapter (no-op unless
    # APPLYPILOT_GREENHOUSE_ADAPTER is set). Owns the application only with the
    # second submit gate on a ready plan; otherwise validates + falls through.
    gh_result = _maybe_greenhouse_apply(job, port, dry_run=dry_run,
                                        resume_text=resume_text, resume_path=resume_path)
    if gh_result is not None:
        return gh_result
    _maybe_lever_shadow(job, port, resume_text=resume_text, resume_path=resume_path)

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
        inbox_auth_hint=inbox_auth_hint,
    )

    # Write per-worker MCP config
    mcp_config_path = config.APP_DIR / f".mcp-apply-{worker_id}.json"
    mcp_config_path.write_text(json.dumps(_make_mcp_config(port)), encoding="utf-8")

    agent = _normalize_agent(agent)
    cmd = build_apply_agent_command(
        agent=agent,
        model=model,
        mcp_config_path=mcp_config_path,
        cdp_port=port,
    )

    env = os.environ.copy()
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE_ENTRYPOINT", None)

    display_score = job.get("audit_score") if job.get("audit_score") is not None else job.get("fit_score", 0)
    update_state(worker_id, status="applying", job_title=job["title"],
                 company=job.get("site", ""), score=display_score,
                 start_time=time.time(), actions=0, last_action="starting")
    add_event(f"[W{worker_id}] Starting: {job['title'][:40]} @ {job.get('site', '')}")

    worker_log = config.LOG_DIR / f"worker-{worker_id}.log"
    worker_log.parent.mkdir(parents=True, exist_ok=True)  # belt-and-suspenders: covers the
    # worker_log (below) AND the per-run job_log -- both live under LOG_DIR. ensure_dirs() is
    # the primary guard; this makes run_job self-sufficient if a fresh env ever skips it.
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
        with _agent_lock:
            _agent_procs[worker_id] = proc

        text_parts: list[str] = []
        final_result_text: list[str] = []  # text from the final 'result' message
        stats_holder: dict = {}
        # Count only application-touching tool calls. ZERO app tool calls + a usage-limit
        # signature == the agent hit a wall before touching the page -> safely re-queuable
        # (see _no_result_status). A list so the daemon-thread closure can mutate it.
        application_tool_calls = [0]

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
                                    if _tool_call_touches_application(name):
                                        application_tool_calls[0] += 1
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
                            final_result_text.clear()
                            final_result_text.append(rt)
                        elif msg_type == "item.completed":
                            item = msg.get("item", {})
                            item_type = item.get("type")
                            if item_type == "agent_message":
                                text = item.get("text") or item.get("message") or ""
                                if text:
                                    text_parts.append(text)
                                    final_result_text.clear()
                                    final_result_text.append(text)
                                    lf.write(text + "\n")
                            elif item_type in {"mcp_tool_call", "tool_call"}:
                                name = item.get("name") or item.get("tool_name") or item_type
                                if _tool_call_touches_application(str(name)):
                                    application_tool_calls[0] += 1
                                lf.write(f"  >> {name}\n")
                                ws = get_state(worker_id)
                                cur_actions = ws.actions if ws else 0
                                update_state(worker_id,
                                             actions=cur_actions + 1,
                                             last_action=str(name)[:35])
                        elif msg_type == "turn.completed":
                            usage = msg.get("usage", {})
                            stats_holder.update({
                                "input_tokens": usage.get("input_tokens", 0),
                                "output_tokens": usage.get("output_tokens", 0),
                                "cache_read": usage.get("cached_input_tokens", 0),
                                "cache_create": usage.get("cache_creation_input_tokens", 0),
                                "cost_usd": usage.get("total_cost_usd", 0),
                                "turns": usage.get("turns", 0),
                            })
                        elif msg_type in ("error", "turn.failed"):
                            # Codex surfaces hard failures (e.g. an invalid --model, an
                            # auth/quota error) as an `error` or `turn.failed` event and
                            # then exits WITHOUT printing a RESULT: line. Capturing the
                            # message into the transcript turns an opaque no_result_line
                            # into a logged, diagnosable reason.
                            err = msg.get("message") or msg.get("error") or line
                            text_parts.append(str(err))
                            lf.write(str(err) + "\n")
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
            # Agent exited before reading the prompt (e.g. a launch error). Let
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
        job_log = config.LOG_DIR / f"{agent}_{ts}_w{worker_id}_{job.get('site', 'unknown')[:20]}.txt"
        job_log.write_text(output, encoding="utf-8")
        run_stats = dict(stats) if stats else {}
        run_stats["transcript"] = output[-20000:]
        run_stats["job_log"] = str(job_log)
        run_stats["job_log_path"] = str(job_log)
        run_stats["application_tool_calls"] = application_tool_calls[0]
        run_stats["transcript_digest"] = (
            "sha256:" + hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()
        )
        run_stats["final_result_source"] = "final_message" if final_text and "RESULT:" in final_text else "transcript"
        _last_run_stats[worker_id] = run_stats

        if stats:
            cost = stats.get("cost_usd", 0)
            ws = get_state(worker_id)
            prev_cost = ws.total_cost if ws else 0.0
            update_state(worker_id, total_cost=prev_cost + cost)
            # Persist the apply-agent's REAL per-job cost durably (stage='apply_agent').
            # The agent runs via the claude CLI -- its cost is otherwise only kept in
            # in-process worker state, which resets on every crash/restart. Recording it
            # to the durable llm_usage table lets the supervisor compute ACTUAL
            # cross-crash spend (snapshot-at-start delta) instead of estimating from
            # applied-count, and surfaces apply cost in the usage reports. Best-effort.
            if cost:
                try:
                    from applypilot.database import record_llm_usage
                    record_llm_usage(
                        stage="apply_agent", model=model, provider="claude-cli",
                        usage={
                            "prompt_tokens": stats.get("input_tokens") or 0,
                            "completion_tokens": stats.get("output_tokens") or 0,
                            "total_tokens": (stats.get("input_tokens") or 0)
                                            + (stats.get("output_tokens") or 0),
                        },
                        est_cost_usd=float(cost),
                    )
                except Exception:
                    logger.debug("apply-agent usage record failed", exc_info=True)

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

        # No RESULT: line. Distinguish an agent usage/quota wall hit on turn 1 (no tool
        # calls -> the page was never touched -> RETRYABLE, re-queued upstream) from a
        # genuine ran-but-no-clean-result crash (-> no_result_line -> crash_unconfirmed).
        status = _no_result_status(output, application_tool_calls[0])
        if status == USAGE_LIMIT_STATUS:
            add_event(f"[W{worker_id}] USAGE-LIMIT wall, page never touched ({elapsed}s) -- retryable")
            update_state(worker_id, status="failed", last_action=f"usage-limit ({elapsed}s)")
        else:
            add_event(f"[W{worker_id}] NO RESULT ({elapsed}s)")
            update_state(worker_id, status="failed", last_action=f"no result ({elapsed}s)")
        return status, duration_ms

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
        with _agent_lock:
            _agent_procs.pop(worker_id, None)
        if proc is not None and proc.poll() is None:
            _kill_process_tree(proc.pid)


# ---------------------------------------------------------------------------
# Permanent failure classification
# ---------------------------------------------------------------------------

# Outcomes that prove the posting itself is CLOSED (not a transient/env failure) ->
# also recorded as liveness_status='dead'. 'expired' = the agent saw "closed / no
# longer accepting". 'page_error' (500/blank) is deliberately NOT here -- it can be
# transient. Used to feed apply-time closures back into the liveness signal.
DEAD_ON_VISIT_REASONS: set[str] = {"expired"}

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
    # Submit clicked but unconfirmed: auto-retrying risks a DOUBLE submission, so
    # don't -- surface it in the run-summary fail reasons for manual review instead.
    "no_confirmation",
}

PERMANENT_PREFIXES: tuple[str, ...] = ("site_blocked", "cloudflare", "blocked_by")


def _is_auth_required_result(result: str | None) -> bool:
    """Return True when a result should be routed to assisted/manual login."""
    if not result:
        return False
    normalized = result.strip().lower()
    parts = [part.strip() for part in normalized.split(":") if part.strip()]
    return normalized in AUTH_REQUIRED_REASONS or any(part in AUTH_REQUIRED_REASONS for part in parts)


def _is_permanent_failure(result: str) -> bool:
    """Determine if a failure should never be retried."""
    reason = result.split(":", 1)[-1] if ":" in result else result
    return (
        result in PERMANENT_FAILURES
        or reason in PERMANENT_FAILURES
        or any(reason.startswith(p) for p in PERMANENT_PREFIXES)
    )


def _local_gmail_available() -> bool:
    """True if THIS machine has its own Gmail OAuth credentials (i.e. the home box)."""
    try:
        from applypilot.config import APP_DIR

        return (APP_DIR / "gmail_credentials.json").exists()
    except Exception:
        return False


def _auto_relay() -> bool:
    """A remote worker with a fleet DB connection but NO local Gmail creds can only use
    the fleet OTP relay for email verification -- so auto-enable relay mode there. This
    lets an offsite worker clear verification with zero local config; without it such a
    worker silently fails every verification-gated job (email_verification_required)."""
    return bool(os.environ.get("FLEET_PG_DSN")) and not _local_gmail_available()


def _inbox_auth_enabled() -> bool:
    """Return True when authenticated inbox automation should be used. Explicit
    APPLYPILOT_INBOX_AUTH wins; otherwise a credential-less remote worker (which can only
    reach codes via the relay) auto-enables."""
    if os.environ.get("APPLYPILOT_INBOX_AUTH", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        return True
    return _auto_relay()


def _inbox_auth_mode() -> str:
    """'relay' (ask the fleet for the code) or 'local' (read Gmail here). Explicit
    APPLYPILOT_INBOX_AUTH_MODE wins; otherwise a credential-less remote worker
    auto-selects 'relay', and everything else defaults to 'local'."""
    mode = os.environ.get("APPLYPILOT_INBOX_AUTH_MODE", "").strip().lower()
    if mode in {"relay", "local"}:
        return mode
    return "relay" if _auto_relay() else "local"


def _should_prearm_inbox_auth(job: dict) -> bool:
    """Whether to file an OTP relay request before the browser reaches the wall."""
    if os.environ.get("APPLYPILOT_INBOX_AUTH_PREARM", "").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return False
    if not _inbox_auth_enabled() or _inbox_auth_mode() != "relay":
        return False
    try:
        from urllib.parse import urlparse

        apply_target = job.get("application_url") or job.get("url")
        url_lower = (apply_target or "").lower()
        host = (urlparse(apply_target or "").hostname or "").lower()
        sites_cfg = config.load_sites_config()
        auth_cfg = sites_cfg.get("auth_gated", {}) or {}
        domains = [str(d).lower() for d in (auth_cfg.get("domains", []) or [])]
        domains.extend(str(d).lower() for d in config.load_blocked_sso())
        return any(d and (d in host or d in url_lower) for d in domains)
    except Exception:
        logger.debug("Could not evaluate inbox auth pre-arm eligibility", exc_info=True)
        return False


def _prearm_inbox_auth_request(job: dict) -> int | None:
    """Create a pending relay request without waiting for the code."""
    try:
        from applypilot.apply import pgqueue
        from applypilot.fleet import otp_relay

        dsn = os.environ.get("FLEET_PG_DSN")
        if not dsn:
            return None
        worker_id = os.environ.get("FLEET_WORKER_ID", "worker")
        timeout = int(os.environ.get("APPLYPILOT_INBOX_AUTH_TIMEOUT", "300"))
        apply_target = job.get("application_url") or job["url"]
        with pgqueue.connect(dsn) as conn:
            return otp_relay.request_code(
                conn,
                worker_id=worker_id,
                job_url=job["url"],
                application_url=apply_target,
                ttl_seconds=timeout,
            )
    except Exception:
        logger.debug("Relay inbox auth pre-arm failed", exc_info=True)
        return None


def _format_relay_code_hint(code) -> str:
    if code.kind == "magic_link":
        return f"magic_link={code.value}\nsource=fleet_relay"
    return f"code={code.value}\nsource=fleet_relay"


def _consume_prearmed_inbox_auth_hint(
    request_id: int | None,
    *,
    timeout_seconds: int | None = None,
    poll_seconds: float | None = None,
) -> str | None:
    """Wait briefly for a pre-filed relay request and return the prompt hint."""
    if request_id is None:
        return None
    try:
        from applypilot.apply import pgqueue
        from applypilot.fleet import otp_relay

        dsn = os.environ.get("FLEET_PG_DSN")
        if not dsn:
            return None
        if timeout_seconds is None:
            timeout_seconds = int(os.environ.get("APPLYPILOT_INBOX_AUTH_POSTRUN_TIMEOUT") or "45")
        if poll_seconds is None:
            poll_seconds = float(os.environ.get("APPLYPILOT_INBOX_AUTH_POLL_SECONDS", "5"))
        with pgqueue.connect(dsn) as conn:
            code = otp_relay.poll_for_code(
                conn,
                request_id,
                timeout_seconds=timeout_seconds,
                poll_seconds=poll_seconds,
            )
        if code is None:
            return None
        return _format_relay_code_hint(code)
    except Exception:
        logger.debug("Relay inbox auth consume failed", exc_info=True)
        return None


def _relay_inbox_auth_hint(job: dict) -> str | None:
    """Remote-worker path: get the verification code from the fleet OTP relay."""
    try:
        request_id = _prearm_inbox_auth_request(job)
        if request_id is None:
            return None
        timeout = int(os.environ.get("APPLYPILOT_INBOX_AUTH_TIMEOUT", "300"))
        poll = float(os.environ.get("APPLYPILOT_INBOX_AUTH_POLL_SECONDS", "5"))
        return _consume_prearmed_inbox_auth_hint(
            request_id,
            timeout_seconds=timeout,
            poll_seconds=poll,
        )
    except Exception:
        logger.debug("Relay inbox auth failed", exc_info=True)
        return None


def _format_inbox_auth_hint(match: inbox_auth.AuthEmailMatch) -> str:
    if match.candidate.kind == "magic_link":
        return f"magic_link={match.candidate.value}\nreason={'; '.join(match.reasons)}"
    return (
        f"code={match.candidate.value}\n"
        f"sender={match.sender}\n"
        f"subject={match.subject}\n"
        f"received_at={match.received_at or 'unknown'}\n"
        f"reason={'; '.join(match.reasons)}"
    )


def _poll_inbox_auth_hint(job: dict) -> str | None:
    """Poll Gmail once for a likely verification code/magic-link for a given job."""
    if not _inbox_auth_enabled():
        return None
    if _inbox_auth_mode() == "relay":
        return _relay_inbox_auth_hint(job)
    try:
        from urllib.parse import urlparse

        if not _inbox_auth_enabled():
            return None

        apply_target = job.get("application_url") or job["url"]
        host = (urlparse(apply_target).hostname or "").lower()
        provider = host if host else "unknown"
        timeout = int(os.environ.get("APPLYPILOT_INBOX_AUTH_TIMEOUT", "300"))
        poll = int(os.environ.get("APPLYPILOT_INBOX_AUTH_POLL_SECONDS", "5"))
        max_errors = int(os.environ.get("APPLYPILOT_INBOX_AUTH_MAX_ERRORS", "3"))
        minutes = int(os.environ.get("APPLYPILOT_INBOX_AUTH_MINUTES", "15"))
        max_messages = int(os.environ.get("APPLYPILOT_INBOX_AUTH_MAX_MESSAGES", "25"))
        challenge_type = os.environ.get("APPLYPILOT_INBOX_AUTH_CHALLENGE_TYPE", "email_code").strip().lower()
        if challenge_type not in {"email_code", "magic_link"}:
            logger.debug(
                "Unsupported inbox auth challenge type %s; defaulting to email_code",
                challenge_type,
            )
            challenge_type = "email_code"
        challenge_id = inbox_auth.create_auth_challenge(
            job_url=job["url"],
            application_url=apply_target,
            provider=provider,
            challenge_type=challenge_type,
        )
        inbox_auth.set_auth_challenge_status(challenge_id, "watching")
        inbox_auth.mark_auth_challenge_attempt(challenge_id, "polling")
        inbox_auth.expire_stale_challenges()

        match = inbox_auth.watch_gmail_for_auth_code(
            timeout_seconds=timeout,
            poll_seconds=poll,
            max_errors=max_errors,
            minutes=minutes,
            max_messages=max_messages,
        )
        if not match:
            return None
        try:
            event_id = inbox_auth.record_inbox_event(
                message_id=match.message_id,
                thread_id=match.thread_id,
                sender=match.sender,
                subject=match.subject,
                event_type="auth_code",
                confidence=match.candidate.confidence,
                matched_job_url=job["url"],
                matched_company=job.get("company"),
                matched_method=match.candidate.kind,
                snippet=match.snippet,
                received_at=match.received_at,
            )
            inbox_auth.resolve_auth_challenge(challenge_id=challenge_id, inbox_event_id=event_id)
        except Exception:
            logger.debug("Failed to persist inbox auth event/challenge resolution", exc_info=True)

        return _format_inbox_auth_hint(match)
    except Exception:
        logger.debug("Inbox auth polling failed", exc_info=True)
        return None


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


# --- Cross-process LinkedIn safety (DB-derived, so it holds across parallel -----
# agents/processes -- the in-memory _linkedin_halt + per-host gap only cover ONE
# process). These reuse existing columns (apply_status/apply_error/last_attempted_at);
# no new schema. The daily cap (_linkedin_today) is already DB-derived/shared.
# Cooldown after a LinkedIn challenge/rate-limit/auth failure during which EVERY
# process stops acquiring LinkedIn jobs. LinkedIn restrictions are serious -> back
# off hard. Tunable via APPLYPILOT_LINKEDIN_HALT_COOLDOWN (seconds; 0 disables).
LINKEDIN_HALT_COOLDOWN = int(os.environ.get("APPLYPILOT_LINKEDIN_HALT_COOLDOWN") or 21600)
LINKEDIN_HALT_REASONS = {
    "linkedin_rate_limited", "linkedin_challenge", "linkedin_pause",
    "rate_limited", "auth_required", "login_issue",
}


def _linkedin_halt_active(conn) -> bool:
    """True if any LinkedIn-lane job failed with a halt-reason within the cooldown.
    SHARED across all processes/workers: if one agent hits a LinkedIn challenge,
    every agent pauses the LinkedIn lane. mark_result already persists apply_error,
    so no extra write is needed to trip this."""
    if LINKEDIN_HALT_COOLDOWN <= 0:
        return False
    since = (datetime.now(timezone.utc) - timedelta(seconds=LINKEDIN_HALT_COOLDOWN)).isoformat()
    ph = ",".join("?" * len(LINKEDIN_HALT_REASONS))
    return conn.execute(
        f"SELECT 1 FROM jobs WHERE {_LINKEDIN_LANE_SQL} AND apply_status = 'failed' "
        f"AND apply_error IN ({ph}) AND last_attempted_at >= ? LIMIT 1",
        (*LINKEDIN_HALT_REASONS, since),
    ).fetchone() is not None


def _linkedin_gap_wait(conn, exclude_url: str) -> float:
    """Cross-process LinkedIn pacing: seconds to wait so this apply lands at least
    the jittered LINKEDIN_HOST_GAP after the most recent OTHER LinkedIn attempt by
    ANY process (read from last_attempted_at). Stops two parallel agents from
    bursting LinkedIn even though their in-memory throttles can't see each other."""
    row = conn.execute(
        f"SELECT MAX(last_attempted_at) FROM jobs WHERE {_LINKEDIN_LANE_SQL} "
        f"AND last_attempted_at IS NOT NULL AND url != ?",
        (exclude_url,),
    ).fetchone()
    if not row or not row[0]:
        return 0.0
    try:
        last = datetime.fromisoformat(row[0])
    except (ValueError, TypeError):
        return 0.0
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    gap = LINKEDIN_HOST_GAP
    if GAP_JITTER_HI > GAP_JITTER_LO > 0:
        gap *= random.uniform(GAP_JITTER_LO, GAP_JITTER_HI)
    return max(0.0, gap - (datetime.now(timezone.utc) - last).total_seconds())


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


# --- Global systemic-failure circuit breaker ----------------------------------
# A mid-run auth/API/CDP outage makes EVERY job fail the SAME way -- the agent emits
# no RESULT line (no_result_line) or hangs past the timeout (timeout). The offsite
# per-host breaker can't catch this (no_result_line isn't a host-fault, and it spans
# many hosts), and no_result_line/timeout are PERMANENT failures, so without a global
# brake an unattended continuous run would mark the ENTIRE applyable queue permanently
# failed before anyone noticed. So: count CONSECUTIVE systemic-suspect failures across
# ALL workers; ANY proof-of-life outcome (an applied, or a job-specific failure that
# shows the agent+browser actually worked -- captcha, salary, etc.) resets the streak.
# After SYSTEMIC_FAIL_BREAKER in a row, halt the run AND un-burn the streak (those jobs
# were almost certainly good). This is the process-wide analogue of the offsite breaker.
SYSTEMIC_FAIL_BREAKER = int(os.environ.get("APPLYPILOT_SYSTEMIC_FAIL_BREAKER")
                            or os.environ.get("APPLYPILOT_GLOBAL_FAIL_BREAKER") or 5)
# Failure reasons that mean the agent never proved it could drive the browser this run
# -> likely the environment (auth/API/CDP/Chrome/usage wall), not the specific posting. A
# streak of these trips the breaker; any proof-of-life outcome resets it. browser_crashed /
# browser_unavailable were observed in a live run -- a one-off is harmless (resets on
# the next success), but a sustained streak means Chrome/CDP is broken, not the jobs.
# usage_limit (agent quota/usage wall, never touched the page) belongs here too: a wall
# storm should halt + keep the streak retryable rather than churn the whole queue.
SYSTEMIC_FAIL_REASONS = {
    "no_result_line", "timeout", "browser_crashed", "browser_unavailable",
    "usage_limit",
}
_systemic_fail_count = 0
_systemic_recent: list[str] = []
_systemic_fail_lock = threading.Lock()


def _is_systemic_failure(reason: str | None) -> bool:
    """True if a failure reason signals a likely environment outage (not job-specific)."""
    r = (reason or "").split(":", 1)[-1].strip().lower()
    return r in SYSTEMIC_FAIL_REASONS


def _note_systemic_failure(url: str | None) -> bool:
    """Record one systemic-suspect failure; return True if the global breaker just
    tripped (caller should halt the run)."""
    global _systemic_fail_count
    with _systemic_fail_lock:
        _systemic_fail_count += 1
        if url:
            _systemic_recent.append(url)
            # Only the current streak matters (a trip clears it); bound the list so a
            # disabled breaker (=0) can't grow it without limit over a long run.
            keep = max(SYSTEMIC_FAIL_BREAKER, 1)
            if len(_systemic_recent) > keep * 2:
                del _systemic_recent[:-keep]
        return SYSTEMIC_FAIL_BREAKER > 0 and _systemic_fail_count >= SYSTEMIC_FAIL_BREAKER


def _note_healthy_outcome() -> None:
    """Any proof-of-life outcome (applied, or a job-specific failure) resets the
    systemic-failure streak -- so only an UNBROKEN run of pure outage-failures trips."""
    global _systemic_fail_count
    with _systemic_fail_lock:
        _systemic_fail_count = 0
        _systemic_recent.clear()


def _trip_systemic_breaker(worker_id: int) -> None:
    """Halt the run and un-burn the recent systemic-failure streak: a string of pure
    no-result/timeout failures means the environment died (auth/API/CDP), not the
    postings, so reset those jobs to retryable instead of leaving them burned to
    attempts=99. Then stop so nobody has to babysit a run that's silently failing."""
    with _systemic_fail_lock:
        urls = list(_systemic_recent)
        _systemic_recent.clear()
    try:
        conn = get_connection()
        for u in urls:
            conn.execute(
                "UPDATE jobs SET apply_status = NULL, apply_error = 'systemic_halt', "
                "apply_attempts = 0, agent_id = NULL WHERE url = ?",
                (u,),
            )
        conn.commit()
    except Exception:
        logger.debug("systemic-streak un-burn failed", exc_info=True)
    add_event(
        f"[W{worker_id}] SYSTEMIC FAILURE BREAKER tripped ({SYSTEMIC_FAIL_BREAKER} consecutive "
        f"no-result/timeout) -- almost certainly an auth/API/CDP outage, not the jobs. "
        f"Halting and keeping {len(urls)} job(s) retryable. Check the apply agent's login/auth."
    )
    _stop_event.set()


def _duration_watchdog(max_seconds: float) -> None:
    """Wall-clock bound for a timed continuous run ("run for 5 hours"). After
    max_seconds -- unless the run already stopped for another reason -- set the stop
    event so workers finish their in-flight job and exit cleanly (we let the current
    application complete rather than abandon a half-filled form). Module-level so it's
    unit-testable. No-op for max_seconds <= 0."""
    if max_seconds <= 0:
        return
    if _stop_event.wait(timeout=max_seconds):
        return  # already stopped by another path (cost cap / breaker / Ctrl+C / drained)
    add_event(f"[run] max duration {max_seconds:.0f}s ({max_seconds / 3600:.1f}h) reached -- "
              f"stopping after the in-flight job finishes")
    _stop_event.set()


def worker_loop(worker_id: int = 0, limit: int = 1,
                target_url: str | None = None,
                min_score: int = 7, headless: bool = False,
                model: str | None = "sonnet", dry_run: bool = False,
                agent: str = "claude", browser: str | None = None,
                supervised: bool = False) -> tuple[int, int]:
    """Run jobs sequentially until limit is reached or queue is empty.

    Args:
        worker_id: Numeric worker identifier.
        limit: Max jobs to process (0 = continuous).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome headless.
        model: Apply-agent model name.
        dry_run: Don't click Submit.
        agent: Apply-agent CLI to run: claude or codex.
        browser: Browser to drive (chrome/edge/...). A browser whose profile lacks the
            LinkedIn session (e.g. edge) is auto-restricted to the offsite lane.
        supervised: When True (the --auth-gated owner-supervised lane), passed
            through to run_job so record_tenant_outcome fires on the real
            terminal status, and a challenge-class result (captcha/
            login_issue/auth_required) halts that tenant for the rest of the
            UTC day via handle_auth_gated_result -- the loop then continues
            with other hosts instead of retry-storming a wall it can't solve.

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
    # Whether this worker's browser holds the LinkedIn session. Chrome-family clones
    # carry it; Edge can't decrypt Chrome's cookies, so an edge worker is offsite-only
    # (it would just bounce LinkedIn jobs auth_required). Tunable via env -- add 'edge'
    # if you've logged into LinkedIn in real Edge so the clone carries that session.
    _li_browsers = {b.strip().lower() for b in
                    (os.environ.get("APPLYPILOT_LINKEDIN_BROWSERS") or "chrome,cft,chromium,default").split(",")
                    if b.strip()}
    browser_li_ok = (browser or "chrome").strip().lower() in _li_browsers
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
            # Browser without the LinkedIn session (e.g. edge) -> offsite lane only,
            # so it never grabs a LinkedIn job it can't finish. Else the shared
            # halt/cap gate below decides.
            _fleet_dsn = os.environ.get("FLEET_PG_DSN")
            exclude_li = (
                (not browser_li_ok) or _linkedin_halt.is_set()
                or (bool(_fleet_dsn) and fleet_linkedin_active(_fleet_dsn))
            )
            if not exclude_li:
                try:
                    _conn = get_connection()
                    if _linkedin_halt_active(_conn):
                        # Shared halt: some process hit a LinkedIn challenge/rate-limit
                        # recently -> every process pauses the LinkedIn lane.
                        exclude_li = True
                        if not _li_cap_announced.is_set():
                            _li_cap_announced.set()
                            add_event(f"[W{worker_id}] LinkedIn paused (recent challenge/rate-limit, "
                                      f"shared across agents) -- offsite lane continues")
                    elif li_cap > 0 and _linkedin_today(_conn) >= li_cap:
                        exclude_li = True
                        if not _li_cap_announced.is_set():
                            _li_cap_announced.set()
                            add_event(f"[W{worker_id}] LinkedIn daily cap "
                                      f"{li_cap} reached -- continuing offsite lane only")
                except Exception:
                    logger.debug("LinkedIn gating check failed", exc_info=True)

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
            # Optional clean exhaustion-exit for an UNATTENDED run: if nothing is
            # acquirable for APPLYPILOT_EXIT_AFTER_EMPTY_POLLS consecutive polls, stop
            # instead of spinning forever. This also breaks the all-lanes-paused case
            # (LinkedIn capped/halted AND every offsite host breaker-halted -> acquire
            # returns None every poll). 0 (default) keeps the classic "wait forever for
            # new jobs" behavior, so an attended run polling for fresh discovery is
            # unaffected.
            _exit_after = int(os.environ.get("APPLYPILOT_EXIT_AFTER_EMPTY_POLLS") or 0)
            if _exit_after > 0 and empty_polls >= _exit_after:
                add_event(f"[W{worker_id}] Nothing acquirable for {empty_polls} polls "
                          f"(~{empty_polls * POLL_INTERVAL}s) -- exiting continuous run")
                update_state(worker_id, status="done", last_action="queue drained")
                break
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

        # Pre-launch liveness probe (off by default; the supervised/production run sets
        # APPLYPILOT_PREFLIGHT_LIVENESS=1). A closed-but-HTTP-200 posting (role
        # filled/closed but the page still renders) otherwise burns a full Chrome
        # launch + agent run (~$1.50) before the agent reaches RESULT:EXPIRED. The
        # liveness classifier already catches many of these with ONE read-only GET
        # (closure-text phrases + ATS JSON-API 404s) and is CONSERVATIVE -- 401/403/
        # 429/999/5xx -> UNCERTAIN, so a LinkedIn/Cloudflare wall never false-skips.
        # Only a strong DEAD signal skips the launch; the row is parked dead (retained
        # for training, never re-acquired). LIVE/UNCERTAIN proceed.
        if not dry_run and not target_url and _preflight_liveness_enabled():
            try:
                from applypilot.apply.liveness import probe_url as _probe_url, DEAD as _LV_DEAD
                _ls, _lr = _probe_url(_apply_target(job))
            except Exception:
                _ls, _lr = "uncertain", "probe_import_error"
            if _ls == _LV_DEAD:
                mark_result(job["url"], "failed", "expired", permanent=True)
                _stamp_liveness_dead(job["url"], f"preflight_{_lr}")
                failed += 1
                update_state(worker_id, jobs_failed=failed, jobs_done=applied + failed)
                add_event(f"[W{worker_id}] Preflight DEAD ({str(_lr)[:24]}); "
                          f"skipped, saved a launch: {job['title'][:30]}")
                jobs_done += 1
                _note_healthy_outcome()  # a definitive closed signal = env is fine
                continue

        # Account-safety throttle: respect the per-host gap before launching
        # (skipped in dry-run so canary tests stay fast).
        if not dry_run:
            _tgt = _apply_target(job)
            _throttle_before_apply(_tgt)
            # Cross-process LinkedIn pacing on top of the in-memory per-host gap: wait
            # out the gap relative to the most recent LinkedIn attempt by ANY agent/
            # process (the in-memory throttle can't see other processes).
            if "linkedin" in _throttle_host(_tgt):
                try:
                    _lw = _linkedin_gap_wait(get_connection(), job["url"])
                    if _lw > 0:
                        _stop_event.wait(timeout=_lw)
                except Exception:
                    logger.debug("LinkedIn cross-process gap wait failed", exc_info=True)
            if _stop_event.is_set():
                release_lock(job["url"])
                break

        # Re-stamp the lease clock at LAUNCH time (after the pre-launch throttle +
        # LinkedIn pacing waits, which can add minutes). The periodic reclaimer keys
        # on last_attempted_at; without this, a long throttle wait + the full agent
        # timeout could push lease-age past STALE_LEASE and let the reclaimer steal a
        # still-live lease -> DOUBLE SUBMIT. Re-stamping bounds lease-age to the agent
        # runtime (<= AGENT_TIMEOUT), comfortably under STALE_LEASE. It also tightens
        # cross-process LinkedIn pacing, which reads last_attempted_at as a submit proxy.
        if not dry_run:
            try:
                _lc = get_connection()
                _lc.execute("UPDATE jobs SET last_attempted_at = ? WHERE url = ?",
                            (datetime.now(timezone.utc).isoformat(), job["url"]))
                _lc.commit()
            except Exception:
                logger.debug("[W%d] lease re-stamp failed", worker_id, exc_info=True)

        chrome_proc = None
        try:
            add_event(f"[W{worker_id}] Launching Chrome...")
            chrome_proc = launch_chrome(worker_id, port=port, headless=headless, browser=browser)

            result, duration_ms = run_job(job, port=port, worker_id=worker_id,
                                            model=model, dry_run=dry_run,
                                            agent=agent, supervised=supervised)

            if (
                not dry_run
                and _is_auth_required_result(result)
                and _inbox_auth_enabled()
            ):
                add_event(f"[W{worker_id}] AUTH_REQUIRED detected; polling inbox for verification hint")
                inbox_hint = _poll_inbox_auth_hint(job)
                if inbox_hint:
                    add_event(f"[W{worker_id}] Inbox hint found; retrying with assistance")
                    result, duration_ms = run_job(
                        job,
                        port=port,
                        worker_id=worker_id,
                        model=model,
                        dry_run=dry_run,
                        agent=agent,
                        inbox_auth_hint=inbox_hint,
                        supervised=supervised,
                    )

            # --auth-gated same-day halt: a challenge-class result means this
            # tenant hit a wall the agent can't solve unattended -- halt it
            # for the rest of the UTC day and keep applying to other hosts
            # rather than retry-storming the same wall.
            if supervised and not dry_run:
                try:
                    _host = tenants_mod._host_of(_apply_target(job))
                    if _host and handle_auth_gated_result(get_connection(), _host, result):
                        with _host_halt_lock:
                            _offsite_halted_hosts.add(_host)
                        add_event(f"[W{worker_id}] {_host} halted for the day (auth-gated challenge: {result})")
                except Exception:
                    logger.debug("[W%d] handle_auth_gated_result failed", worker_id, exc_info=True)

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
                _note_healthy_outcome()  # proof-of-life: reset the systemic streak
            else:
                reason = result.split(":", 1)[-1] if ":" in result else result
                mark_result(job["url"], "failed", reason,
                            permanent=_is_permanent_failure(result),
                            duration_ms=duration_ms)
                # Closed-on-visit -> also record as a liveness fact so the closure
                # carries beyond this row's apply_error (freshness/liveness/reporting).
                if reason in DEAD_ON_VISIT_REASONS:
                    _stamp_liveness_dead(job["url"], f"apply_{reason}")
                failed += 1
                update_state(worker_id, jobs_failed=failed,
                             jobs_done=applied + failed)
                # Global systemic-failure breaker: a string of pure no-result/timeout
                # failures means the environment died (auth/API/CDP), not the jobs --
                # halt before the whole queue is burned and keep the streak retryable.
                # Any other failure is proof-of-life (the agent reached the page) -> reset.
                if _is_systemic_failure(reason):
                    if _note_systemic_failure(job["url"]):
                        _trip_systemic_breaker(worker_id)
                        break
                else:
                    _note_healthy_outcome()
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
            release_lock(job["url"])  # keeps the job retryable
            failed += 1
            update_state(worker_id, jobs_failed=failed)
            # A launcher error (Chrome/CDP failed to come up) is systemic, not job-
            # specific -> feed the global breaker so a broken browser halts the run.
            if _note_systemic_failure(job["url"]):
                _trip_systemic_breaker(worker_id)
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
         min_score: int = 7, headless: bool = False, model: str | None = "sonnet",
         dry_run: bool = False, continuous: bool = False,
         poll_interval: int = 60, workers: int = 1,
         agent: str = "claude", agents: list[str] | None = None,
         browsers: list[str] | None = None,
         supervised: bool = False) -> None:
    """Launch the apply pipeline.

    Args:
        limit: Max jobs to apply to (0 or with continuous=True means run forever).
        target_url: Apply to a specific URL.
        min_score: Minimum fit_score threshold.
        headless: Run Chrome in headless mode.
        model: Apply-agent model name (claude only; codex uses its default).
        dry_run: Don't click Submit.
        continuous: Run forever, polling for new jobs.
        poll_interval: Seconds between DB polls when queue is empty.
        workers: Number of parallel workers (default 1).
        agent: Apply-agent CLI for all workers when `agents` is not given.
        agents: Optional per-worker agent list (e.g. ["claude", "codex"]), assigned
            round-robin across workers so claude and codex run concurrently in ONE
            process -- which keeps the per-host throttle, jitter, and offsite breaker
            (all in-memory) SHARED across the mixed agents. The shared queue lease
            still guarantees each job is taken by exactly one worker.
        supervised: The --auth-gated owner-supervised lane (Task 5). Threaded
            through to worker_loop/run_job so record_tenant_outcome fires on
            the real terminal status and a challenge-class result halts that
            tenant for the day.
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
    global _systemic_fail_count
    with _systemic_fail_lock:
        _systemic_fail_count = 0
        _systemic_recent.clear()

    agent = _normalize_agent(agent)
    # Per-worker agent assignment: round-robin the `agents` list across workers so
    # e.g. --agents claude,codex runs worker 0 on claude and worker 1 on codex in
    # ONE process (shared throttle/breaker). Falls back to a single agent for all.
    if agents:
        worker_agents = [_normalize_agent(agents[i % len(agents)]) for i in range(workers)]
    else:
        worker_agents = [agent] * workers

    # Per-worker BROWSER assignment (round-robin), mirroring agents: e.g.
    # browsers=["chrome","edge"] runs worker 0 on Chrome, worker 1 on Edge in ONE
    # process. None -> every worker uses the default (CHROME_PATH / get_chrome_path).
    if browsers:
        worker_browsers = [browsers[i % len(browsers)].strip().lower() for i in range(workers)]
    else:
        worker_browsers = [None] * workers

    def _worker_model(wa: str) -> str | None:
        # claude uses the chosen model (sonnet default); codex uses its own default.
        return model if wa == "claude" else None

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

    # Periodic DB safeguard: a continuous run only backs up on exit, so a corruption
    # mid-run (the 2026-06-23 incident: SQLite 'disk I/O error' under sustained writes,
    # likely AV touching the -wal) could destroy hours of progress AND keep the run
    # limping on a broken DB. Every APPLYPILOT_BACKUP_INTERVAL seconds (default 600,
    # 0=off): integrity-check the live DB; if CLEAN, write a rolling online backup; if
    # CORRUPT, HALT the run immediately so it can't do more damage or waste launches.
    def _periodic_backup() -> None:
        interval = int(os.environ.get("APPLYPILOT_BACKUP_INTERVAL") or 600)
        if interval <= 0:
            return
        import sqlite3
        bdir = config.DB_PATH.parent / "backups"
        try:
            bdir.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        while not _stop_event.wait(timeout=interval):
            try:
                src = get_connection()
                qc = src.execute("PRAGMA quick_check").fetchone()[0]
                if qc != "ok":
                    add_event(f"[backup] DB INTEGRITY FAILED ({str(qc)[:40]}) -- stopping run to "
                              f"prevent further corruption; restore from backups/ or .applypilot")
                    _stop_event.set()
                    break
                tmp = bdir / "rolling.db.tmp"
                dest = sqlite3.connect(str(tmp))
                src.backup(dest)
                dest.close()
                tmp.replace(bdir / "rolling.db")
                # Also refresh the OFF-MACHINE (OneDrive-synced) copy. The local rolling
                # backup shares a disk with the authoritative DB; this survives a local-
                # disk loss. No-op when the DB already lives in the synced dir.
                offsite = mirror_db_offsite()
                add_event("[backup] integrity OK; rolling backup written"
                          + ("; offsite mirror refreshed" if offsite else ""))
            except Exception:
                logger.debug("Periodic backup failed", exc_info=True)
    threading.Thread(target=_periodic_backup, daemon=True, name="db-backup").start()

    # Wall-clock bound for a timed continuous run (e.g. "run for 5 hours"). Without it,
    # --continuous only stops on cost cap / queue exhaustion / breaker / Ctrl+C. This
    # daemon sets _stop_event after APPLYPILOT_MAX_DURATION_SECONDS (0 = no bound); the
    # workers then finish their in-flight job and exit cleanly (we deliberately let the
    # current application complete rather than abandoning a half-filled form).
    _max_dur = int(os.environ.get("APPLYPILOT_MAX_DURATION_SECONDS") or 0)
    if _max_dur > 0:
        console.print(f"[dim]Max duration armed: {_max_dur / 3600:.1f}h "
                      f"(run self-stops after the in-flight job; cost cap is the backstop)[/dim]")
        threading.Thread(target=_duration_watchdog, args=(_max_dur,),
                         daemon=True, name="max-duration").start()

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
    _agent_label = "+".join(sorted(set(worker_agents)))
    console.print(
        f"Launching apply pipeline ({mode_label}, {worker_label}, "
        f"{_agent_label} agent{'s' if len(set(worker_agents)) > 1 else ''}, "
        f"poll every {POLL_INTERVAL}s)..."
    )
    console.print("[dim]Ctrl+C = skip current job(s) | Ctrl+C x2 = stop[/dim]")

    # Double Ctrl+C handler
    _ctrl_c_count = 0

    def _sigint_handler(sig, frame):
        nonlocal _ctrl_c_count
        _ctrl_c_count += 1
        if _ctrl_c_count == 1:
            console.print("\n[yellow]Skipping current job(s)... (Ctrl+C again to STOP)[/yellow]")
            # Kill all active apply-agent processes to skip current jobs
            with _agent_lock:
                for wid, cproc in list(_agent_procs.items()):
                    if cproc.poll() is None:
                        _kill_process_tree(cproc.pid)
        else:
            console.print("\n[red bold]STOPPING[/red bold]")
            _stop_event.set()
            with _agent_lock:
                for wid, cproc in list(_agent_procs.items()):
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
                    model=_worker_model(worker_agents[0]),
                    dry_run=dry_run,
                    agent=worker_agents[0],
                    browser=worker_browsers[0],
                    supervised=supervised,
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
                            model=_worker_model(worker_agents[i]),
                            dry_run=dry_run,
                            agent=worker_agents[i],
                            browser=worker_browsers[i],
                            supervised=supervised,
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
        # Auto-monitor: surface any likely-duplicate applications right at the end of
        # the run, so a double-apply that slipped the live guard is caught immediately
        # (not weeks later via a "Duplicate Application Received" email).
        try:
            dups = audit_duplicate_applications()
            if dups:
                console.print(
                    f"\n[bold yellow]⚠ {len(dups)} possible DUPLICATE application(s) detected[/bold yellow] "
                    f"-- review with [bold]applypilot audit-duplicates[/bold]:")
                for d in dups[:8]:
                    console.print(f"  [yellow]{d['employer']}[/yellow]: "
                                  f"\"{d['title_a'][:40]}\" vs \"{d['title_b'][:40]}\"")
        except Exception:
            logger.debug("Duplicate audit failed", exc_info=True)
        console.print(f"Logs: {config.LOG_DIR}")

    except KeyboardInterrupt:
        pass
    except Exception:
        # Capture an uncaught crash to a file with a full traceback -- the Rich Live
        # console can otherwise swallow it, leaving a silent death that's hard to
        # diagnose. (An OOM/external kill produces no exception; the supervisor catches
        # that via the subprocess dying.)
        import traceback as _tb
        try:
            with open(config.LOG_DIR / "apply_crash.log", "a", encoding="utf-8") as _f:
                _f.write(f"\n=== apply crash {datetime.now(timezone.utc).isoformat()} ===\n")
                _tb.print_exc(file=_f)
        except Exception:
            pass
        raise
    finally:
        _stop_event.set()
        kill_all_chrome()
