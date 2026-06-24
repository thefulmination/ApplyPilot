"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Mapping, Optional

import typer
from rich.console import Console
from rich.table import Table

from applypilot import __version__

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)

app = typer.Typer(
    name="applypilot",
    help="AI-powered end-to-end job application pipeline.",
    no_args_is_help=True,
)
console = Console()
log = logging.getLogger(__name__)

# Valid pipeline stages (in execution order)
VALID_STAGES = ("discover", "enrich", "score", "audit", "diagnose", "tailor", "cover", "pdf")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bootstrap() -> None:
    """Common setup: load env, create dirs, init DB."""
    from applypilot.config import load_env, ensure_dirs
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    init_db()


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"[bold]applypilot[/bold] {__version__}")
        raise typer.Exit()


def _llm_status_from_env(env: Mapping[str, str]) -> tuple[str, str]:
    """Return a normalized provider status for doctor output."""
    provider = (env.get("LLM_PROVIDER") or "").strip().lower()
    model = (env.get("LLM_MODEL") or "").strip()

    if env.get("DEEPSEEK_API_KEY") and (provider == "deepseek" or model.lower().startswith("deepseek")):
        return "ok", f"DeepSeek ({model or 'deepseek-chat'})"
    if env.get("GEMINI_API_KEY"):
        return "ok", f"Gemini ({model or 'gemini-2.0-flash'})"
    if env.get("OPENAI_API_KEY"):
        return "ok", f"OpenAI ({model or 'gpt-4o-mini'})"
    if env.get("LLM_URL"):
        return "ok", f"Local: {env.get('LLM_URL')}"
    return "missing", "Set GEMINI_API_KEY, DEEPSEEK_API_KEY, OPENAI_API_KEY, or LLM_URL in ~/.applypilot/.env"


def _site_rows_for_status(
    rows: list[tuple[str, int]],
    *,
    top_sites: int,
    all_sites: bool,
) -> tuple[list[tuple[str, int]], int]:
    """Limit source rows for readable status output."""
    if all_sites or top_sites <= 0:
        return rows, 0
    return rows[:top_sites], max(0, len(rows) - top_sites)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@app.callback()
def main(
    version: bool = typer.Option(
        False, "--version", "-V",
        help="Show version and exit.",
        callback=_version_callback,
        is_eager=True,
    ),
) -> None:
    """ApplyPilot — AI-powered end-to-end job application pipeline."""


@app.command()
def init() -> None:
    """Run the first-time setup wizard (profile, resume, search config)."""
    from applypilot.wizard.init import run_wizard

    run_wizard()


@app.command()
def run(
    stages: Optional[list[str]] = typer.Argument(
        None,
        help=(
            "Pipeline stages to run. "
            f"Valid: {', '.join(VALID_STAGES)}, all. "
            "Defaults to 'all' if omitted."
        ),
    ),
    min_score: Optional[int] = typer.Option(None, "--min-score", help="Minimum fit score for tailor/cover stages. Defaults to APPLYPILOT_MIN_SCORE or 7."),
    batch_size: int = typer.Option(
        900,
        "--batch-size",
        help="Maximum jobs per tailor/cover/pdf stage run. Use 0 for all eligible jobs.",
    ),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel threads for discovery/enrichment stages."),
    stream: bool = typer.Option(False, "--stream", help="Run stages concurrently (streaming mode)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview stages without executing."),
    validation: str = typer.Option(
        "normal",
        "--validation",
        help=(
            "Validation strictness for tailor/cover stages. "
            "strict: banned words = errors, judge must pass. "
            "normal: banned words = warnings only (default, recommended for Gemini free tier). "
            "lenient: banned words ignored, LLM judge skipped (fastest, fewest API calls)."
        ),
    ),
    discover_mode: str = typer.Option(
        "safe",
        "--discover-mode",
        help=(
            "Discovery breadth/concurrency mode. "
            "safe: source-level parallelism with conservative high-risk sources. "
            "fast: daily mode that narrows expensive company-watchlist crawls. "
            "full: broad crawl using configured source breadth."
        ),
    ),
) -> None:
    """Run pipeline stages: discover, enrich, score, audit, diagnose, tailor, cover, pdf."""
    _bootstrap()

    from applypilot import config
    from applypilot.pipeline import run_pipeline

    if min_score is None:
        min_score = config.get_min_score()

    stage_list = stages if stages else ["all"]

    # Validate stage names
    for s in stage_list:
        if s != "all" and s not in VALID_STAGES:
            console.print(
                f"[red]Unknown stage:[/red] '{s}'. "
                f"Valid stages: {', '.join(VALID_STAGES)}, all"
            )
            raise typer.Exit(code=1)

    # Gate AI stages behind Tier 2
    llm_stages = {"score", "diagnose", "tailor", "cover"}
    if any(s in stage_list for s in llm_stages) or "all" in stage_list:
        from applypilot.config import check_tier
        check_tier(2, "AI scoring/tailoring")

    # Validate the --validation flag value
    valid_modes = ("strict", "normal", "lenient")
    if validation not in valid_modes:
        console.print(
            f"[red]Invalid --validation value:[/red] '{validation}'. "
            f"Choose from: {', '.join(valid_modes)}"
        )
        raise typer.Exit(code=1)
    if batch_size < 0:
        console.print("[red]Invalid --batch-size:[/red] use 0 or a positive number.")
        raise typer.Exit(code=1)
    valid_discover_modes = ("safe", "fast", "full")
    if discover_mode not in valid_discover_modes:
        console.print(
            f"[red]Invalid --discover-mode value:[/red] '{discover_mode}'. "
            f"Choose from: {', '.join(valid_discover_modes)}"
        )
        raise typer.Exit(code=1)

    result = run_pipeline(
        stages=stage_list,
        min_score=min_score,
        batch_size=batch_size,
        dry_run=dry_run,
        stream=stream,
        workers=workers,
        validation_mode=validation,
        discover_mode=discover_mode,
    )

    if result.get("errors"):
        raise typer.Exit(code=1)


@app.command("rescore-jobs")
def rescore_jobs(
    limit: int = typer.Option(
        100,
        "--limit",
        "-l",
        help="Maximum already-scored jobs to rescore with the current LLM and preference profile. Use 0 for all.",
    ),
) -> None:
    """Re-score existing jobs, useful after updating the human preference profile."""
    _bootstrap()

    from applypilot.config import check_tier
    from applypilot.scoring.scorer import run_scoring

    check_tier(2, "AI scoring")
    if limit < 0:
        console.print("[red]Invalid --limit:[/red] use 0 or a positive number.")
        raise typer.Exit(code=1)

    result = run_scoring(limit=limit, rescore=True)
    console.print("\n[bold green]Preference-aware rescore complete[/bold green]")
    console.print(f"  Jobs rescored: {result['scored']}")
    console.print(f"  Errors:        {result['errors']}")
    console.print(f"  Time:          {result['elapsed']:.1f}s")


@app.command("dedupe-jobs")
def dedupe_jobs_command(
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview duplicate markings without changing the database."),
) -> None:
    """Mark same-job duplicates across different source URLs."""
    _bootstrap()

    from applypilot.database import dedupe_existing_jobs

    result = dedupe_existing_jobs(dry_run=dry_run)
    title = "Same-job duplicate preview" if dry_run else "Same-job duplicate pass complete"
    console.print(f"\n[bold green]{title}[/bold green]")
    console.print(f"  Jobs processed:      {result['processed']}")
    console.print(f"  Jobs with key:       {result['keys']}")
    console.print(f"  Duplicate groups:    {result['groups']}")
    console.print(f"  Duplicates marked:   {result['duplicates']}")
    if dry_run:
        console.print("\n[yellow]Dry run only.[/yellow] Run without --dry-run to write duplicate markers.")


@app.command("verify-live")
def verify_live_command(
    tiers: str = typer.Option(
        "priority,recommended", "--tiers",
        help="Comma-separated audit_label tiers to verify.",
    ),
    max_age_days: int = typer.Option(
        7, "--max-age-days",
        help="Skip jobs already verified within this many days (0 = re-check all).",
    ),
    limit: int = typer.Option(0, "--limit", "-l", help="Max jobs to probe. 0 = all eligible."),
    workers: int = typer.Option(
        16, "--workers", "-w",
        help="Concurrent probe threads (per-host throttling still applies).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Probe and report without writing liveness columns."),
) -> None:
    """Verify scored postings are still open (READ-ONLY) and mark dead ones so apply skips them.

    Never deletes jobs: dead postings stay in the DB with their full description
    and scores (plus the close-marker) for future training. Apply selection
    simply excludes liveness_status = 'dead'; uncertain/unchecked still apply.
    """
    _bootstrap()

    from applypilot.apply import liveness
    from applypilot.database import get_connection

    if limit < 0:
        console.print("[red]Invalid --limit:[/red] use 0 or a positive number.")
        raise typer.Exit(code=1)

    tier_list = [t.strip() for t in tiers.split(",") if t.strip()]
    conn = get_connection()

    def _progress(done: int, total: int, _results: list) -> None:
        console.print(f"  probed {done}/{total}…", end="\r")

    result = liveness.verify_jobs(
        conn, tiers=tier_list, max_age_days=max_age_days,
        limit=limit, workers=workers, dry_run=dry_run, progress=_progress,
    )
    by = result["by_status"]
    title = "Liveness check (dry run)" if dry_run else "Liveness check complete"
    console.print(f"\n[bold green]{title}[/bold green]")
    console.print(f"  Candidates:   {result['candidates']}  (skipped fresh: {result['skipped_fresh']})")
    console.print(f"  Probed:       {result['checked']}")
    console.print(f"  Live:         {by.get('live', 0)}")
    console.print(f"  Uncertain:    {by.get('uncertain', 0)}  (kept — apply still considers these)")
    console.print(f"  [red]Dead:         {by.get('dead', 0)}[/red]  (apply will skip; rows retained in DB)")
    if dry_run:
        console.print("\n[yellow]Dry run only.[/yellow] Run without --dry-run to write liveness_status.")


@app.command("resolve-ats-boards")
def resolve_ats_boards_command(
    sources: str = typer.Option(
        "greenhouse,lever,ashby,smartrecruiters,workable", "--sources",
        help="Comma-separated ATS platforms to probe."),
    candidates: int = typer.Option(6, "--candidates", help="Token guesses to try per company per source."),
    workers: int = typer.Option(8, "--workers", "-w", help="Parallel company probers."),
    refresh: bool = typer.Option(False, "--refresh", help="Re-probe companies already in the registry."),
    limit: int = typer.Option(0, "--limit", help="Probe only the first N watchlist companies (0 = all)."),
) -> None:
    """Resolve & persist verified ATS board tokens (corporate_ats.yaml).

    Probes the PUBLIC Greenhouse/Lever/Ashby/SmartRecruiters/Workable JSON APIs for
    each watchlist company and records the working board token, so discovery hits
    known boards directly instead of guessing. Read-only network; writes only the
    registry file (never the jobs table). Additive + idempotent.
    """
    _bootstrap()
    from applypilot import config
    from applypilot.discovery.corporate_ats import resolve_ats_boards

    src = [s.strip() for s in sources.split(",") if s.strip()]
    res = resolve_ats_boards(
        cfg=config.load_search_config(), sources=src,
        candidate_limit=candidates, workers=workers, refresh=refresh, limit=limit,
    )
    console.print("\n[bold green]ATS board resolution complete[/bold green]")
    console.print(f"  companies considered: {res.get('companies', 0)}  (probed this run: {res.get('probed', 0)})")
    console.print(f"  [green]newly resolved companies:[/green] {res.get('resolved', 0)}")
    by = res.get("per_source") or {}
    if by:
        console.print("  by ATS: " + ", ".join(f"{k}={v}" for k, v in sorted(by.items(), key=lambda x: -x[1])))
    console.print(f"  registry total entries: {res.get('registry_total', 0)}")
    console.print(f"  registry: {res.get('registry_path', '')}")
    if res.get("resolved", 0):
        console.print("[dim]Next: run discovery (`applypilot run discover`) to ingest postings from the resolved boards.[/dim]")


@app.command("linkedin-split")
def linkedin_split_command() -> None:
    """LinkedIn apply split: offsite (external ATS, fast lane) vs Easy-Apply/unresolved
    (LinkedIn-paced). Watch the offsite count climb as the extractor resolves links."""
    _bootstrap()
    from urllib.parse import urlparse
    from applypilot.database import get_connection

    def _host(u: str) -> str:
        h = (urlparse(u or "").hostname or "").lower()
        return h[4:] if h.startswith("www.") else h

    conn = get_connection()
    rows = conn.execute(
        "SELECT url, application_url, audit_label FROM jobs "
        "WHERE (lower(site)='linkedin' OR url LIKE '%linkedin.com/jobs%') "
        "AND duplicate_of_url IS NULL "
        "AND COALESCE(liveness_status,'') != 'dead' "
        "AND applied_at IS NULL"
    ).fetchall()
    offsite = easyapply = band_offsite = band_total = 0
    for r in rows:
        au = r["application_url"] or ""
        is_offsite = au.startswith("http") and "linkedin" not in _host(au)
        if is_offsite:
            offsite += 1
        else:
            easyapply += 1
        if r["audit_label"] in ("priority", "recommended"):
            band_total += 1
            if is_offsite:
                band_offsite += 1
    console.print("\n[bold]LinkedIn apply split[/bold] (live, not-yet-applied)")
    console.print(f"  total LinkedIn jobs:                       {len(rows)}")
    console.print(f"  [green]offsite (fast external lane):[/green]              {offsite}")
    console.print(f"  [yellow]Easy-Apply / unresolved (LinkedIn-paced):[/yellow]  {easyapply}")
    console.print(
        f"  apply-band (priority/recommended): {band_total}   "
        f"(offsite there: {band_offsite})"
    )
    if offsite == 0:
        console.print(
            "[dim]No offsite URLs resolved yet — have the extractor emit companyApplyUrl, "
            "then re-run sync_linkedin_picks.py to backfill application_url.[/dim]"
        )
    conn.close()


@app.command("apply-failures")
def apply_failures_command(
    reason: Optional[str] = typer.Option(None, "--reason", help="Filter to one reason (e.g. captcha, auth_required, expired, no_result_line)."),
    manual: bool = typer.Option(False, "--manual", help="Only the recoverable 'apply by hand' candidates (auth/login/captcha)."),
    limit: int = typer.Option(40, "--limit", "-l", help="Max rows to list. 0 = all."),
    export: Optional[str] = typer.Option(None, "--export", help="Write all matching failures to a CSV at this path."),
) -> None:
    """Review WHY apply attempts bounced, grouped, with an 'apply by hand' shortlist.

    Every bounce is persisted on the jobs table (apply_status + apply_error, never
    deleted), so this reflects all runs. Categories: recoverable (login/captcha walls
    you can clear by hand), dead (gone/ineligible -- skip), agent (pipeline hiccup),
    blocked (rate-limited/blocked -- retry later).
    """
    _bootstrap()
    from collections import Counter
    from applypilot import config
    from applypilot.database import get_connection

    RECOVERABLE = {"auth_required", "login_issue", "sso_required", "account_required",
                   "email_verification_required", "two_factor_required", "2fa_required",
                   "mfa_required", "captcha", "linkedin_challenge"}
    DEAD = {"expired", "page_error", "not_eligible_location", "not_eligible_salary",
            "not_eligible_work_auth", "already_applied", "not_a_job_application",
            "bad_application_url", "unsafe_permissions", "unsafe_verification"}
    BLOCKED = {"linkedin_rate_limited", "rate_limited", "site_blocked",
               "cloudflare_blocked", "blocked_by_cloudflare"}
    AGENT = {"no_result_line", "timeout", "stuck", "suspicious_page", "unknown"}

    def category(r: str) -> str:
        if r in RECOVERABLE: return "recoverable"
        if r in DEAD: return "dead"
        if r in BLOCKED: return "blocked"
        if r in AGENT: return "agent"
        return "other"

    conn = get_connection()
    rows = conn.execute(
        "SELECT COALESCE(apply_error, apply_status) AS reason, apply_status, "
        "       site, company, title, COALESCE(application_url, url) AS url, "
        "       substr(last_attempted_at, 1, 19) AS at "
        "FROM jobs "
        "WHERE apply_status IS NOT NULL AND apply_status NOT IN ('applied','in_progress','manual') "
        "  AND duplicate_of_url IS NULL "
        "ORDER BY last_attempted_at DESC"
    ).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    for it in items:
        it["category"] = category((it["reason"] or "").strip())
    if reason:
        items = [it for it in items if (it["reason"] or "") == reason]
    if manual:
        items = [it for it in items if it["category"] == "recoverable"]

    if not items:
        console.print("[green]No matching apply failures recorded.[/green]")
        return

    by_cat = Counter(it["category"] for it in items)
    by_reason = Counter((it["reason"] or "?") for it in items)
    console.print(f"\n[bold]Apply failures: {len(items)} recorded[/bold]  "
                  f"({', '.join(f'{c}={n}' for c, n in by_cat.most_common())})")
    console.print("  by reason: " + ", ".join(f"{r}={n}" for r, n in by_reason.most_common()))

    recov = [it for it in items if it["category"] == "recoverable"]
    if recov and not reason:
        console.print(f"\n[bold yellow]Apply by hand ({len(recov)}) — login/captcha walls; use `assist-apply` or apply manually:[/bold yellow]")
        for it in recov[: (limit or len(recov))]:
            console.print(f"  [{it['reason']}] {(it['company'] or it['site'] or '?')[:22]:22} | "
                          f"{(it['title'] or '')[:38]:38} | {it['url']}")

    console.print("\n[bold]All matching (newest first):[/bold]")
    for it in items[: (limit or len(items))]:
        console.print(f"  {it['at']} | {it['category']:11} | {(it['reason'] or '?'):18} | "
                      f"{(it['company'] or it['site'] or '?')[:18]:18} | {(it['title'] or '')[:30]}")
    if limit and len(items) > limit:
        console.print(f"  [dim]... +{len(items) - limit} more — raise --limit or --export to CSV[/dim]")

    console.print(f"\n[dim]Full per-job agent reasoning: {config.LOG_DIR}\\{{claude,codex}}_<ts>_<company>.txt[/dim]")

    if export:
        import csv
        from pathlib import Path
        p = Path(export)
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["last_attempted_at", "reason", "category", "apply_status",
                        "company", "site", "title", "url"])
            for it in items:
                w.writerow([it["at"], it["reason"], it["category"], it["apply_status"],
                            it["company"], it["site"], it["title"], it["url"]])
        console.print(f"[green]Wrote {len(items)} failures to {p}[/green]")


@app.command("smart-health")
def smart_health_command(
    all_sites: bool = typer.Option(False, "--all", help="Show healthy sources too."),
) -> None:
    """Show Smart Extract source issues, cooldowns, timeouts, and challenge pages."""
    _bootstrap()

    from applypilot.discovery import smartextract

    health = smartextract.load_smart_health()
    rows = smartextract.summarize_smart_health(health)
    if not all_sites:
        rows = [
            row for row in rows
            if row["cooling_down"] or row["issue_type"] or row["failures"] or row["timeouts"] or row["challenges"]
        ]

    if not rows:
        console.print("[green]No Smart Extract source health issues recorded.[/green]")
        console.print(f"Health file: {smartextract.SMART_HEALTH_PATH}")
        return

    table = Table(title="Smart Extract Source Health")
    table.add_column("Source")
    table.add_column("Status")
    table.add_column("Issue")
    table.add_column("Failures", justify="right")
    table.add_column("Timeouts", justify="right")
    table.add_column("Challenges", justify="right")
    table.add_column("Cooldown")
    table.add_column("Jobs", justify="right")
    table.add_column("Avg s", justify="right")
    table.add_column("URL")

    for row in rows:
        cooldown = "yes" if row["cooling_down"] else ""
        table.add_row(
            str(row["source"]),
            str(row["status"]),
            str(row["issue_type"]),
            str(row["failures"]),
            str(row["timeouts"]),
            str(row["challenges"]),
            cooldown,
            str(row["last_jobs_found"]),
            f"{row['average_runtime_seconds']:.1f}",
            str(row["last_url"])[:80],
        )

    console.print(table)
    console.print(f"Health file: {smartextract.SMART_HEALTH_PATH}")


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(2, "--workers", "-w", help="Number of parallel browser workers. Account-safe: the LinkedIn daily cap and per-host throttle are process-global, shared across workers."),
    min_score: Optional[int] = typer.Option(None, "--min-score", help="Minimum fit score for job selection. Defaults to APPLYPILOT_MIN_SCORE or 7."),
    agent: str = typer.Option("claude", "--agent", help="Apply agent CLI to run: claude or codex."),
    agents: Optional[str] = typer.Option(None, "--agents", help="Comma-separated per-worker agents (round-robin), e.g. 'claude,codex' to run BOTH concurrently in one process. Overrides --agent; needs --workers >= the number of agents."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Apply-agent model name. Defaults to sonnet for Claude; Codex uses its configured default when omitted."),
    poll_interval: int = typer.Option(15, "--poll-interval", help="Seconds a worker waits between DB polls when the queue is empty."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    base_resume: bool = typer.Option(False, "--base-resume", help="Apply with the base resume as-is (no per-job tailoring); jobs lacking a tailored resume fall back to .applypilot/resume.pdf."),
    max_cost_usd: float = typer.Option(0.0, "--max-cost-usd", help="Stop the run once estimated apply cost reaches this USD amount (0 = no cap)."),
    linkedin_daily_cap: int = typer.Option(-1, "--linkedin-daily-cap", help="Rolling-24h cap on LinkedIn Easy-Apply submissions; offsite lane keeps flowing after the cap. -1 = use default (20), 0 = no cap."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    preflight: bool = typer.Option(True, "--preflight/--skip-preflight", help="Run readiness checks before launching the apply agent."),
    stale_days: int = typer.Option(21, "--stale-days", help="Preflight warning threshold for stale jobs."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset all failed jobs for retry."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _bootstrap()

    from applypilot import config
    from applypilot.config import check_tier, PROFILE_PATH as _profile_path
    from applypilot.database import get_connection

    if min_score is None:
        min_score = config.get_min_score()
    agent = agent.strip().lower()
    if agent not in {"claude", "codex"}:
        console.print("[red]--agent must be either 'claude' or 'codex'.[/red]")
        raise typer.Exit(code=1)
    agent_list: Optional[list] = None
    if agents:
        agent_list = [a.strip().lower() for a in agents.split(",") if a.strip()]
        bad = [a for a in agent_list if a not in {"claude", "codex"}]
        if bad:
            console.print(f"[red]--agents entries must be 'claude' or 'codex' (got: {', '.join(bad)}).[/red]")
            raise typer.Exit(code=1)
        if not agent_list:
            agent_list = None
        elif workers < len(set(agent_list)):
            console.print(f"[yellow]--workers ({workers}) < distinct agents ({len(set(agent_list))}); "
                          f"some agents won't run. Raise --workers.[/yellow]")
    # Distinct agents that will actually run (for the auth canary + model default).
    run_agents = agent_list if agent_list else [agent]
    if not model and "claude" in run_agents:
        model = "sonnet"

    # --base-resume: process-level flag read by acquire_job / build_prompt /
    # readiness so jobs without a tailored resume apply with the base resume.
    if base_resume:
        import os
        os.environ["APPLYPILOT_BASE_RESUME"] = "1"
    if max_cost_usd and max_cost_usd > 0:
        import os
        os.environ["APPLYPILOT_APPLY_MAX_COST"] = str(max_cost_usd)
    if linkedin_daily_cap >= 0:
        import os
        os.environ["APPLYPILOT_LINKEDIN_DAILY_CAP"] = str(linkedin_daily_cap)

    # --- Utility modes (no Chrome/apply-agent needed) ---

    if mark_applied:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_applied, "applied")
        console.print(f"[green]Marked as applied:[/green] {mark_applied}")
        return

    if mark_failed:
        from applypilot.apply.launcher import mark_job
        mark_job(mark_failed, "failed", reason=fail_reason)
        console.print(f"[yellow]Marked as failed:[/yellow] {mark_failed} ({fail_reason or 'manual'})")
        return

    if reset_failed:
        from applypilot.apply.launcher import reset_failed as do_reset
        count = do_reset()
        console.print(f"[green]Reset {count} failed job(s) for retry.[/green]")
        return

    # --- Full apply mode ---

    # Check 1: Tier 3 required (Claude Code CLI or Codex CLI + Chrome)
    check_tier(3, "auto-apply")

    # Check 2: Profile exists
    if not _profile_path.exists():
        console.print(
            "[red]Profile not found.[/red]\n"
            "Run [bold]applypilot init[/bold] to create your profile first."
        )
        raise typer.Exit(code=1)

    # Check 3: resume readiness (skip for --gen with --url)
    if not (gen and url):
        if base_resume:
            if not config.RESUME_PDF_PATH.exists():
                console.print(
                    f"[red]Base resume PDF not found:[/red] {config.RESUME_PDF_PATH}\n"
                    "Place your resume (PDF) at that path to use --base-resume."
                )
                raise typer.Exit(code=1)
        else:
            conn = get_connection()
            ready = conn.execute(
                "SELECT COUNT(*) FROM jobs "
                "WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL AND duplicate_of_url IS NULL"
            ).fetchone()[0]
            if ready == 0:
                console.print(
                    "[red]No tailored resumes ready.[/red]\n"
                    "Run [bold]applypilot run score tailor[/bold] first to prepare applications,\n"
                    "or pass [bold]--base-resume[/bold] to apply with your base resume as-is."
                )
                raise typer.Exit(code=1)

    if gen:
        from applypilot.apply.launcher import BASE_CDP_PORT, build_apply_agent_command, gen_prompt
        import subprocess as _sp
        target = url or ""
        if not target:
            console.print("[red]--gen requires --url to specify which job.[/red]")
            raise typer.Exit(code=1)
        prompt_file = gen_prompt(target, min_score=min_score, model=model)
        if not prompt_file:
            console.print("[red]No matching job found for that URL.[/red]")
            raise typer.Exit(code=1)
        mcp_path = _profile_path.parent / ".mcp-apply-0.json"
        cmd = build_apply_agent_command(
            agent=agent,
            model=model,
            mcp_config_path=mcp_path,
            cdp_port=BASE_CDP_PORT,
        )
        console.print(f"[green]Wrote prompt to:[/green] {prompt_file}")
        console.print("\n[bold]Run manually:[/bold]")
        console.print(f"  {_sp.list2cmdline(cmd)} < {prompt_file}")
        return

    from applypilot.apply.launcher import main as apply_main

    effective_limit = limit if limit is not None else (0 if continuous else 1)

    if preflight:
        from applypilot.apply.readiness import collect_preapply_checks, summarize_checks

        preflight_limit = effective_limit if effective_limit > 0 else 25
        checks = collect_preapply_checks(
            min_score=min_score,
            limit=preflight_limit,
            stale_days=stale_days,
            job_ref=url,
        )
        _print_preapply_checks(checks, "Auto-Apply Preflight")
        summary = summarize_checks(checks)
        if summary["checked"] == 0:
            console.print("[red]No matching ready-to-apply jobs passed the queue filters.[/red]")
            raise typer.Exit(code=1)
        if summary["blocked"]:
            console.print(
                "\n[red]Preflight found blocked job(s).[/red] "
                "Fix them, choose a specific --url, or use --skip-preflight if you intentionally want to proceed."
            )
            raise typer.Exit(code=1)

    # Pre-flight AUTH canary: the apply runner spawns an agent CLI to drive the
    # browser; if it isn't authenticated, every job dies in seconds and the whole
    # run is wasted. Version checks are not enough, so probe with a minimal query.
    # Skipped for --gen.
    if not gen:
        import os as _os
        import subprocess as _sp
        from applypilot.apply.launcher import build_agent_canary_command
        _env = _os.environ.copy()
        _env.pop("CLAUDECODE", None)
        _env.pop("CLAUDE_CODE_ENTRYPOINT", None)
        # Probe EVERY distinct agent that will run (a mixed claude+codex run needs both).
        for _ag in dict.fromkeys(run_agents):
            _ag_model = model if _ag == "claude" else None
            try:
                _r = _sp.run(
                    build_agent_canary_command(_ag, _ag_model),
                    capture_output=True, text=True, timeout=90, env=_env,
                )
                _out = ((_r.stdout or "") + (_r.stderr or "")).lower()
                if _r.returncode != 0 or "not logged in" in _out or "/login" in _out or "please run" in _out:
                    raise RuntimeError(((_r.stdout or "") or (_r.stderr or "") or f"exit {_r.returncode}").strip()[:160])
            except Exception as e:
                console.print(
                    f"[red]{_ag} apply agent not authenticated — aborting before wasting a run.[/red]\n"
                    f"  detail: {e}\n"
                    f"  Authenticate the [bold]{_ag}[/bold] CLI once, or set "
                    f"[bold]{'ANTHROPIC_API_KEY' if _ag == 'claude' else 'Codex auth / CODEX_PATH'}[/bold] — then retry."
                )
                raise typer.Exit(code=1)

    console.print("\n[bold blue]Launching Auto-Apply[/bold blue]")
    console.print(f"  Limit:    {'unlimited' if continuous else effective_limit}")
    console.print(f"  Workers:  {workers}")
    console.print(f"  Agent:    {'+'.join(dict.fromkeys(run_agents))}")
    console.print(f"  Model:    {model or 'codex default'}")
    console.print(f"  Headless: {headless}")
    console.print(f"  Dry run:  {dry_run}")
    if url:
        console.print(f"  Target:   {url}")
    console.print()

    apply_main(
        limit=effective_limit,
        target_url=url,
        min_score=min_score,
        headless=headless,
        model=model,
        dry_run=dry_run,
        continuous=continuous,
        workers=workers,
        poll_interval=poll_interval,
        agent=agent,
        agents=agent_list,
    )


@app.command()
def status(
    top_sites: int = typer.Option(50, "--top-sites", help="Maximum source rows to show. Use 0 for all."),
    all_sites: bool = typer.Option(False, "--all-sites", help="Show every source row."),
) -> None:
    """Show pipeline statistics from the database."""
    _bootstrap()

    from applypilot.database import get_stats

    stats = get_stats()

    console.print("\n[bold]ApplyPilot Pipeline Status[/bold]\n")

    # Summary table
    summary = Table(title="Pipeline Overview", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="bold")
    summary.add_column("Count", justify="right")

    summary.add_row("Total jobs discovered", str(stats["total"]))
    summary.add_row("Same-job duplicates", str(stats.get("duplicates", 0)))
    summary.add_row("Active jobs", str(stats.get("active_total", stats["total"])))
    summary.add_row("With full description", str(stats["with_description"]))
    summary.add_row("Pending enrichment", str(stats["pending_detail"]))
    summary.add_row("Enrichment errors", str(stats["detail_errors"]))
    summary.add_row("Scored by LLM", str(stats["scored"]))
    summary.add_row("Audited/reranked", str(stats.get("audited", 0)))
    summary.add_row("Recommended after audit", str(stats.get("recommended", 0)))
    summary.add_row("Excluded by audit", str(stats.get("audit_excluded", 0)))
    summary.add_row("Diagnosed fits", str(stats.get("diagnosed", 0)))
    summary.add_row("Pending diagnosis", str(stats.get("undiagnosed", 0)))
    summary.add_row("Pending scoring", str(stats["unscored"]))
    summary.add_row("Tailored resumes", str(stats["tailored"]))
    summary.add_row("Pending tailoring (7+)", str(stats["untailored_eligible"]))
    summary.add_row("Cover letters", str(stats["with_cover_letter"]))
    summary.add_row("Ready to apply", str(stats["ready_to_apply"]))
    summary.add_row("Applied", str(stats["applied"]))
    summary.add_row("Apply errors", str(stats["apply_errors"]))
    summary.add_row("Tracked applications", str(stats.get("applications_tracked", 0)))
    summary.add_row("Interviews active", str(stats.get("interviews", 0)))
    summary.add_row("Follow-ups due", str(stats.get("followups_due", 0)))
    summary.add_row("Rejections", str(stats.get("rejections", 0)))

    console.print(summary)

    # Score distribution
    if stats["score_distribution"]:
        dist_table = Table(title="\nScore Distribution", show_header=True, header_style="bold yellow")
        dist_table.add_column("Score", justify="center")
        dist_table.add_column("Count", justify="right")
        dist_table.add_column("Bar")

        max_count = max(count for _, count in stats["score_distribution"]) or 1
        for score, count in stats["score_distribution"]:
            bar_len = int(count / max_count * 30)
            if score >= 7:
                color = "green"
            elif score >= 5:
                color = "yellow"
            else:
                color = "red"
            bar = f"[{color}]{'=' * bar_len}[/{color}]"
            dist_table.add_row(str(score), str(count), bar)

        console.print(dist_table)

    # By site
    if stats["by_site"]:
        site_table = Table(title="\nJobs by Source", show_header=True, header_style="bold magenta")
        site_table.add_column("Site")
        site_table.add_column("Count", justify="right")

        visible_sites, hidden_sites = _site_rows_for_status(stats["by_site"], top_sites=top_sites, all_sites=all_sites)
        for site, count in visible_sites:
            site_table.add_row(site or "Unknown", str(count))

        console.print(site_table)
        if hidden_sites:
            console.print(f"[dim]Showing top {len(visible_sites)} sources; {hidden_sites} hidden. Use --all-sites to show every source.[/dim]")

    console.print()


def _print_tracker(rows: list[dict], title: str) -> None:
    table = Table(title=title, show_header=True, header_style="bold cyan")
    table.add_column("Status")
    table.add_column("Applied")
    table.add_column("Follow-up")
    table.add_column("Score", justify="right")
    table.add_column("Company")
    table.add_column("Role")
    table.add_column("Channel")
    table.add_column("URL")

    for row in rows:
        table.add_row(
            str(row.get("status") or ""),
            str(row.get("applied_at") or "")[:10],
            str(row.get("next_follow_up_at") or "")[:10],
            str(row.get("score") if row.get("score") is not None else ""),
            str(row.get("company") or row.get("site") or ""),
            str(row.get("title") or "")[:50],
            str(row.get("channel") or ""),
            str(row.get("application_url") or row.get("url") or row.get("job_url") or "")[:70],
        )
    console.print(table)


def _print_preapply_checks(checks: list[dict], title: str) -> None:
    from applypilot.apply.readiness import summarize_checks

    summary = summarize_checks(checks)
    console.print(f"\n[bold]{title}[/bold]")
    console.print(
        "  Checked: {checked} | Ready: {ready} | Warnings: {warnings} | Blocked: {blocked}".format(**summary)
    )

    if summary["issue_counts"]:
        issue_text = ", ".join(f"{code}={count}" for code, count in summary["issue_counts"].items())
        console.print(f"  Issues:  {issue_text}")

    table = Table(title="Pre-Apply Findings", show_header=True, header_style="bold cyan")
    table.add_column("State")
    table.add_column("Score", justify="right")
    table.add_column("Company")
    table.add_column("Role")
    table.add_column("Issues")
    table.add_column("URL")

    for row in checks:
        issues = ", ".join(issue["code"] for issue in row.get("issues", []))
        state = str(row.get("severity") or "")
        style = "red" if state == "blocked" else "yellow" if state == "warning" else "green"
        table.add_row(
            f"[{style}]{state}[/{style}]",
            str(row.get("score") if row.get("score") is not None else ""),
            str(row.get("company") or "")[:28],
            str(row.get("title") or "")[:44],
            issues[:60],
            str(row.get("application_url") or row.get("url") or "")[:70],
        )
    console.print(table)


@app.command("track")
def track_command(
    ready: bool = typer.Option(False, "--ready", help="Show jobs ready to apply instead of tracked applications."),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by tracker status, such as applied or recruiter_screen."),
    active: bool = typer.Option(True, "--active/--all", help="Show active tracked applications by default."),
    min_score: int = typer.Option(7, "--min-score", help="Minimum score for --ready jobs."),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum rows to show. Use 0 for all."),
) -> None:
    """Show application tracker rows or the ready-to-apply queue."""
    _bootstrap()

    from applypilot.applications import list_applications, list_ready_to_apply

    if ready:
        rows = list_ready_to_apply(min_score=min_score, limit=limit)
        _print_tracker(rows, f"Ready to Apply (score >= {min_score})")
        return

    rows = list_applications(status=status, active_only=active and status is None, limit=limit)
    _print_tracker(rows, "Application Tracker")


@app.command("preapply-check")
def preapply_check_command(
    min_score: int = typer.Option(7, "--min-score", help="Minimum score for ready jobs."),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum ready jobs to inspect. Use 0 for all."),
    stale_days: int = typer.Option(21, "--stale-days", help="Warn when a job was discovered more than this many days ago."),
    url: Optional[str] = typer.Option(None, "--url", help="Inspect one job URL or application URL."),
) -> None:
    """Audit the ready-to-apply queue before launching browser automation."""
    _bootstrap()

    from applypilot.apply.readiness import collect_preapply_checks, summarize_checks

    checks = collect_preapply_checks(min_score=min_score, limit=limit, stale_days=stale_days, job_ref=url)
    _print_preapply_checks(checks, "Pre-Apply Readiness")
    summary = summarize_checks(checks)
    if summary["blocked"]:
        raise typer.Exit(code=1)


@app.command("assist-apply")
def assist_apply_command(
    job_ref: str = typer.Argument(..., help="Job URL or application URL to finish manually."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the application URL in the default browser."),
    mark_auth_required: bool = typer.Option(True, "--mark/--no-mark", help="Track this job as auth_required before handoff."),
) -> None:
    """Prepare a manual handoff for login/account/2FA-gated applications."""
    _bootstrap()

    import webbrowser
    from applypilot.applications import get_application_handoff, record_application

    handoff = get_application_handoff(job_ref)
    if mark_auth_required:
        record_application(
            job_ref,
            status="auth_required",
            channel="assisted",
            notes="Manual login/account/2FA handoff prepared.",
        )

    console.print("\n[bold]Assisted Application Handoff[/bold]")
    console.print(f"  Role:        {handoff.get('title')} @ {handoff.get('company')}")
    console.print(f"  Score:       {handoff.get('score')}")
    console.print(f"  URL:         {handoff.get('application_url')}")
    console.print(f"  Resume PDF:  {handoff.get('resume_pdf_path') or handoff.get('resume_path') or 'missing'}")
    console.print(f"  Cover PDF:   {handoff.get('cover_letter_pdf_path') or handoff.get('cover_letter_path') or 'missing'}")
    console.print("\nAfter you finish the site login/2FA and submit:")
    console.print(f"  .\\run-applypilot.ps1 mark-applied \"{handoff.get('job_url')}\" --channel company_site")

    if open_browser and handoff.get("application_url"):
        webbrowser.open(str(handoff["application_url"]))


@app.command("mark-applied")
def mark_applied_command(
    job_ref: str = typer.Argument(..., help="Job URL or application URL to mark as applied."),
    channel: str = typer.Option("manual", "--channel", help="Where you applied: company_site, linkedin, email, referral, applypilot, manual."),
    notes: Optional[str] = typer.Option(None, "--notes", help="Optional note to save with the application."),
    follow_up: Optional[str] = typer.Option(None, "--follow-up", help="Optional follow-up date, YYYY-MM-DD."),
) -> None:
    """Mark a job as applied and save the applied date in the tracker."""
    _bootstrap()

    from applypilot.applications import record_application

    row = record_application(
        job_ref,
        status="applied",
        channel=channel,
        notes=notes,
        next_follow_up_at=follow_up,
    )
    console.print("[green]Application tracked.[/green]")
    console.print(f"  Applied: {row.get('applied_at')}")
    console.print(f"  Role:    {row.get('title')} @ {row.get('company')}")


@app.command("update-application")
def update_application_command(
    job_ref: str = typer.Argument(..., help="Job URL or application URL to update."),
    status: str = typer.Option(..., "--status", help="New status, such as followed_up, recruiter_screen, rejected, offer."),
    channel: str = typer.Option("manual", "--channel", help="Where the update happened."),
    notes: Optional[str] = typer.Option(None, "--notes", help="Optional note to save."),
    follow_up: Optional[str] = typer.Option(None, "--follow-up", help="Optional next follow-up date, YYYY-MM-DD."),
    contact_name: Optional[str] = typer.Option(None, "--contact-name", help="Recruiter or hiring contact name."),
    contact_email: Optional[str] = typer.Option(None, "--contact-email", help="Recruiter or hiring contact email."),
    contact_url: Optional[str] = typer.Option(None, "--contact-url", help="Recruiter LinkedIn/profile URL."),
) -> None:
    """Update a tracked application's status, notes, contact, or follow-up date."""
    _bootstrap()

    from applypilot.applications import record_application

    row = record_application(
        job_ref,
        status=status,
        channel=channel,
        notes=notes,
        next_follow_up_at=follow_up,
        contact_name=contact_name,
        contact_email=contact_email,
        contact_url=contact_url,
    )
    console.print("[green]Application updated.[/green]")
    console.print(f"  Status: {row.get('status')}")
    console.print(f"  Role:   {row.get('title')} @ {row.get('company')}")


@app.command("application-history")
def application_history_command(
    job_ref: str = typer.Argument(..., help="Job URL or application URL."),
    limit: int = typer.Option(25, "--limit", "-l", help="Maximum history rows to show."),
) -> None:
    """Show status history for one application."""
    _bootstrap()

    from applypilot.applications import application_events

    rows = application_events(job_ref, limit=limit)
    table = Table(title="Application History", show_header=True, header_style="bold cyan")
    table.add_column("When")
    table.add_column("Status")
    table.add_column("Channel")
    table.add_column("Notes")
    for row in rows:
        table.add_row(
            str(row.get("happened_at") or ""),
            str(row.get("status") or ""),
            str(row.get("channel") or ""),
            str(row.get("notes") or "")[:90],
        )
    console.print(table)


@app.command("export-applications")
def export_applications_command(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination folder. Defaults to a timestamped application_exports folder."),
) -> None:
    """Export the application tracker to CSV and JSONL."""
    _bootstrap()

    from applypilot.applications import export_applications

    result = export_applications(output_dir=output)
    console.print("\n[bold green]Application export complete[/bold green]")
    console.print(f"  Applications exported: {result['applications_exported']}")
    console.print(f"  Folder:                {result['output_dir']}")
    console.print(f"  CSV:                   {result['csv_path']}")
    console.print(f"  JSONL:                 {result['jsonl_path']}")
    console.print()


@app.command("export-outcomes")
def export_outcomes_command(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination folder. Defaults to a timestamped application_exports folder."),
) -> None:
    """Export per-job application outcomes (keyed by url) for the recommendation engine.

    This is the learning loop's return leg: feed outcomes.jsonl back into brainstorm
    so it can correlate its scores with which jobs actually reached interview/offer.
    """
    _bootstrap()

    from applypilot.applications import export_outcomes

    result = export_outcomes(output_dir=output)
    console.print("\n[bold green]Outcomes export complete[/bold green]")
    console.print(f"  Outcomes exported: {result['outcomes_exported']}")
    for stage, n in result["by_stage"].items():
        console.print(f"    {stage:<16} {n}")
    console.print(f"  JSONL: {result['jsonl_path']}")
    console.print(
        "\n[dim]In brainstorm: npm run applypilot:outcomes -- "
        f"--outcomes={result['jsonl_path']}[/dim]\n"
    )


@app.command("scan-gmail")
def scan_gmail_command(
    days: int = typer.Option(30, "--days", "-d", help="How many days back to search."),
    min_confidence: str = typer.Option(
        "medium", "--min-confidence",
        help="Minimum confidence to display/apply: low | medium | high",
    ),
    credentials: Optional[Path] = typer.Option(
        None, "--credentials",
        help="Path to gmail_credentials.json. Defaults to ~/.applypilot/gmail_credentials.json",
    ),
    apply: bool = typer.Option(
        False, "--apply",
        help="Write detected outcomes to the tracker. Default is dry-run (show only).",
    ),
) -> None:
    """[Standalone] Scan Gmail for application outcomes (interview / offer / rejection).

    NOT part of the main pipeline — run manually when you want to sync email outcomes.

    One-time setup (required):
      pip install google-auth-oauthlib google-api-python-client
      # Download OAuth credentials from Google Cloud Console (Desktop app)
      # Save as ~/.applypilot/gmail_credentials.json
      # First run opens a browser for read-only consent

    Dry-run by default — add --apply to write outcomes to the tracker.
    """
    _bootstrap()

    try:
        from applypilot.gmail_outcomes import scan_inbox, apply_outcomes
    except ImportError as exc:
        console.print(f"[red]Import error:[/red] {exc}")
        raise typer.Exit(1)

    dry_run = not apply
    tag = " [dim](dry-run — add --apply to write)[/dim]" if dry_run else " [bold green](writing to tracker)[/bold green]"
    console.print(f"\n[bold]Gmail outcome scan[/bold]{tag}")
    console.print(f"  Scanning last [bold]{days}[/bold] day(s)  •  min confidence: {min_confidence}\n")

    try:
        outcomes = scan_inbox(
            days=days,
            credentials_path=credentials,
            min_confidence=min_confidence,
        )
    except FileNotFoundError as exc:
        console.print(f"[red]Setup required:[/red]\n{exc}")
        raise typer.Exit(1)
    except ImportError as exc:
        console.print(f"[red]Missing dependencies:[/red] {exc}")
        raise typer.Exit(1)

    if not outcomes:
        console.print("[dim]No application-related emails found in the search window.[/dim]\n")
        return

    _COLORS = {"offer": "green", "interview": "cyan", "rejected": "red"}

    table = Table(
        title=f"Detected outcomes ({len(outcomes)})",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Outcome", style="bold")
    table.add_column("Conf.")
    table.add_column("Matched job", max_width=35)
    table.add_column("Method")
    table.add_column("Subject", max_width=50)

    for o in outcomes:
        color = _COLORS.get(o.outcome, "white")
        matched = o.matched_job_title or "[dim]unmatched[/dim]"
        table.add_row(
            f"[{color}]{o.outcome}[/{color}]",
            o.confidence,
            matched,
            o.match_method or "-",
            o.subject,
        )

    console.print(table)

    unmatched = sum(1 for o in outcomes if not o.matched_job_url)
    if unmatched:
        console.print(
            f"\n[yellow]{unmatched} email(s) could not be matched to an applied job.[/yellow] "
            "[dim](company may be tracked under a different name, or the job was never imported)[/dim]"
        )

    counts = apply_outcomes(outcomes, dry_run=dry_run)

    verb = "Would write" if dry_run else "Wrote"
    console.print(f"\n{verb}: [bold]{counts['written']}[/bold] outcome(s)")
    if counts["skipped_no_match"]:
        console.print(f"  Skipped (no job match):  {counts['skipped_no_match']}")
    if counts["skipped_ambiguous"]:
        console.print(f"  Skipped (ambiguous):     {counts['skipped_ambiguous']}")
    if counts.get("errors"):
        console.print(f"  [red]Errors:[/red]              {counts['errors']}")
    if dry_run:
        console.print("\n[dim]Re-run with --apply to write these outcomes to the tracker.[/dim]")
    console.print()


@app.command()
def dashboard() -> None:
    """Generate and open the HTML dashboard in your browser."""
    _bootstrap()

    from applypilot.view import open_dashboard

    open_dashboard()


@app.command("export-jobs")
def export_jobs_command(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination folder. Defaults to a timestamped job_exports folder."),
    min_score: Optional[int] = typer.Option(None, "--min-score", help="Only export jobs at or above this score. Omit for all jobs."),
    scored_only: bool = typer.Option(False, "--scored-only", help="Only export jobs that already have a fit score."),
    full_description_only: bool = typer.Option(False, "--full-description-only", help="Only export jobs with a full saved description."),
    limit: int = typer.Option(0, "--limit", "-l", help="Maximum jobs to export. 0 means all matching jobs."),
    descriptions: bool = typer.Option(True, "--descriptions/--no-descriptions", help="Write one readable text file per job."),
) -> None:
    """Export saved jobs, scores, and descriptions for review."""
    _bootstrap()

    from applypilot.export_jobs import export_jobs

    result = export_jobs(
        output_dir=output,
        min_score=min_score,
        scored_only=scored_only,
        full_description_only=full_description_only,
        limit=limit,
        write_descriptions=descriptions,
    )

    console.print("\n[bold green]Export complete[/bold green]")
    console.print(f"  Jobs exported:      {result['jobs_exported']}")
    console.print(f"  Description files:  {result['description_files']}")
    console.print(f"  Folder:             {result['output_dir']}")
    console.print(f"  CSV:                {result['csv_path']}")
    console.print(f"  JSONL:              {result['jsonl_path']}")
    console.print(f"  Summary:            {result['summary_path']}")
    console.print()


@app.command("import-decisions")
def import_decisions_command(
    path: Path = typer.Argument(..., help="Apply-decision export from the recommendation engine (.jsonl or .json)."),
    scale: str = typer.Option("auto", "--scale", help="Score scale of the input: auto, ten (1-10), unit (0-1), or percent (0-100)."),
    source: str = typer.Option("brainstorm", "--source", help="Tag written to decision_source when a record omits one."),
) -> None:
    """Import a curated apply list from the recommendation engine.

    Approved jobs become authoritative for the apply gate (their verdict is
    written into audit_score and they skip ApplyPilot's own audit). The LLM
    fit_score is kept as a benchmark alongside external_decision_score.
    """
    _bootstrap()

    from applypilot.import_decisions import import_decisions

    if scale not in {"auto", "ten", "unit", "percent"}:
        console.print(f"[red]Invalid --scale '{scale}'.[/red] Use one of: auto, ten, unit, percent.")
        raise typer.Exit(1)

    r = import_decisions(path, scale=scale, default_source=source)

    console.print("\n[bold green]Decisions imported[/bold green]")
    console.print(f"  Records read:          {r['records']}")
    console.print(f"  Approved:              {r['approved']}")
    console.print(f"  Updated (existing):    {r['updated']}")
    console.print(f"  Inserted (new):        {r['inserted']}")
    if r["skipped_not_approved"]:
        console.print(f"  [dim]Skipped (not approved): {r['skipped_not_approved']}[/dim]")
    if r["skipped_already_applied"]:
        console.print(f"  [dim]Skipped (already applied): {r['skipped_already_applied']}[/dim]")
    if r["skipped_duplicate"]:
        console.print(f"  [yellow]Skipped (duplicate of another job): {r['skipped_duplicate']}[/yellow]")
    if r["not_found_insufficient_metadata"]:
        console.print(f"  [yellow]Approved but not in DB and no title to insert: {r['not_found_insufficient_metadata']}[/yellow]")
    if r["skipped_no_url"] or r["skipped_no_score"]:
        console.print(f"  [yellow]Skipped (missing url/score): {r['skipped_no_url'] + r['skipped_no_score']}[/yellow]")
    if r["below_apply_threshold"]:
        console.print(
            f"  [yellow]Approved but scored below apply threshold {r['apply_threshold']}: "
            f"{r['below_apply_threshold']}[/yellow] (won't apply unless you lower --min-score)"
        )
    if r["manual_ats"]:
        console.print(
            f"  [yellow]Approved but point at a manual ATS: {r['manual_ats']}[/yellow] "
            "(the apply worker will skip these)"
        )
    enrich_hint = " enrich" if r.get("inserted") else ""
    console.print(
        f"\n[dim]Run 'applypilot run{enrich_hint} tailor cover pdf' then 'applypilot apply' "
        "to apply to the approved jobs"
        + (" (newly-inserted jobs need enrich first to fetch their description).[/dim]\n"
           if r.get("inserted") else ".[/dim]\n")
    )


@app.command("reenrich")
def reenrich_command(
    min_chars: int = typer.Option(200, "--min-chars", help="Re-scrape jobs whose stored description is shorter than this."),
    limit: int = typer.Option(0, "--limit", "-l", help="Max jobs to re-enrich. 0 = all eligible."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel site-batch workers."),
) -> None:
    """Re-fetch descriptions for jobs with a missing or too-thin description.

    Normal enrichment marks a job done even when it only captured a title/stub,
    so the job is never retried and gets dropped (or pollutes the recommendation
    engine's fit map) for lack of a real description. This gives those jobs a
    fresh scrape so good ones aren't lost.
    """
    _bootstrap()

    from applypilot.enrichment.detail import reenrich_thin_descriptions

    r = reenrich_thin_descriptions(min_chars=min_chars, limit=limit, workers=workers)

    console.print("\n[bold green]Re-enrichment complete[/bold green]")
    console.print(f"  Eligible (thin/missing):     {r['eligible']}")
    console.print(f"  Re-enriched this run:        {r['reenriched']}")
    console.print(f"  Now have a real description: {r['improved']}")
    if r["still_thin"]:
        console.print(
            f"  [yellow]Still thin after retry: {r['still_thin']}[/yellow] "
            "(source likely only provides a stub; will stop retrying after a few attempts)"
        )
    console.print()


@app.command("usage")
def usage_command() -> None:
    """Report LLM token usage and estimated cost per stage and model."""
    _bootstrap()

    from applypilot.database import get_llm_usage_summary, init_db
    from rich.table import Table

    init_db()
    s = get_llm_usage_summary()
    if not s["total_calls"]:
        console.print("\n[dim]No LLM usage recorded yet. Run scoring/tailoring/etc. with this build to start tracking.[/dim]\n")
        return

    table = Table(title="LLM usage by stage", show_header=True, header_style="bold")
    table.add_column("Stage")
    for col in ("Calls", "Prompt", "Completion", "Total tokens", "Est. cost"):
        table.add_column(col, justify="right")
    for r in s["by_stage"]:
        cost = f"${r['cost']:.4f}" if r.get("cost") else "-"
        table.add_row(r["stage"], f"{r['calls']:,}", f"{(r['prompt'] or 0):,}",
                      f"{(r['completion'] or 0):,}", f"{(r['total'] or 0):,}", cost)
    console.print()
    console.print(table)
    console.print(
        f"\n[bold]Totals:[/bold] {s['total_calls']:,} calls, {s['total_tokens']:,} tokens, "
        f"est. ${s['total_cost_usd']:.4f}"
    )
    console.print("[dim](Cost is a rough estimate from public list prices; free-tier usage is $0. Token counts are exact.)[/dim]")

    if s.get("by_variant"):
        vtable = Table(title="Prompt variant tracking (MAB)", show_header=True, header_style="bold cyan")
        vtable.add_column("Variant")
        vtable.add_column("Stage")
        for col in ("Calls", "Total tokens", "Est. cost"):
            vtable.add_column(col, justify="right")
        for r in s["by_variant"]:
            cost = f"${r['cost']:.4f}" if r.get("cost") else "-"
            vtable.add_row(r["variant"], r["stage"], f"{r['calls']:,}",
                           f"{(r['total'] or 0):,}", cost)
        console.print()
        console.print(vtable)
    console.print()


@app.command("analytics")
def analytics_command() -> None:
    """Apply funnel, success rate by site, failure reasons, and outcome tracker."""
    _bootstrap()

    from applypilot.database import get_apply_analytics, get_scrape_quality_report, init_db
    from rich.table import Table

    init_db()
    a = get_apply_analytics()

    console.print("\n[bold]Apply funnel[/bold]")
    for status, n in sorted(a["funnel"].items(), key=lambda kv: -kv[1]):
        console.print(f"  {status:<16} {n:,}")

    if a["success_rate"] is not None:
        console.print(
            f"\n[bold]Success rate:[/bold] {a['applied']:,}/{a['attempted']:,} = "
            f"{a['success_rate'] * 100:.1f}%"
        )
    if a["avg_apply_seconds"]:
        console.print(f"[bold]Avg apply time:[/bold] {a['avg_apply_seconds']:.0f}s")

    if a["by_site"]:
        console.print("\n[bold]By site (applied / failed)[/bold]")
        for r in a["by_site"]:
            console.print(f"  {(r['site'] or '?')[:30]:<30} {r['applied'] or 0:>4} / {r['failed'] or 0}")
    if a["fail_reasons"]:
        console.print("\n[bold]Top failure reasons[/bold]")
        for r in a["fail_reasons"]:
            console.print(f"  {r['count']:>4}  {(r['reason'] or '?')[:55]}")
    if a["outcomes"]:
        console.print("\n[bold]Outcome tracker[/bold]")
        for status, n in sorted(a["outcomes"].items(), key=lambda kv: -kv[1]):
            console.print(f"  {status:<16} {n:,}")

    try:
        sqr = get_scrape_quality_report()
        boards_with_issues = [b for b in sqr["boards"] if b["null_rate"] > 0]
        if boards_with_issues:
            qtable = Table(title="Scrape quality by board (null rate)", show_header=True, header_style="bold yellow")
            qtable.add_column("Board")
            qtable.add_column("Total", justify="right")
            qtable.add_column("Null rate", justify="right")
            qtable.add_column("Missing title", justify="right")
            qtable.add_column("Missing desc", justify="right")
            qtable.add_column("Missing loc", justify="right")
            qtable.add_column("Missing co", justify="right")
            for b in boards_with_issues[:20]:
                rate_str = f"{b['null_rate'] * 100:.1f}%"
                style = "red" if b["null_rate"] >= 0.20 else "yellow" if b["null_rate"] >= 0.05 else ""
                nc = b["null_counts"]
                qtable.add_row(
                    b["board"], f"{b['total']:,}",
                    f"[{style}]{rate_str}[/{style}]" if style else rate_str,
                    str(nc.get("title", 0)), str(nc.get("full_description", 0)),
                    str(nc.get("location", 0)), str(nc.get("company", 0)),
                )
            console.print()
            console.print(qtable)
            console.print("[dim](Null rate >= 20% may indicate selector drift or anti-bot substitution.)[/dim]")
    except Exception:
        pass
    console.print()


@app.command("audit-scores")
def audit_scores_command(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination folder. Defaults to a timestamped score_audits folder."),
    min_score: Optional[int] = typer.Option(None, "--min-score", help="Only audit jobs at or above this original LLM score."),
    limit: int = typer.Option(0, "--limit", "-l", help="Maximum jobs to audit. 0 means all scored jobs."),
    reaudit: bool = typer.Option(False, "--reaudit", help="Re-audit already current scores instead of only pending/stale audit rows."),
) -> None:
    """Audit scored jobs and rerank them by target-role fit."""
    _bootstrap()

    from applypilot.scoring.audit import run_score_audit

    result = run_score_audit(output_dir=output, min_score=min_score, limit=limit, reaudit=reaudit)

    console.print("\n[bold green]Score audit complete[/bold green]")
    console.print(f"  Jobs audited:          {result['audited']}")
    console.print(f"  Recommended/review:    {result['recommended_review']}")
    console.print(f"  False positives:       {result['false_positives']}")
    console.print(f"  Missed priority roles: {result['missed_priority_roles']}")
    console.print(f"  Folder:                {result['output_dir']}")
    console.print(f"  Review CSV:            {result['recommended_review_csv']}")
    console.print(f"  False positives CSV:   {result['false_positives_csv']}")
    console.print()


@app.command("diagnose-fits")
def diagnose_fits_command(
    limit: int = typer.Option(900, "--limit", "-l", help="Maximum jobs to diagnose. Use 0 for all pending jobs."),
    rediagnose: bool = typer.Option(False, "--rediagnose", help="Re-run diagnosis even when a job already has one."),
) -> None:
    """Explain why scored jobs are strong, weak, fixable, or wrong-lane."""
    _bootstrap()

    from applypilot.config import check_tier
    from applypilot.scoring.diagnosis import run_diagnostics

    check_tier(2, "fit diagnosis")
    result = run_diagnostics(limit=limit, rediagnose=rediagnose)

    console.print("\n[bold green]Fit diagnosis complete[/bold green]")
    console.print(f"  Jobs diagnosed: {result['diagnosed']}")
    console.print(f"  Errors:         {result['errors']}")
    if result.get("labels"):
        table = Table(title="Recommended Actions", show_header=True, header_style="bold cyan")
        table.add_column("Action")
        table.add_column("Count", justify="right")
        for action, count in sorted(result["labels"].items(), key=lambda item: item[0]):
            table.add_row(action, str(count))
        console.print(table)
    console.print()


@app.command("reset-generated")
def reset_generated_command(
    min_score: int = typer.Option(7, "--min-score", help="Minimum audited score / fit score to reset."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Show how many jobs would be reset without changing the DB."),
) -> None:
    """Reset generated resumes and cover letters so they can be regenerated."""
    _bootstrap()

    from applypilot.database import get_connection

    conn = get_connection()
    where = (
        "COALESCE(audit_score, fit_score, 0) >= ? "
        "AND duplicate_of_url IS NULL "
        "AND applied_at IS NULL "
        "AND (tailored_resume_path IS NOT NULL OR cover_letter_path IS NOT NULL)"
    )
    count = conn.execute(f"SELECT COUNT(*) FROM jobs WHERE {where}", (min_score,)).fetchone()[0]
    if dry_run:
        console.print(f"[yellow]Dry run:[/yellow] would reset {count} generated job package(s).")
        return

    conn.execute(
        f"""
        UPDATE jobs
        SET tailored_resume_path = NULL,
            tailored_at = NULL,
            tailor_attempts = 0,
            cover_letter_path = NULL,
            cover_letter_at = NULL,
            cover_attempts = 0
        WHERE {where}
        """,
        (min_score,),
    )
    conn.commit()
    console.print(f"[green]Reset {count} generated job package(s).[/green]")


@app.command()
def doctor() -> None:
    """Check your setup and diagnose missing requirements."""
    import shutil
    from applypilot.config import (
        load_env, PROFILE_PATH, RESUME_PATH, RESUME_PDF_PATH,
        SEARCH_CONFIG_PATH, get_chrome_path, get_claude_path, get_codex_path,
        load_preference_profile, PREFERENCE_PROFILE_PATH, KNOWLEDGE_GRAPH_PROMPT_PATH,
    )

    load_env()

    ok_mark = "[green]OK[/green]"
    fail_mark = "[red]MISSING[/red]"
    warn_mark = "[yellow]WARN[/yellow]"

    results: list[tuple[str, str, str]] = []  # (check, status, note)

    # --- Tier 1 checks ---
    # Profile
    if PROFILE_PATH.exists():
        results.append(("profile.json", ok_mark, str(PROFILE_PATH)))
    else:
        results.append(("profile.json", fail_mark, "Run 'applypilot init' to create"))

    # Resume
    if RESUME_PATH.exists():
        results.append(("resume.txt", ok_mark, str(RESUME_PATH)))
    elif RESUME_PDF_PATH.exists():
        results.append(("resume.txt", warn_mark, "Only PDF found — plain-text needed for AI stages"))
    else:
        results.append(("resume.txt", fail_mark, "Run 'applypilot init' to add your resume"))

    # Search config
    if SEARCH_CONFIG_PATH.exists():
        results.append(("searches.yaml", ok_mark, str(SEARCH_CONFIG_PATH)))
    else:
        results.append(("searches.yaml", warn_mark, "Will use example config — run 'applypilot init'"))

    # jobspy (discovery dep installed separately)
    try:
        import jobspy  # noqa: F401
        results.append(("python-jobspy", ok_mark, "Job board scraping available"))
    except ImportError:
        results.append(("python-jobspy", warn_mark,
                        "pip install --no-deps python-jobspy && pip install pydantic tls-client requests markdownify regex"))

    # --- Tier 2 checks ---
    import os
    llm_status, llm_note = _llm_status_from_env(os.environ)
    if llm_status == "ok":
        results.append(("LLM API key", ok_mark, llm_note))
    else:
        results.append(("LLM API key", fail_mark, f"{llm_note} (run 'applypilot init')"))

    # --- Tier 3 checks ---
    # Apply agent CLIs
    try:
        claude_bin = get_claude_path()
        results.append(("Claude Code CLI", ok_mark, claude_bin))
    except FileNotFoundError:
        results.append(("Claude Code CLI", fail_mark,
                        "Install from https://claude.ai/code or use --agent codex"))

    try:
        codex_bin = get_codex_path()
        results.append(("Codex CLI", ok_mark, codex_bin))
    except FileNotFoundError:
        results.append(("Codex CLI", warn_mark,
                        "Install Codex CLI or set CODEX_PATH to use --agent codex"))

    # Chrome
    try:
        chrome_path = get_chrome_path()
        results.append(("Chrome/Chromium", ok_mark, chrome_path))
    except FileNotFoundError:
        results.append(("Chrome/Chromium", fail_mark,
                        "Install Chrome or set CHROME_PATH env var (needed for auto-apply)"))

    # Node.js / npx (for Playwright MCP)
    npx_bin = shutil.which("npx")
    if npx_bin:
        results.append(("Node.js (npx)", ok_mark, npx_bin))
    else:
        results.append(("Node.js (npx)", fail_mark,
                        "Install Node.js 18+ from nodejs.org (needed for auto-apply)"))

    # CapSolver (optional)
    capsolver = os.environ.get("CAPSOLVER_API_KEY")
    if capsolver:
        results.append(("CapSolver API key", ok_mark, "CAPTCHA solving enabled"))
    else:
        results.append(("CapSolver API key", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))

    # --- Recommendation calibration (optional) ---
    # These files are produced by the external recommendation engine
    # ("brainstorm") and consumed by the scorer to calibrate fit scores.
    if PREFERENCE_PROFILE_PATH.exists():
        prof = load_preference_profile()  # robust: returns None if malformed
        if prof is None:
            results.append(("preference profile", warn_mark,
                            f"Present but unreadable/invalid JSON: {PREFERENCE_PROFILE_PATH}"))
        else:
            from applypilot.scoring.scorer import _PREFERENCE_FIELDS
            if any(prof.get(k) for k in _PREFERENCE_FIELDS):
                results.append(("preference profile", ok_mark, "Scoring calibration loaded"))
            else:
                results.append(("preference profile", warn_mark,
                                "Loaded but no known fields — check recommendation engine schema"))
    else:
        results.append(("preference profile", "[dim]optional[/dim]",
                        "Drop job_preference_profile.json to calibrate scoring"))

    if KNOWLEDGE_GRAPH_PROMPT_PATH.exists():
        results.append(("knowledge graph", ok_mark, "Scoring calibration loaded"))
    else:
        results.append(("knowledge graph", "[dim]optional[/dim]",
                        "Drop job_knowledge_graph_prompt.md to calibrate scoring"))

    # --- Render results ---
    console.print()
    console.print("[bold]ApplyPilot Doctor[/bold]\n")

    col_w = max(len(r[0]) for r in results) + 2
    for check, status, note in results:
        pad = " " * (col_w - len(check))
        console.print(f"  {check}{pad}{status}  [dim]{note}[/dim]")

    console.print()

    # Tier summary
    from applypilot.config import get_tier, TIER_LABELS
    tier = get_tier()
    console.print(f"[bold]Current tier: Tier {tier} — {TIER_LABELS[tier]}[/bold]")

    if tier == 1:
        console.print("[dim]  → Tier 2 unlocks: scoring, tailoring, cover letters (needs LLM API key)[/dim]")
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI or Codex CLI + Chrome + Node.js)[/dim]")
    elif tier == 2:
        console.print("[dim]  → Tier 3 unlocks: auto-apply (needs Claude Code CLI or Codex CLI + Chrome + Node.js)[/dim]")

    console.print()


if __name__ == "__main__":
    app()
