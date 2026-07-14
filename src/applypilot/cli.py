"""ApplyPilot CLI — the main entry point."""

from __future__ import annotations

import logging
import os
import sys
import json
from collections import defaultdict
from datetime import datetime as _dt, time as _time, timedelta as _td, timezone as _tz
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
outcomes_review_app = typer.Typer(
    name="outcomes-review",
    help="Review and correct outcome evidence before alerts/analytics consume it.",
)
app.add_typer(outcomes_review_app)
outcomes_alerts_app = typer.Typer(
    name="outcomes-alerts",
    help="Build critical/warning alerts and digest artifacts for outcome monitoring.",
)
app.add_typer(outcomes_alerts_app)
outcomes_learn_app = typer.Typer(
    name="outcomes-learn",
    help="Trusted-only learning exports and policy-review reports.",
)
app.add_typer(outcomes_learn_app)
console = Console()
log = logging.getLogger(__name__)
canonical_app = typer.Typer(help="Canonical decision policy lifecycle and reviewed feedback.")
app.add_typer(canonical_app, name="canonical")

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


def _require_local_apply_admission(*, target_url: str | None = None) -> None:
    from applypilot.fleet import emergency_admission

    admission = emergency_admission.local_apply_admission(target_url=target_url)
    if admission.allowed:
        return
    typer.echo(
        f"{emergency_admission.denial_marker(admission)} {admission.reason}",
        err=True,
    )
    raise typer.Exit(code=emergency_admission.DENIAL_EXIT_CODE)


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


def _canonical_connection():
    from applypilot.config import ensure_dirs, load_env
    from applypilot.database import init_db

    load_env()
    ensure_dirs()
    return init_db(os.environ.get("APPLYPILOT_DB_PATH"))


def _canonical_policy(conn, policy_version: str):
    row = conn.execute(
        "SELECT * FROM decision_policy_versions WHERE policy_version=?",
        (policy_version,),
    ).fetchone()
    if row is None:
        raise typer.BadParameter(f"unknown policy: {policy_version}")
    return row


@canonical_app.command("status")
def canonical_status(
    dsn: Optional[str] = typer.Option(None, "--dsn", envvar="FLEET_PG_DSN"),
) -> None:
    """Report policy state and fail-closed projection/queue counts by lane."""
    conn = _canonical_connection()
    result: dict[str, object] = {"lanes": {}}
    for lane in ("ats", "linkedin"):
        active = conn.execute(
            "SELECT policy_version,status,metrics_json FROM decision_policy_versions "
            "WHERE lane=? AND status IN ('canary','active') ORDER BY status='active' DESC, policy_version",
            (lane,),
        ).fetchall()
        missing = conn.execute(
            "SELECT COUNT(*) FROM jobs j WHERE "
            + ("COALESCE(j.application_url,j.url) NOT LIKE '%linkedin.com%'" if lane == "ats" else "COALESCE(j.application_url,j.url) LIKE '%linkedin.com%'")
            + " AND j.canonical_decision_id IS NULL"
        ).fetchone()[0]
        mismatch = conn.execute(
            "SELECT COUNT(*) FROM jobs j JOIN job_decisions d ON d.decision_id=j.canonical_decision_id "
            "WHERE d.lane=? AND (d.policy_version<>j.canonical_policy_version "
            "OR j.canonical_policy_version IS NULL)",
            (lane,),
        ).fetchone()[0]
        result["lanes"][lane] = {
            "policies": [dict(row) for row in active],
            "missing_projection": missing,
            "mismatched_projection": mismatch,
        }
    if dsn:
        from applypilot.apply import pgqueue
        try:
            with pgqueue.connect(dsn) as pg, pg.cursor() as cur:
                cur.execute("SELECT ats_policy_version,linkedin_policy_version FROM fleet_config WHERE id=1")
                cfg = dict(cur.fetchone())
                for lane, table in (("ats", "apply_queue"), ("linkedin", "linkedin_queue")):
                    cur.execute(
                        f"SELECT COUNT(*) AS total, COUNT(*) FILTER (WHERE decision_id IS NULL) AS missing, "
                        f"COUNT(*) FILTER (WHERE policy_version<>%s) AS mismatched FROM {table} WHERE status='queued'",
                        (cfg[f"{lane}_policy_version"],),
                    )
                    result["lanes"][lane]["queue"] = dict(cur.fetchone())
                    result["lanes"][lane]["fleet_policy_version"] = cfg[f"{lane}_policy_version"]
        except Exception as exc:
            result["fleet_error"] = str(exc)
    console.print_json(data=result)


@canonical_app.command("validate")
def canonical_validate(policy_version: str = typer.Argument(...)) -> None:
    """Validate a draft only when every locked replay gate passes."""
    from applypilot import canonical_decisions

    conn = _canonical_connection()
    canonical_decisions.validate_policy(conn, policy_version)
    console.print(f"validated {policy_version}")


def _stage_pg_policy(pg, *, policy_version: str, lane: str) -> None:
    table = "apply_queue" if lane == "ats" else "linkedin_queue"
    with pg.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version,lane,status) VALUES (%s,%s,'validated') "
            "ON CONFLICT (policy_version) DO UPDATE SET status='validated' "
            "WHERE fleet_decision_policies.lane=EXCLUDED.lane",
            (policy_version, lane),
        )
        cur.execute(
            f"UPDATE fleet_config SET {lane}_policy_version=%s, updated_at=now() WHERE id=1",
            (policy_version,),
        )
        cur.execute(
            f"UPDATE {table} SET status='failed', apply_status='skipped', "
            "apply_error='canonical_policy_replaced', updated_at=now() "
            "WHERE status='queued' AND policy_version IS DISTINCT FROM %s",
            (policy_version,),
        )
    pg.commit()


@canonical_app.command("promote")
def canonical_promote(
    policy_version: str = typer.Argument(...),
    lane: str = typer.Option(..., "--lane", help="Explicitly select ats or linkedin."),
    dsn: str = typer.Option(..., "--dsn", envvar="FLEET_PG_DSN"),
) -> None:
    """Promote replay-validated policy; never clears an operator lane pause."""
    from applypilot import canonical_decisions
    from applypilot.apply import pgqueue

    if lane not in {"ats", "linkedin"}:
        raise typer.BadParameter("--lane must be ats or linkedin")
    conn = _canonical_connection()
    policy = _canonical_policy(conn, policy_version)
    if policy["lane"] != lane:
        raise typer.BadParameter(f"policy belongs to {policy['lane']}, not {lane}")
    recovering_active = policy["status"] == "active"
    if policy["status"] == "draft":
        canonical_decisions.validate_policy(conn, policy_version)
    elif policy["status"] not in {"validated", "active"}:
        raise typer.BadParameter("policy must be draft, validated, or an active recovery")

    try:
        with pgqueue.connect(dsn) as pg:
            _stage_pg_policy(pg, policy_version=policy_version, lane=lane)
    except Exception as exc:
        console.print(f"[red]Postgres staging failed; {policy_version} remains validated: {exc}[/red]")
        raise typer.Exit(2) from exc

    if not recovering_active:
        canonical_decisions.activate_policy(conn, policy_version, lane=lane)
    try:
        with pgqueue.connect(dsn) as pg, pg.cursor() as cur:
            cur.execute(
                "UPDATE fleet_decision_policies SET status='retired',retired_at=now() "
                "WHERE lane=%s AND status='active' AND policy_version<>%s",
                (lane, policy_version),
            )
            cur.execute(
                "UPDATE fleet_decision_policies SET status='active',activated_at=now(),retired_at=NULL "
                "WHERE policy_version=%s AND lane=%s",
                (policy_version, lane),
            )
            pg.commit()
    except Exception as exc:
        console.print(
            f"[red]SQLite activated but Postgres remained non-active and fail-closed: {exc}[/red]"
        )
        raise typer.Exit(3) from exc
    console.print(f"promoted {policy_version} for {lane}; operator lane pause unchanged")


@canonical_app.command("retire")
def canonical_retire(
    policy_version: str = typer.Argument(...),
    dsn: str = typer.Option(..., "--dsn", envvar="FLEET_PG_DSN"),
) -> None:
    """Retire one policy, invalidate its queued work, and pause only its lane."""
    from applypilot import canonical_decisions
    from applypilot.apply import pgqueue

    conn = _canonical_connection()
    policy = _canonical_policy(conn, policy_version)
    lane = policy["lane"]
    table = "apply_queue" if lane == "ats" else "linkedin_queue"
    try:
        with pgqueue.connect(dsn) as pg, pg.cursor() as cur:
            cur.execute(
                "UPDATE fleet_decision_policies SET status='retired',retired_at=COALESCE(retired_at,now()) "
                "WHERE policy_version=%s AND lane=%s",
                (policy_version, lane),
            )
            if lane == "ats":
                cur.execute(
                    "UPDATE fleet_config SET ats_policy_version=NULL,ats_paused=TRUE,"
                    "ats_pause_source='canonical_policy_retired',updated_at=now() "
                    "WHERE id=1 AND ats_policy_version=%s",
                    (policy_version,),
                )
            else:
                cur.execute(
                    "UPDATE fleet_config SET linkedin_policy_version=NULL,linkedin_canary_enabled=TRUE,"
                    "linkedin_canary_remaining=0,updated_at=now() "
                    "WHERE id=1 AND linkedin_policy_version=%s",
                    (policy_version,),
                )
            cur.execute(
                f"UPDATE {table} SET status='failed',apply_status='skipped',"
                "apply_error='canonical_policy_retired',updated_at=now() "
                "WHERE status='queued' AND policy_version=%s",
                (policy_version,),
            )
            pg.commit()
    except Exception as exc:
        console.print(f"[red]Postgres retirement failed; SQLite policy unchanged: {exc}[/red]")
        raise typer.Exit(2) from exc
    canonical_decisions.retire_policy(conn, policy_version)
    console.print(f"retired {policy_version}; {lane} paused and queued rows invalidated")


@canonical_app.command("backfill")
def canonical_backfill(path: Path = typer.Argument(..., exists=True, file_okay=False)) -> None:
    from applypilot.canonical_backfill import backfill_research_artifacts

    report = backfill_research_artifacts(_canonical_connection(), path)
    console.print_json(data=report)


@canonical_app.command("outcome-review")
def canonical_outcome_review(
    message_id: str = typer.Argument(...),
    resolution: str = typer.Option(..., "--resolution"),
    job_url: Optional[str] = typer.Option(None, "--job-url"),
    stage: Optional[str] = typer.Option(None, "--stage"),
    note: Optional[str] = typer.Option(None, "--note"),
) -> None:
    from applypilot.outcome_review import record_review

    row = record_review(
        _canonical_connection(), message_id, resolution=resolution,
        corrected_job_url=job_url, corrected_stage=stage, note=note,
    )
    console.print_json(data=row)


@canonical_app.command("outcome-review-queue")
def canonical_outcome_review_queue(
    limit: int = typer.Option(100, "--limit", min=1),
) -> None:
    """Show unreviewed email evidence; no row is accepted automatically."""
    from applypilot.outcome_review import list_canonical_review_queue

    rows = list_canonical_review_queue(_canonical_connection())
    console.print_json(data={"total": len(rows), "items": rows[:limit]})

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
    generation_workers: int = typer.Option(
        1,
        "--generation-workers",
        help="Parallel LLM workers for tailor/cover stages. Keep modest to avoid rate limits.",
    ),
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
    if generation_workers < 1:
        console.print("[red]Invalid --generation-workers:[/red] use 1 or a positive number.")
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
        generation_workers=generation_workers,
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


@app.command("score-jobs")
def score_jobs(
    limit: int = typer.Option(
        400,
        "--limit",
        "-l",
        help="Maximum unscored jobs to score with the current LLM and preference profile. Use 0 for all.",
    ),
    workers: int = typer.Option(
        1,
        "--workers",
        "-w",
        help="Parallel scoring workers. Keep at 1 on the home box.",
    ),
) -> None:
    """Score unscored jobs with an explicit per-run cap."""
    _bootstrap()

    from applypilot.config import check_tier
    from applypilot.scoring.scorer import run_scoring

    check_tier(2, "AI scoring")
    if limit < 0:
        console.print("[red]Invalid --limit:[/red] use 0 or a positive number.")
        raise typer.Exit(code=1)
    if workers < 1:
        console.print("[red]Invalid --workers:[/red] use 1 or a positive number.")
        raise typer.Exit(code=1)

    result = run_scoring(limit=limit, rescore=False, workers=workers)
    console.print("\n[bold green]Scoring complete[/bold green]")
    console.print(f"  Jobs scored: {result['scored']}")
    console.print(f"  Errors:      {result['errors']}")
    console.print(f"  Time:        {result['elapsed']:.1f}s")


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
    score_floor: float = typer.Option(
        -1.0, "--score-floor",
        help=("Minimum effective score floor for score-based auto-include candidates. "
              "-1 = APPLYPILOT_MIN_SCORE, 0 = disable score floor and use tiers only."),
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
    if score_floor == 0:
        score_floor = None
    elif score_floor == -1:
        from applypilot import config
        score_floor = float(config.get_min_score())
    elif score_floor < 0:
        console.print("[red]Invalid --score-floor:[/red] use -1, 0, or a positive number.")
        raise typer.Exit(code=1)

    tier_list = [t.strip() for t in tiers.split(",") if t.strip()]
    conn = get_connection()

    def _progress(done: int, total: int, _results: list) -> None:
        console.print(f"  probed {done}/{total}…", end="\r")

    result = liveness.verify_jobs(
        conn, tiers=tier_list, score_floor=score_floor, max_age_days=max_age_days,
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
            "[dim]No offsite URLs resolved yet. Run "
            "`applypilot linkedin-resolve-apply-urls --dry-run --limit 20` first, "
            "then a small live resolver pass if the candidate list looks right.[/dim]"
        )
    conn.close()


@app.command("linkedin-resolve-apply-urls")
def linkedin_resolve_apply_urls_command(
    limit: int = typer.Option(200, "--limit", help="Maximum unresolved LinkedIn jobs to inspect."),
    delay_min: float = typer.Option(8.0, "--delay-min", help="Minimum delay between LinkedIn job pages."),
    delay_max: float = typer.Option(20.0, "--delay-max", help="Maximum delay between LinkedIn job pages."),
    tiers: str = typer.Option("priority,recommended", "--tiers", help="Comma-separated audit labels to include."),
    include_low: bool = typer.Option(False, "--include-low", help="Also include review and low audit labels."),
    refresh: bool = typer.Option(False, "--refresh", help="Revisit rows with previous resolver statuses."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List candidates without opening LinkedIn."),
    browser: str = typer.Option("chrome", "--browser", help="Browser profile source: chrome, edge, cft, chromium, or default."),
    worker_id: int = typer.Option(80, "--worker-id", help="Resolver browser worker id; keep separate from apply workers."),
    chunk_size: int = typer.Option(10, "--chunk-size", help="Restart browser after this many LinkedIn pages. 0 disables chunking."),
) -> None:
    """Resolve external ATS apply URLs from LinkedIn job pages without applying."""
    _bootstrap()
    from applypilot import linkedin_resolver

    parsed_tiers = tuple(t.strip() for t in tiers.split(",") if t.strip())
    if not parsed_tiers:
        console.print("[red]--tiers must include at least one audit label.[/red]")
        raise typer.Exit(code=1)
    if delay_max < delay_min:
        console.print("[red]--delay-max must be greater than or equal to --delay-min.[/red]")
        raise typer.Exit(code=1)
    browser_name = browser.strip().lower()
    if browser_name not in {"chrome", "edge", "cft", "chromium", "default"}:
        console.print("[red]--browser must be one of chrome, edge, cft, chromium, or default.[/red]")
        raise typer.Exit(code=1)
    if chunk_size < 0:
        console.print("[red]--chunk-size must be 0 or a positive number.[/red]")
        raise typer.Exit(code=1)

    summary = linkedin_resolver.run_resolver(
        linkedin_resolver.ResolverOptions(
            limit=limit,
            tiers=parsed_tiers,
            include_low=include_low,
            refresh=refresh,
            dry_run=dry_run,
            delay_min=delay_min,
            delay_max=delay_max,
            browser=browser_name,
            worker_id=worker_id,
            chunk_size=chunk_size,
        )
    )

    console.print("\n[bold]LinkedIn external apply URL resolver[/bold]")
    console.print(f"  considered: {summary.considered}")
    if summary.dry_run:
        console.print("  mode:       dry run")
        for url in summary.sample_urls or []:
            console.print(f"  - {url}")
        return
    for status, count in sorted((summary.counts or {}).items()):
        console.print(f"  {status}: {count}")
    if summary.stopped_reason:
        console.print(f"  [yellow]stopped:[/yellow] {summary.stopped_reason}")
    console.print("[dim]Next: run `applypilot linkedin-split` to inspect the offsite/Easy Apply split.[/dim]")


@app.command("indeed-resolve-apply-urls")
def indeed_resolve_apply_urls_command(
    limit: int = typer.Option(200, "--limit", help="Maximum unresolved Indeed jobs to inspect."),
    tiers: str = typer.Option("priority,recommended", "--tiers", help="Comma-separated audit labels to include."),
    include_low: bool = typer.Option(False, "--include-low", help="Also include review and low audit labels."),
    refresh: bool = typer.Option(False, "--refresh", help="Revisit rows already processed by Indeed resolution."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview candidates without writing application_url metadata."),
) -> None:
    """Classify Indeed apply destinations without LLM/OCR or browser automation."""
    _bootstrap()
    from applypilot import indeed_resolver

    parsed_tiers = tuple(t.strip() for t in tiers.split(",") if t.strip())
    if not parsed_tiers:
        console.print("[red]--tiers must include at least one audit label.[/red]")
        raise typer.Exit(code=1)
    if limit < 0:
        console.print("[red]--limit must be 0 or a positive number.[/red]")
        raise typer.Exit(code=1)

    summary = indeed_resolver.run_resolver(
        indeed_resolver.IndeedResolverOptions(
            limit=limit,
            tiers=parsed_tiers,
            include_low=include_low,
            refresh=refresh,
            dry_run=dry_run,
        )
    )

    console.print("\n[bold]Indeed apply URL resolver[/bold]")
    console.print(f"  considered: {summary.considered}")
    if summary.dry_run:
        console.print("  mode:       dry run")
    for status, count in sorted((summary.counts or {}).items()):
        console.print(f"  {status}: {count}")
    for kind, count in sorted((summary.unresolved_kinds or {}).items()):
        console.print(f"  unresolved.{kind}: {count}")
    for url in summary.sample_urls or []:
        console.print(f"  - {url}")
    console.print("[dim]Browser-click Indeed resolution is not enabled; unresolved rows include next-action metadata.[/dim]")


@app.command("resolve-company-apply-urls")
def resolve_company_apply_urls_command(
    limit: int = typer.Option(200, "--limit", help="Maximum unresolved LinkedIn jobs to inspect."),
    tiers: str = typer.Option("priority,recommended", "--tiers", help="Comma-separated audit labels to include."),
    include_low: bool = typer.Option(False, "--include-low", help="Also include review and low audit labels."),
    refresh: bool = typer.Option(False, "--refresh", help="Revisit rows already processed by company matching."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview matches without writing application_url."),
    min_confidence: float = typer.Option(0.86, "--min-confidence", help="Minimum company/title/location confidence to accept."),
) -> None:
    """Resolve LinkedIn jobs by matching them to existing company/ATS rows in the DB."""
    _bootstrap()
    from applypilot import company_resolver

    parsed_tiers = tuple(t.strip() for t in tiers.split(",") if t.strip())
    if not parsed_tiers:
        console.print("[red]--tiers must include at least one audit label.[/red]")
        raise typer.Exit(code=1)
    if limit < 0:
        console.print("[red]--limit must be 0 or a positive number.[/red]")
        raise typer.Exit(code=1)
    if min_confidence <= 0 or min_confidence > 1:
        console.print("[red]--min-confidence must be between 0 and 1.[/red]")
        raise typer.Exit(code=1)

    summary = company_resolver.run_resolver(
        company_resolver.CompanyResolverOptions(
            limit=limit,
            tiers=parsed_tiers,
            include_low=include_low,
            refresh=refresh,
            dry_run=dry_run,
            min_confidence=min_confidence,
        )
    )

    console.print("\n[bold]Company apply URL resolver[/bold]")
    console.print(f"  considered: {summary.considered}")
    if summary.dry_run:
        console.print("  mode:       dry run")
    for status, count in sorted((summary.counts or {}).items()):
        console.print(f"  {status}: {count}")
    for url in summary.sample_urls or []:
        console.print(f"  - {url}")
    console.print("[dim]Next: run `applypilot linkedin-split`; use LinkedIn browser resolver only for leftovers.[/dim]")


@app.command("boost-output")
def boost_output_command(
    target_ready: int = typer.Option(300, "--target-ready", help="Build ready-to-apply queue to at least this size."),
    company_limit: int = typer.Option(2000, "--company-limit", help="LinkedIn rows to try resolving via company/ATS matches."),
    indeed_limit: int = typer.Option(2000, "--indeed-limit", help="Indeed rows to classify with the deterministic resolver. 0 disables."),
    verify_limit: int = typer.Option(500, "--verify-limit", help="Ready/high-priority jobs to liveness-check before generation. 0 disables."),
    verify_workers: int = typer.Option(16, "--verify-workers", help="Concurrent liveness probes."),
    batch_size: int = typer.Option(500, "--batch-size", help="Tailor/cover/pdf batch size per pass."),
    generation_workers: int = typer.Option(
        4,
        "--generation-workers",
        help="Parallel LLM workers for tailor/cover during queue generation.",
    ),
    max_passes: int = typer.Option(5, "--max-passes", help="Maximum generation passes before stopping."),
    min_score: Optional[int] = typer.Option(None, "--min-score", help="Minimum score for generation/apply. Defaults to config."),
    validation: str = typer.Option("lenient", "--validation", help="Generation validation mode: strict, normal, or lenient."),
    start_apply: bool = typer.Option(False, "--start-apply", help="After queue build, start supervised apply."),
    apply_workers: int = typer.Option(2, "--apply-workers", help="Workers for supervised apply when --start-apply is set."),
    agents: str = typer.Option("claude,codex", "--agents", help="Reserved for the direct apply command; supervisor currently uses configured apply agent."),
    model: str = typer.Option("sonnet", "--model", help="Apply-agent model when --start-apply is set."),
    max_cost_usd: float = typer.Option(90.0, "--max-cost-usd", help="Apply budget when --start-apply is set."),
    linkedin_daily_cap: int = typer.Option(0, "--linkedin-daily-cap", help="LinkedIn cap for supervised apply. 0 = no cap."),
    max_job_age_days: int = typer.Option(45, "--max-job-age-days", help="Skip older jobs during supervised apply."),
) -> None:
    """Increase application throughput by filling the ready queue before apply."""
    _bootstrap()

    from applypilot import company_resolver, config, indeed_resolver
    from applypilot.apply import liveness
    from applypilot.database import get_connection, get_stats
    from applypilot.pipeline import run_pipeline

    if min_score is None:
        min_score = config.get_min_score()
    if target_ready < 0:
        console.print("[red]--target-ready must be 0 or a positive number.[/red]")
        raise typer.Exit(code=1)
    if company_limit < 0 or indeed_limit < 0 or verify_limit < 0 or batch_size < 0 or max_passes < 0:
        console.print("[red]Limits, batch size, and max passes must be 0 or positive numbers.[/red]")
        raise typer.Exit(code=1)
    if validation not in {"strict", "normal", "lenient"}:
        console.print("[red]--validation must be strict, normal, or lenient.[/red]")
        raise typer.Exit(code=1)
    if apply_workers < 1:
        console.print("[red]--apply-workers must be at least 1.[/red]")
        raise typer.Exit(code=1)
    if generation_workers < 1:
        console.print("[red]--generation-workers must be at least 1.[/red]")
        raise typer.Exit(code=1)

    console.print("\n[bold]ApplyPilot output boost[/bold]")
    before = get_stats()
    console.print(f"  Ready before:        {before.get('ready_to_apply', 0)}")
    console.print(f"  Target ready:        {target_ready}")
    console.print(f"  Generation workers:  {generation_workers}")

    if company_limit:
        res = company_resolver.run_resolver(
            company_resolver.CompanyResolverOptions(
                limit=company_limit,
                tiers=("priority", "recommended"),
                min_confidence=0.86,
            )
        )
        resolved = (res.counts or {}).get("resolved_company_match", 0)
        console.print(f"  Company URL matches: {resolved} / {res.considered}")

    if indeed_limit:
        res = indeed_resolver.run_resolver(
            indeed_resolver.IndeedResolverOptions(
                limit=indeed_limit,
                tiers=("priority", "recommended"),
            )
        )
        resolved = (res.counts or {}).get("resolved_offsite", 0)
        hosted = (res.counts or {}).get("hosted_apply", 0)
        unresolved = (res.counts or {}).get("unresolved", 0)
        console.print(
            "  Indeed URL pass:     "
            f"offsite={resolved}, hosted={hosted}, unresolved={unresolved} / {res.considered}"
        )

    if verify_limit:
        conn = get_connection()
        live = liveness.verify_jobs(
            conn,
            tiers=["priority", "recommended"],
            max_age_days=7,
            limit=verify_limit,
            workers=verify_workers,
            dry_run=False,
        )
        by_status = live.get("by_status", {}) or {}
        console.print(
            "  Liveness checked:    "
            f"{live.get('checked', 0)} "
            f"(live={by_status.get('live', 0)}, dead={by_status.get('dead', 0)}, "
            f"uncertain={by_status.get('uncertain', 0)})"
        )

    passes = 0
    last_ready = int(get_stats().get("ready_to_apply", 0) or 0)
    while last_ready < target_ready and passes < max_passes:
        passes += 1
        console.print(
            f"\n  [cyan]Generation pass {passes}/{max_passes}[/cyan] "
            f"(ready={last_ready}, batch={batch_size})"
        )
        result = run_pipeline(
            stages=["tailor", "cover", "pdf"],
            min_score=min_score,
            batch_size=batch_size,
            workers=1,
            generation_workers=generation_workers,
            validation_mode=validation,
            discover_mode="safe",
        )
        if result.get("errors"):
            console.print(f"[red]Generation stopped with errors:[/red] {result['errors']}")
            raise typer.Exit(code=1)
        current_ready = int(get_stats().get("ready_to_apply", 0) or 0)
        console.print(f"  Ready now:           {current_ready}")
        if current_ready <= last_ready:
            console.print("[yellow]Ready queue did not grow this pass; stopping to avoid a loop.[/yellow]")
            break
        last_ready = current_ready

    final = get_stats()
    console.print("\n[bold green]ApplyPilot output boost complete[/bold green]")
    console.print(f"  Ready to apply:      {final.get('ready_to_apply', 0)}")
    console.print(f"  Tailored resumes:    {final.get('tailored', 0)}")
    console.print(f"  Cover letters:       {final.get('with_cover_letter', 0)}")
    console.print(f"  Applied:             {final.get('applied', 0)}")

    if start_apply:
        console.print("\n[bold blue]Starting supervised apply[/bold blue]")
        console.print(f"  Agents hint:         {agents}")
        from applypilot.apply.supervisor import supervise

        supervise(
            total_cost_usd=max_cost_usd,
            model=model,
            workers=apply_workers,
            linkedin_daily_cap=linkedin_daily_cap,
            base_resume=True,
            max_job_age_days=max_job_age_days,
            lane_filter=True,
            preflight_liveness=True,
        )
    else:
        console.print("[dim]Apply was not started. Pass --start-apply after stopping any existing apply worker.[/dim]")


@app.command("apply-cost-report")
def apply_cost_report_command(
    pg_dsn: Optional[str] = typer.Option(None, "--dsn", help="Fleet Postgres DSN. Defaults to FLEET_PG_DSN or local fleet DB."),
    sqlite_path: Optional[str] = typer.Option(None, "--sqlite", help="Local ApplyPilot SQLite brain path."),
) -> None:
    """Print quality-adjusted apply cost and success metrics."""
    from applypilot.config import load_env

    load_env()
    from applypilot.fleet.cost_quality_report import build_report, render_report_markdown

    report = build_report(pg_dsn=pg_dsn, sqlite_path=sqlite_path)
    console.print(render_report_markdown(report), markup=False)


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
        if r in RECOVERABLE:
            return "recoverable"
        if r in DEAD:
            return "dead"
        if r in BLOCKED:
            return "blocked"
        if r in AGENT:
            return "agent"
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
    table.add_column("Session")
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


@app.command("parse-health")
def parse_health_command() -> None:
    """Run parse-quality refresh + drift snapshot and print threshold alerts."""
    from applypilot import config, database

    _bootstrap()
    conn = database.init_db(config.DB_PATH)
    database.refresh_desc_quality(conn, limit=None)
    snapshot = database.snapshot_desc_quality(conn, window_days=7)

    rows = sorted(
        snapshot["rows"],
        key=lambda row: (row["board"] == "__all__", str(row["board"])),
    )

    table = Table(title="Parse-quality Drift Snapshot")
    table.add_column("Board")
    table.add_column("Window", justify="right")
    table.add_column("Total", justify="right")
    table.add_column("Null Rate", justify="right")
    table.add_column("Stub Rate", justify="right")
    table.add_column("Short <500", justify="right")
    table.add_column("HTML", justify="right")

    for row in rows:
        table.add_row(
            str(row["board"]),
            "7",
            str(row["total"]),
            f"{float(row['null_rate']):.2%}",
            f"{float(row['stub_rate']):.2%}",
            f"{float(row['short_rate']):.2%}",
            f"{float(row['html_rate']):.2%}",
        )
    console.print(table)

    alerts = []
    for row in rows:
        if row["board"] == "__all__":
            continue
        if float(row["null_rate"]) > 0.02:
            alerts.append(f"ALERT {row['board']}: null_rate={float(row['null_rate']):.2%}")
        if float(row["short_rate"]) > 0.05:
            alerts.append(f"ALERT {row['board']}: short_rate={float(row['short_rate']):.2%}")
        if float(row["html_rate"]) > 0.01:
            alerts.append(f"ALERT {row['board']}: html_rate={float(row['html_rate']):.2%}")
    if alerts:
        for alert in alerts:
            console.print(f"[red]{alert}[/red]")
    else:
        console.print("[green]No parse-quality drift alerts.[/green]")


@app.command("parse-spot-audit")
def parse_spot_audit_command(
    sample: int = typer.Option(50, "--sample", help="Max jobs to sample across board×band."),
    window_days: int = typer.Option(30, "--window-days", help="Lookback window in days."),
) -> None:
    """Sample jobs for lightweight parse-quality spot checks and persist results."""
    from applypilot import config, database, llm

    _bootstrap()
    if sample < 0:
        console.print("[red]Invalid --sample: must be >= 0[/red]")
        raise typer.Exit(code=1)
    if window_days <= 0:
        console.print("[red]Invalid --window-days: must be > 0[/red]")
        raise typer.Exit(code=1)

    conn = database.get_connection(config.DB_PATH)
    cutoff = (_dt.now(_tz.utc) - _td(days=window_days)).isoformat()
    raw_rows = conn.execute(
        """
        SELECT
            url,
            title,
            COALESCE(source_board, strategy, site, 'unknown') AS board,
            COALESCE(audit_score, fit_score) AS score,
            full_description
        FROM jobs
        WHERE duplicate_of_url IS NULL
          AND discovered_at IS NOT NULL
          AND datetime(discovered_at) >= datetime(?)
        ORDER BY discovered_at DESC
        """,
        (cutoff,),
    ).fetchall()

    def _band(score: float | int | None) -> str:
        if score is None:
            return "unscored"
        return "high" if float(score) >= 6 else "low"

    buckets: defaultdict[str, list[tuple[str, str, str | None]]] = defaultdict(list)
    for row in raw_rows:
        b = str(row["board"])
        buckets[(b, _band(row["score"]))].append((row["url"], row["title"], row["full_description"]))

    if not buckets:
        console.print("[yellow]No jobs available for parse-spot audit.[/yellow]")
        return

    if sample == 0:
        sample = len(raw_rows)

    grouped = sorted(buckets.items(), key=lambda kv: (kv[0][0], {"unscored": 2, "low": 1, "high": 0}.get(kv[0][1], 99)))
    cursor = {key: 0 for key, _ in grouped}
    sampled: list[tuple[str, str, str, str]] = []
    while len(sampled) < sample:
        progressed = False
        for (board, band), rowset in grouped:
            i = cursor[(board, band)]
            if i < len(rowset):
                url, title, description = rowset[i]
                sampled.append((board, band, str(url), str(title or "")))
                cursor[(board, band)] += 1
                progressed = True
                if len(sampled) >= sample:
                    break
        if not progressed:
            break

    client = llm.get_client(stage="parse_audit")
    sampled_at = _dt.now(_tz.utc).isoformat()
    by_board = defaultdict(lambda: {"total": 0, "complete": 0})

    for board, band, url, title in sampled:
        row = conn.execute("SELECT full_description FROM jobs WHERE url = ?", (url,)).fetchone()
        desc = row["full_description"] if row else None
        short_desc = (desc or "")
        if len(short_desc) > 8000:
            short_desc = short_desc[:8000]

        prompt = (
            "You are a strict job-description quality checker. Return JSON only.\n\n"
            f"Title: {title}\nDescription:\n{short_desc}"
        )
        try:
            raw = client.chat([
                {"role": "system", "content": "Return only compact JSON."},
                {"role": "user", "content": prompt},
            ], max_tokens=200)
        except Exception:
            raw = "{}"
        complete = 0
        defects = []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                complete = 1 if str(parsed.get("complete", "n")).strip().lower() in {"y", "yes", "1", "true"} else 0
                raw_defects = parsed.get("defects", [])
                if isinstance(raw_defects, list):
                    defects = [str(v) for v in raw_defects]
        except Exception:
            complete = 0
            defects = []

        by_board[board]["total"] += 1
        by_board[board]["complete"] += complete
        conn.execute(
            """
            INSERT INTO parse_spot_audit (
                audited_at, url, board, band, complete, defects, model
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """
            ,
            (
                sampled_at,
                url,
                board,
                band,
                complete,
                json.dumps(defects),
                getattr(client, "model", None),
            ),
        )
    conn.commit()

    for board, stat in sorted(by_board.items(), key=lambda kv: kv[0]):
        total = int(stat["total"])
        complete_n = int(stat["complete"])
        ratio = (complete_n / total) if total else 0.0
        console.print(f"board={board} complete={ratio:.0%} ({complete_n}/{total})")
        if ratio < 0.9:
            console.print(f"[red]ALERT board={board}: complete rate below 90%[/red]")
    console.print(f"[green]parse-spot-audit sampled {len(sampled)} jobs[/green]")


@app.command()
def apply(
    limit: Optional[int] = typer.Option(None, "--limit", "-l", help="Max applications to submit."),
    workers: int = typer.Option(2, "--workers", "-w", help="Number of parallel browser workers. Account-safe: the LinkedIn daily cap and per-host throttle are process-global, shared across workers."),
    min_score: Optional[int] = typer.Option(None, "--min-score", help="Minimum fit score for job selection. Defaults to APPLYPILOT_MIN_SCORE or 7."),
    agent: str = typer.Option("codex", "--agent", help="Apply agent CLI to run: claude or codex. Defaults to codex to keep apply off the Claude Max subscription."),
    agents: Optional[str] = typer.Option(None, "--agents", help="Comma-separated per-worker agents (round-robin), e.g. 'claude,codex' to run BOTH concurrently in one process. Overrides --agent; needs --workers >= the number of agents."),
    browsers: Optional[str] = typer.Option(None, "--browsers", help="Comma-separated per-worker browsers (round-robin), e.g. 'chrome,edge' to run real Chrome + real Edge. Edge has no Chrome LinkedIn session -> auto-restricted to the offsite lane."),
    model: Optional[str] = typer.Option(None, "--model", "-m", help="Apply-agent model name. Defaults to sonnet for Claude; Codex uses its configured default when omitted."),
    poll_interval: int = typer.Option(15, "--poll-interval", help="Seconds a worker waits between DB polls when the queue is empty."),
    continuous: bool = typer.Option(False, "--continuous", "-c", help="Run forever, polling for new jobs."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview actions without submitting."),
    headless: bool = typer.Option(False, "--headless", help="Run browsers in headless mode."),
    base_resume: bool = typer.Option(False, "--base-resume", help="Apply with the base resume as-is (no per-job tailoring); jobs lacking a tailored resume fall back to .applypilot/resume.pdf."),
    inbox_auth: bool | None = typer.Option(
        None,
        "--inbox-auth/--no-inbox-auth",
        help="Enable or disable Gmail inbox auth code automation during apply retries."
    ),
    max_cost_usd: float = typer.Option(0.0, "--max-cost-usd", help="Stop the run once estimated apply cost reaches this USD amount (0 = no cap)."),
    linkedin_daily_cap: int = typer.Option(-1, "--linkedin-daily-cap", help="Rolling-24h cap on LinkedIn Easy-Apply submissions; offsite lane keeps flowing after the cap. -1 = use default (20), 0 = no cap."),
    url: Optional[str] = typer.Option(None, "--url", help="Apply to a specific job URL."),
    gen: bool = typer.Option(False, "--gen", help="Generate prompt file for manual debugging instead of running."),
    preflight: bool = typer.Option(True, "--preflight/--skip-preflight", help="Run readiness checks before launching the apply agent."),
    stale_days: int = typer.Option(21, "--stale-days", help="Preflight warning threshold for stale jobs."),
    mark_applied: Optional[str] = typer.Option(None, "--mark-applied", help="Manually mark a job URL as applied."),
    mark_failed: Optional[str] = typer.Option(None, "--mark-failed", help="Manually mark a job URL as failed (provide URL)."),
    fail_reason: Optional[str] = typer.Option(None, "--fail-reason", help="Reason for --mark-failed."),
    reset_failed: bool = typer.Option(False, "--reset-failed", help="Reset failed jobs for retry (skips possibly-submitted crash_unconfirmed/no_confirmation jobs to avoid double-applying)."),
    auth_gated: bool = typer.Option(False, "--auth-gated", help="Owner-supervised auth-gated lane: headed + home-box, applies to supervised/trusted ATS tenants only. You watch; the agent applies fully (no confirm pause). A challenge (CAPTCHA/login wall) halts that tenant for the rest of the UTC day."),
    tenant: Optional[str] = typer.Option(None, "--tenant", help="With --auth-gated: scope this run to a single tenant host (e.g. acme.myworkdayjobs.com)."),
) -> None:
    """Launch auto-apply to submit job applications."""
    _require_local_apply_admission(target_url=url)
    _bootstrap()

    from applypilot import config
    from applypilot.config import PROFILE_PATH as _profile_path
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
    browser_list: Optional[list] = None
    if browsers:
        browser_list = [b.strip().lower() for b in browsers.split(",") if b.strip()]
        bad_b = [b for b in browser_list if b not in {"chrome", "edge", "cft", "chromium", "default"}]
        if bad_b:
            console.print(f"[red]--browsers entries must be chrome/edge (got: {', '.join(bad_b)}).[/red]")
            raise typer.Exit(code=1)
        if not browser_list:
            browser_list = None
        elif workers < len(set(browser_list)):
            console.print(f"[yellow]--workers ({workers}) < distinct browsers ({len(set(browser_list))}); "
                          f"some browsers won't run. Raise --workers.[/yellow]")
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
    if inbox_auth is True:
        import os
        os.environ["APPLYPILOT_INBOX_AUTH"] = "1"
    elif inbox_auth is False:
        import os
        os.environ["APPLYPILOT_INBOX_AUTH"] = "0"

    # --auth-gated: owner-supervised lane (Task 5 of the auth-gated-tenant-lane
    # plan). Headed + home-box only; scoped to supervised/trusted ATS tenants.
    # Supervised design (owner decision 2026-07-03, amendment 0b2fead): no
    # confirm-before-submit pause -- the owner watches, the agent applies
    # fully, and record_tenant_outcome() records the real terminal status.
    if auth_gated:
        import os
        from applypilot.database import get_connection as _get_conn
        from applypilot import tenants as _tenants

        os.environ["APPLYPILOT_AUTH_GATED_MODE"] = "supervised"
        # Force headed (the owner must be able to watch) and home-box (this
        # lane never runs on the fleet -- fleet_sync.py excludes auth-gated
        # jobs regardless of tenant status, per Task 3).
        headless = False
        os.environ.pop("FLEET_PG_DSN", None)
        if tenant:
            os.environ["APPLYPILOT_AUTH_GATED_TENANT_HOST"] = tenant.strip().lower()
        else:
            os.environ.pop("APPLYPILOT_AUTH_GATED_TENANT_HOST", None)

        _conn = _get_conn()
        _rows = _tenants.list_tenants(_conn)
        _enabled = [r for r in _rows if r["status"] in ("supervised", "trusted")]
        if tenant:
            _enabled = [r for r in _enabled if r["host"] == tenant.strip().lower()]
        if not _enabled:
            _excluded = [r["host"] for r in _rows if r["status"] == "excluded"]
            if tenant:
                console.print(f"[red]No supervised/trusted tenant found for --tenant {tenant}.[/red]")
            else:
                console.print("[red]No supervised or trusted ATS tenants are registered -- nothing to run.[/red]")
            if _excluded:
                console.print(f"[dim]Excluded tenants: {', '.join(_excluded)}[/dim]")
            console.print(
                "[dim]Enable one with: [bold]applypilot tenants set <host> supervised[/bold][/dim]"
            )
            return

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
    config.check_tier(3, "auto-apply")

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
    if not gen and preflight:
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
    console.print(f"  Browser:  {'+'.join(dict.fromkeys(browser_list)) if browser_list else 'default (Chrome for Testing)'}")
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
        browsers=browser_list,
        supervised=auth_gated,
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


@app.command("answer")
def answer_command(
    question: str = typer.Option(..., "--question", "-q", help="The application question to answer."),
    title: str = typer.Option("", "--title", help="Job title, for context."),
    company: str = typer.Option("", "--company", help="Company name, for context."),
    description: str = typer.Option("", "--description", help="Job description snippet, for context."),
    kind: Optional[str] = typer.Option(None, "--kind", help="Question kind hint: motivation | behavioral | open."),
    remember: bool = typer.Option(False, "--remember", help="Save the verified answer to the corpus for future retrieval."),
) -> None:
    """Generate ONE verified free-text application answer with the cheap model.

    Retrieves the candidate's past approved answers, asks the answer-stage LLM
    (DeepSeek by default), and runs the deterministic verifier. Prints the answer
    only if it passes; otherwise exits non-zero so an optional field is left blank
    or escalated to a human.
    """
    _bootstrap()

    # Model answers routinely contain em-dashes / curly quotes; force UTF-8 so a
    # Windows cp1252 console doesn't UnicodeEncodeError on the printed answer.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    from applypilot import config
    from applypilot.apply.answerer import (
        AnswerCorpus,
        answer_question,
        default_corpus_path,
        remember_answer,
    )

    profile = config.load_profile()
    resume_text = ""
    if config.RESUME_PATH.exists():
        resume_text = config.RESUME_PATH.read_text(encoding="utf-8", errors="ignore")

    job = {"title": title, "site": company, "description": description}
    corpus = AnswerCorpus.from_jsonl(default_corpus_path())
    res = answer_question(question, job=job, profile=profile,
                          resume_text=resume_text, corpus=corpus, kind=kind)

    if not res.verified:
        console.print(
            f"[yellow]No verified answer (failed: {', '.join(res.checks) or 'unknown'}). "
            f"Leave blank if optional, or answer manually.[/yellow]"
        )
        raise typer.Exit(code=2)

    console.print(res.text)
    console.print(
        f"[dim][verified] via {res.model} in {res.attempts} attempt(s); "
        f"used {len(res.retrieved)} past answer(s)[/dim]"
    )
    if remember:
        remember_answer(question, res.text, job=job)
        console.print("[dim]saved to corpus[/dim]")


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


@app.command("outcomes-export")
def outcomes_export_command(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination folder. Defaults to a timestamped application_exports folder."),
) -> None:
    """Export email events + per-application outcome timelines (JSONL) for the learning loop / external tools."""
    _bootstrap()
    from applypilot.outcome_export import export_outcome_events
    result = export_outcome_events(output_dir=output)
    console.print("\n[bold green]Outcome export complete[/bold green]")
    console.print(f"  Email events:      {result['email_events_exported']}")
    console.print(f"  Outcome timelines: {result['outcome_timelines_exported']}")
    console.print(f"  Folder:            {result['output_dir']}")
    console.print()


@app.command("outcomes-promote")
def outcomes_promote_command() -> None:
    """PREVIEW ONLY: show what email outcomes WOULD promote into the application tracker.
    Reads the tracker read-only and writes NOTHING (no apply flag; promotion is parked)."""
    _bootstrap()
    from applypilot.config import DB_PATH
    from applypilot.outcome_dashboard import _read_only_conn, build_application_rows
    from applypilot.outcome_implied import implied_status

    conn = _read_only_conn(DB_PATH)
    rows = build_application_rows(conn)
    table = Table(title="Implied promotions (PREVIEW — writes nothing)", show_header=True, header_style="bold")
    table.add_column("Company")
    table.add_column("Current tracker")
    table.add_column("Implied")
    table.add_column("Would")
    shown = 0
    for row in rows:
        imp = implied_status(row)
        if not imp:
            continue
        cur = conn.execute(
            "SELECT status, last_status_at FROM applications WHERE job_url = ?",
            (row["job_url"],),
        ).fetchone()
        current = cur["status"] if cur else "(none)"
        last_at = cur["last_status_at"] if cur else None
        # Recency guard (display only): an email older than the known status would be stale.
        if last_at and imp["occurred_at"] and imp["occurred_at"] <= last_at:
            verdict = "skip (stale)"
        elif current == imp["implied_status"]:
            verdict = "no change"
        else:
            verdict = "advance"
        table.add_row(row.get("company") or "?", str(current), imp["implied_status"], verdict)
        shown += 1
    if shown:
        console.print(table)
    else:
        console.print("[dim]No implied promotions (no offer/interview/rejected outcomes yet).[/dim]")
    console.print("\n[dim]Preview only — nothing was written. Promotion is parked (spec 2026-06-30 #3).[/dim]\n")


@app.command("outcomes-lanes")
def outcomes_lanes_command(
    floor: int = typer.Option(8, "--floor", help="Min applications in a lane before it can be flagged."),
) -> None:
    """Advisory: which coarse lanes (board/role/seniority/score-band/...) respond above/below
    your baseline. Read-only; NEVER folded into scoring or the apply gate."""
    _bootstrap()
    from applypilot.config import DB_PATH
    from applypilot.outcome_dashboard import _read_only_conn, build_application_rows
    from applypilot.outcome_lane_signal import compute_lane_report

    conn = _read_only_conn(DB_PATH)
    rows = build_application_rows(conn)
    rep = compute_lane_report(rows, floor=floor)
    console.print(f"\n[bold]Lane signal[/bold]  (n={rep['n']}, baseline reply rate "
                  f"{rep['baseline_response_rate'] * 100:.0f}%)  [dim]advisory only[/dim]")
    table = Table(show_header=True, header_style="bold")
    table.add_column("Flag", style="bold")
    table.add_column("Lane")
    table.add_column("Reply rate")
    table.add_column("n")
    for s in rep["warm"] + rep["cold"]:
        color = "green" if s["flag"] == "warm" else "red"
        table.add_row(f"[{color}]{s['flag']}[/{color}]", f"{s['dimension']}={s['value']}",
                      f"{s['response_rate'] * 100:.0f}%", f"{s['n_responded']}/{s['n_applied']}")
    if rep["warm"] or rep["cold"]:
        console.print(table)
    else:
        console.print("[dim]No lanes meet the sample-size floor yet (need more outcomes).[/dim]")
    console.print()


@app.command("supervise-apply")
def supervise_apply_command(
    max_cost_usd: float = typer.Option(..., "--max-cost-usd",
        help="TOTAL cost budget (USD) across all auto-restarts."),
    model: str = typer.Option("sonnet", "--model", "-m"),
    workers: int = typer.Option(1, "--workers", "-w",
        help="Parallel apply browser workers. Account-safe: the LinkedIn daily cap and "
             "per-host throttle are process-global (shared across workers), and the lease "
             "is re-stamped at launch so the reclaimer can't double-submit. 1 is "
             "conservative; 2 ~doubles throughput on a machine with RAM headroom."),
    linkedin_daily_cap: int = typer.Option(20, "--linkedin-daily-cap"),
    base_resume: bool = typer.Option(True, "--base-resume/--no-base-resume"),
    max_job_age_days: int = typer.Option(0, "--max-job-age-days",
        help="Freshness filter: skip postings older than N days (0 = off)."),
    lane_filter: bool = typer.Option(True, "--lane-filter/--no-lane-filter",
        help="Skip clearly off-lane roles (IC-sales/AE) so a drained on-lane queue "
             "doesn't drift; on-lane audit flags override. On by default for the "
             "supervised/production run."),
    preflight_liveness: bool = typer.Option(True, "--preflight-liveness/--no-preflight-liveness",
        help="HTTP-probe each posting for closure before launching Chrome (one read-only "
             "GET); skip dead-on-visit postings so they don't burn a ~$1.50 launch. "
             "Conservative -- only a strong DEAD signal skips. On by default."),
    stall_minutes: float = typer.Option(20.0, "--stall-minutes",
        help="Kill + restart if no output AND no new apply for this long."),
    max_attempts: int = typer.Option(30, "--max-attempts"),
    max_hours: float = typer.Option(14.0, "--max-hours"),
    est_cost_per_apply: float = typer.Option(1.5, "--est-cost-per-apply",
        help="Used to estimate spend from applied-count across crashes."),
    target_applied: int = typer.Option(0, "--target-applied",
        help="Absolute stop: run until COUNT(applied) reaches this (composes across "
             "restarts; writes a done-marker for an outer keep-alive task). 0 = use --max-cost-usd."),
) -> None:
    """Run apply under a crash/stall SUPERVISOR that auto-restarts until the budget is spent.

    The apply run is heavy and can be OOM-killed on a contended machine -- a silent death
    it can't recover from. This supervisor is a separate lightweight process: it detects a
    crash within ~30s (and a stall after --stall-minutes), cleans up orphaned Chrome / MCP
    servers, and relaunches with the remaining budget. Logs to <LOG_DIR>/supervisor.log.
    """
    _require_local_apply_admission()
    _bootstrap()
    from applypilot.apply.supervisor import supervise
    supervise(
        total_cost_usd=max_cost_usd, model=model, workers=workers,
        linkedin_daily_cap=linkedin_daily_cap,
        base_resume=base_resume, max_job_age_days=max_job_age_days, lane_filter=lane_filter,
        preflight_liveness=preflight_liveness,
        stall_minutes=stall_minutes, max_attempts=max_attempts, max_hours=max_hours,
        est_cost_per_apply=est_cost_per_apply, target_applied=target_applied,
    )


@app.command("audit-duplicates")
def audit_duplicates_command() -> None:
    """Report likely DUPLICATE applications (the same role applied more than once).

    Catches near-duplicate company repostings -- one role listed twice with a tweaked
    title + a new ATS job ID (e.g. Amae Health "Founder Associate" and "Business
    Development Associate", both ".../Growth & Partnership Operations" on
    greenhouse.io/amaehealth) -- which exact (company,title)+url dedup cannot see.
    Read-only; run it anytime to spot double-applies the live guard may have missed.
    """
    _bootstrap()
    from applypilot.apply.launcher import audit_duplicate_applications

    dups = audit_duplicate_applications()
    if not dups:
        console.print("[green]No duplicate applications detected.[/green]")
        return
    console.print(f"\n[bold yellow]{len(dups)} likely-duplicate application pair(s):[/bold yellow]\n")
    for d in dups:
        console.print(f"  [bold]{d['employer']}[/bold]  [dim]({d['kind']})[/dim]")
        console.print(f"     - {d['title_a']}  [dim]{(d['applied_a'] or '')[:19]}[/dim]")
        console.print(f"     - {d['title_b']}  [dim]{(d['applied_b'] or '')[:19]}[/dim]")
        if d["shared_tokens"]:
            console.print(f"       [dim]shared role tokens: {', '.join(d['shared_tokens'])}[/dim]")
    console.print()


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

    _COLORS = {"offer": "green", "interview": "cyan", "rejected": "red",
               "acknowledged": "blue"}

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
    if counts.get("skipped_acknowledged"):
        console.print(f"  Acknowledgments (receipts, not written): {counts['skipped_acknowledged']}")
    if counts["skipped_no_match"]:
        console.print(f"  Skipped (no job match):  {counts['skipped_no_match']}")
    if counts["skipped_ambiguous"]:
        console.print(f"  Skipped (ambiguous):     {counts['skipped_ambiguous']}")
    if counts.get("errors"):
        console.print(f"  [red]Errors:[/red]              {counts['errors']}")
    if dry_run:
        console.print("\n[dim]Re-run with --apply to write these outcomes to the tracker.[/dim]")
    console.print()


@app.command("outcomes-scan")
def outcomes_scan_command(
    days: int = typer.Option(30, "--days", "-d", help="How many days back to search."),
    max_messages: int = typer.Option(200, "--max-messages", "-n", help="Max emails to scan (paginated; Gmail pages are 500)."),
    concurrency: int = typer.Option(8, "--concurrency", "-j", help="Parallel LLM extractions (network-bound, so higher = faster)."),
    reextract: bool = typer.Option(False, "--reextract", help="Re-run LLM extraction on already-seen emails."),
    credentials: Optional[Path] = typer.Option(None, "--credentials", help="Path to gmail_credentials.json."),
    reaudit: bool = typer.Option(
        False, "--reaudit",
        help="Re-run match guards over stored email_events (no Gmail calls); reversible via prev_job_url."),
) -> None:
    """Scan Gmail and populate the email_events outcome timeline (LLM extraction)."""
    _bootstrap()

    if reaudit:
        from applypilot.database import get_connection
        from applypilot.outcome_reaudit import reaudit_email_events
        conn = get_connection()
        report = reaudit_email_events(conn)

        table = Table(title="Outcome re-audit", show_header=True, header_style="bold")
        table.add_column("Result", style="bold")
        table.add_column("Count", justify="right")
        table.add_row("checked", str(report["checked"]))
        table.add_row("backfilled", str(report["backfilled"]))
        table.add_row("flipped", str(sum(report["flipped"].values())))
        console.print(table)

        if report["flipped"]:
            reasons = report["flipped"]
            console.print(
                "flipped reasons: " + ", ".join(f"{k}={v}" for k, v in sorted(reasons.items()))
            )
        return

    from applypilot.outcome_scan import scan_outcomes
    try:
        counts = scan_outcomes(
            days=days, credentials_path=credentials, reextract=reextract,
            max_messages=max_messages, concurrency=concurrency,
        )
    except FileNotFoundError as exc:
        console.print(f"[red]Setup required:[/red]\n{exc}")
        raise typer.Exit(1)
    except ImportError as exc:
        console.print(f"[red]Missing dependencies:[/red] {exc}")
        raise typer.Exit(1)

    table = Table(title="Outcome scan", show_header=True, header_style="bold")
    table.add_column("Result", style="bold")
    table.add_column("Count", justify="right")
    for k in ("inserted", "updated", "skipped", "errors"):
        table.add_row(k, str(counts.get(k, 0)))
    console.print(table)

    needs_review = counts.get("needs_review", 0)
    if needs_review:
        reasons = counts.get("needs_review_reasons", {}) or {}
        console.print(
            f"needs_review: {needs_review} "
            f"(predates={reasons.get('predates_application', 0)} "
            f"ambiguous={reasons.get('ambiguous_company', 0)} "
            f"no_timestamp={reasons.get('no_timestamp', 0)})"
        )


@app.command("outcomes-dashboard")
def outcomes_dashboard_command(
    port: int = typer.Option(8765, "--port", "-p", help="Port to serve on (localhost)."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind (loopback/private only)."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the browser."),
) -> None:
    """Serve the local read-only outcomes dashboard (timeline, analytics, lanes)."""
    _bootstrap()
    from applypilot.outcome_dashboard import serve
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://{host}:{port}")
    serve(host=host, port=port)


@app.command("outcomes-operator")
def outcomes_operator_command(
    port: int = typer.Option(8765, "--port", "-p", help="Port to serve on (localhost)."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind (loopback/private only)."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the browser."),
) -> None:
    """Serve the outcomes operator surface with review/action queues."""
    outcomes_dashboard_command(port=port, host=host, open_browser=open_browser)


@outcomes_review_app.command("queue")
def outcomes_review_queue_command() -> None:
    """Show outcome events that still need review."""
    _bootstrap()
    from applypilot.database import get_connection
    from applypilot.outcome_review import list_review_queue

    conn = get_connection()
    rows = list_review_queue(conn)
    if not rows:
        console.print("[dim]No outcome review items queued.[/dim]")
        return

    table = Table(title="Outcome review queue", show_header=True, header_style="bold")
    table.add_column("Message")
    table.add_column("Trust")
    table.add_column("Company")
    table.add_column("Title")
    table.add_column("Stage")
    table.add_column("Reason")
    for row in rows:
        table.add_row(
            row["message_id"],
            row["trust_state"],
            str(row.get("company") or ""),
            str(row.get("title") or ""),
            str(row.get("stage") or ""),
            str(row.get("match_reason") or row.get("reason") or ""),
        )
    console.print(table)


@outcomes_review_app.command("resolve")
def outcomes_review_resolve_command(
    message_id: str = typer.Argument(..., help="email_events.message_id to resolve."),
    resolution: str = typer.Option(..., "--resolution", help="trusted | needs_review | ignored | corrected"),
    job_url: Optional[str] = typer.Option(None, "--job-url", help="Override the matched job URL."),
    stage: Optional[str] = typer.Option(None, "--stage", help="Override the effective stage."),
    outcome: Optional[str] = typer.Option(None, "--outcome", help="Override the effective outcome."),
    confidence: Optional[str] = typer.Option(None, "--confidence", help="Override the effective confidence."),
    review_action: Optional[str] = typer.Option(None, "--review-action", help="Explicit review action label."),
    note: Optional[str] = typer.Option(None, "--note", help="Optional operator note."),
) -> None:
    """Resolve one outcome review item."""
    _bootstrap()
    from applypilot.database import get_connection
    from applypilot.outcome_review import record_review

    conn = get_connection()
    try:
        row = record_review(
            conn,
            message_id,
            resolution=resolution,
            review_action=review_action,
            corrected_job_url=job_url,
            corrected_stage=stage,
            corrected_outcome=outcome,
            corrected_confidence=confidence,
            note=note,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)

    console.print(
        f"[green]Recorded outcome review[/green] for [bold]{message_id}[/bold] "
        f"as [bold]{row['resolution']}[/bold]."
    )


@outcomes_alerts_app.command("digest")
def outcomes_alerts_digest_command(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination folder for digest artifacts."),
) -> None:
    """Write the current outcome digest as text/json artifacts."""
    _bootstrap()
    from applypilot.database import get_connection
    from applypilot.outcome_alerts import write_digest

    conn = get_connection()
    summary = write_digest(conn, output_dir=output)
    console.print("\n[bold green]Outcome digest written[/bold green]")
    console.print(f"  Critical alerts: {summary['critical_count']}")
    console.print(f"  Warning alerts:  {summary['warning_count']}")
    console.print(f"  Folder:          {summary['digest_dir']}")
    console.print()


@outcomes_learn_app.command("export")
def outcomes_learn_export_command(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination folder for trusted-learning exports."),
) -> None:
    """Export trusted-only learning reports and recommendation artifacts."""
    _bootstrap()
    from applypilot.database import get_connection
    from applypilot.outcome_learning import export_learning_bundle

    conn = get_connection()
    summary = export_learning_bundle(output_dir=output, conn=conn)
    console.print("\n[bold green]Outcome learning export complete[/bold green]")
    console.print(f"  Trusted rows:    {summary['trusted_rows']}")
    console.print(f"  Review queue:    {summary['review_queue_count']}")
    console.print(f"  Action queue:    {summary['action_queue_count']}")
    console.print(f"  Folder:          {summary['output_dir']}")
    console.print()


@app.command("linkedin-login")
def linkedin_login_command(
    timeout: int = typer.Option(420, "--timeout", help="Max seconds to wait for you to log in."),
    reset_workers: bool = typer.Option(
        False, "--reset-workers",
        help="After login, delete existing apply-worker profiles so they re-clone the "
             "LinkedIn session on the next run. STOP the apply run first."),
    browser: str = typer.Option("chrome", "--browser"),
) -> None:
    """One-time LinkedIn login so apply workers can apply via LinkedIn (~74% of the pool).

    Opens a visible Chrome on a dedicated seed profile; YOU log in (including any 2FA or
    security challenge) in that window. The tool NEVER types your password -- it only
    detects when the LinkedIn session cookie (li_at) appears, then captures it for the
    apply workers (which clone the seed profile).
    """
    _bootstrap()
    import shutil
    from applypilot.apply.chrome import linkedin_login

    console.print("\n[bold]LinkedIn login[/bold] -- opening a Chrome window.")
    console.print("  Log in to LinkedIn there (complete any 2FA / security checkpoint).")
    console.print("  [dim]The tool never types your password; it just waits for the session.[/dim]\n")

    ok, seed = linkedin_login(browser=browser, timeout_seconds=timeout)
    if not ok:
        console.print("[red]No LinkedIn session detected[/red] (li_at not found). "
                      "Re-run and finish the login, or raise --timeout.")
        raise typer.Exit(1)

    console.print(f"[green]✓ LinkedIn session captured[/green] in the seed profile ({seed.name}).")
    console.print("  Apply workers will clone this seed and inherit the session.")

    if reset_workers:
        from applypilot import config as _cfg
        removed = 0
        for d in _cfg.CHROME_WORKER_DIR.glob("worker-*"):
            try:
                shutil.rmtree(d)
                removed += 1
            except OSError as exc:
                console.print(f"  [yellow]Could not remove {d.name}[/yellow] "
                              f"(apply run using it? stop it first): {exc}")
        console.print(f"  Reset {removed} worker profile(s) -- they re-clone the LinkedIn "
                      "session on the next apply run.")
    else:
        console.print("  [dim]Existing worker profiles still hold the logged-out session. "
                      "Re-run with --reset-workers (apply run stopped) to refresh them now.[/dim]")
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
    """Import legacy recommendations as review/migration evidence only."""
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
        f"\n[dim]Imported for review{enrich_hint}; this command does not authorize applications. "
        "Run canonical scoring, replay, validation, and explicit promotion.[/dim]\n"
    )


@app.command("resbuild-promote")
def resbuild_promote_command(
    path: Path = typer.Argument(..., help="res_build apply-list JSONL (from applypilotExportApplyList.ts)."),
    source: str = typer.Option("res_build", "--source", help="decision_source tag written to promoted rows."),
    scale: str = typer.Option("ten", "--scale", help="Score scale of decision_score: ten (1-10), auto, unit, percent."),
    limit: int = typer.Option(0, "--limit", "-l", help="Promote only the top-N by the user's own score. 0 = all."),
    exclude_host: list[str] = typer.Option(["linkedin.com"], "--exclude-host",
                                            help="Host(s) to skip (repeatable). LinkedIn excluded by default -- its lane is separate."),
    include_applied: bool = typer.Option(False, "--include-applied",
                                         help="Also consider already-applied / duplicate rows (default: skip them)."),
    snapshot: Path = typer.Option(None, "--snapshot", help="Snapshot path for revert (default: <path>.snapshot.json)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview only -- no writes, no snapshot."),
) -> None:
    """Promote res_build's curated apply-list into ApplyPilot's apply gate (REVERSIBLE).

    Makes the jobs YOU reviewed and kept authoritative (audit_score + decision_source)
    so the apply gate selects them -- including the ones ApplyPilot's own ranker scores
    below the apply threshold. Promotion only STAGES jobs (nothing applies until you run
    the fleet). LinkedIn is excluded by default. Reverse with 'resbuild-revert <snapshot>'.
    """
    _bootstrap()
    from applypilot import resbuild_bridge as rb

    if scale not in {"auto", "ten", "unit", "percent"}:
        console.print(f"[red]Invalid --scale '{scale}'.[/red] Use one of: auto, ten, unit, percent.")
        raise typer.Exit(1)
    if not Path(path).exists():
        console.print(f"[red]Apply-list not found: {path}[/red]")
        raise typer.Exit(1)

    snap = snapshot if snapshot is not None else (None if dry_run else Path(str(path) + ".snapshot.json"))
    r = rb.promote(
        path, source=source, scale=scale,
        exclude_hosts=tuple(exclude_host), only_applyable=not include_applied,
        limit=(limit or None), snapshot_path=snap, dry_run=dry_run,
    )

    console.print(f"\n[bold green]{'DRY RUN (no writes)' if dry_run else 'Promoted'}[/bold green]")
    console.print(f"  Input records:         {r['input_records']}")
    console.print(f"  After filter:          {r['after_filter']}  (excluded hosts: {', '.join(r['excluded_hosts'])})")
    console.print(f"  Below-threshold UNLOCKED by bridge: {r['would_raise']}  (apply threshold {r['apply_threshold']})")
    if dry_run:
        console.print(f"  Would promote:         {r['would_promote']}")
        for u in r.get("sample", []):
            console.print(f"    [dim]{u}[/dim]")
        console.print("\n[dim]Re-run without --dry-run to apply. Reverse later with resbuild-revert.[/dim]")
    else:
        console.print(f"  Promoted (gate-authoritative): {r['promoted']}")
        console.print(f"  Excluded (fleet cross-check applied): {r['excluded_fleet_applied']}")
        console.print(f"  Fleet cross-check mode: {r['fleet_cross_check']}")
        c = r["import_counts"]
        if c.get("not_found_insufficient_metadata"):
            console.print(f"  [yellow]Apply-list URLs not in brain (skipped, never inserted): "
                          f"{c['not_found_insufficient_metadata']}[/yellow]")
        if c.get("skipped_already_applied"):
            console.print(f"  [dim]Skipped (already applied): {c['skipped_already_applied']}[/dim]")
        console.print(f"  [bold]Snapshot (for revert):[/bold] {r['snapshot_path']}")
        console.print("\n[dim]Now apply-eligible. Stage to the fleet with 'applypilot-fleet-apply-home push' "
                      "(or 'applypilot run tailor cover pdf' + 'applypilot apply').[/dim]")


@app.command("resbuild-revert")
def resbuild_revert_command(
    snapshot: Path = typer.Argument(..., help="Snapshot JSON written by resbuild-promote."),
    source: str = typer.Option("res_build", "--source", help="decision_source tag to match when reverting."),
) -> None:
    """Reverse a resbuild-promote: restore each promoted job's prior audit state.

    Only rows that still carry the promotion's decision_source tag are restored, so a
    job re-decided since the promotion is left untouched.
    """
    _bootstrap()
    from applypilot import resbuild_bridge as rb

    if not Path(snapshot).exists():
        console.print(f"[red]Snapshot not found: {snapshot}[/red]")
        raise typer.Exit(1)
    n = rb.revert(snapshot, source=source)
    console.print(f"[bold green]Reverted {n} job(s)[/bold green] to their pre-promotion (pre-res_build) state.")


@app.command("reenrich")
def reenrich_command(
    min_chars: int = typer.Option(200, "--min-chars", help="Re-scrape jobs whose stored description is shorter than this."),
    limit: int = typer.Option(0, "--limit", "-l", help="Max jobs to re-enrich. 0 = all eligible."),
    workers: int = typer.Option(1, "--workers", "-w", help="Parallel site-batch workers."),
    source_board: str | None = typer.Option(None, "--source-board", help="Limit re-enrichment to one source_board, e.g. hiringcafe."),
) -> None:
    """Re-fetch descriptions for jobs with a missing or too-thin description.

    Normal enrichment marks a job done even when it only captured a title/stub,
    so the job is never retried and gets dropped (or pollutes the recommendation
    engine's fit map) for lack of a real description. This gives those jobs a
    fresh scrape so good ones aren't lost.
    """
    _bootstrap()

    from applypilot.enrichment.detail import reenrich_thin_descriptions

    r = reenrich_thin_descriptions(min_chars=min_chars, limit=limit, workers=workers, source_board=source_board)

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

    # CapSolver (optional, but if configured it should be live and funded).
    from applypilot.apply import capsolver as capsolver_mod

    cap_status = capsolver_mod.check_balance(timeout=5.0)
    if not cap_status.configured:
        results.append(("CapSolver account", "[dim]optional[/dim]",
                        "Set CAPSOLVER_API_KEY in .env for CAPTCHA solving"))
    elif cap_status.ok:
        bal = f"; balance ${cap_status.balance:.2f}" if cap_status.balance is not None else ""
        results.append(("CapSolver account", ok_mark, f"API reachable{bal}"))
    elif cap_status.error_code == "network_error":
        results.append(("CapSolver account", warn_mark,
                        f"{cap_status.note} {cap_status.error_description or ''}".strip()))
    else:
        detail = f"{cap_status.error_code}: {cap_status.error_description}".strip(": ")
        results.append(("CapSolver account", fail_mark, detail or cap_status.note))

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


@app.command("capsolver-check")
def capsolver_check(
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable status JSON."),
) -> None:
    """Verify CapSolver key/account reachability without printing the secret."""
    from applypilot.apply import capsolver as capsolver_mod

    status = capsolver_mod.check_balance()
    if json_output:
        typer.echo(json.dumps(status.to_dict(), sort_keys=True))
    else:
        if status.ok:
            balance = f" Balance: ${status.balance:.2f}." if status.balance is not None else ""
            console.print(f"[green]OK[/green] CapSolver account reachable.{balance}")
        elif not status.configured:
            console.print("[yellow]MISSING[/yellow] CAPSOLVER_API_KEY is not set.")
        else:
            detail = f"{status.error_code or 'error'}: {status.error_description or status.note}"
            console.print(f"[red]FAILED[/red] {detail}")

    if not status.ok:
        raise typer.Exit(code=1)


@app.command("fleet-capsolver-check")
def fleet_capsolver_check(
    json_output: bool = typer.Option(False, "--json", help="Print machine-readable status JSON."),
) -> None:
    """Verify this machine is ready to handle fleet CAPTCHA solving."""
    from applypilot.apply import capsolver as capsolver_mod

    readiness = capsolver_mod.check_fleet_readiness()
    if json_output:
        typer.echo(json.dumps(readiness.to_dict(), sort_keys=True))
    else:
        if readiness.ready:
            balance = f" Balance: ${readiness.balance:.2f}." if readiness.balance is not None else ""
            console.print(f"[green]OK[/green] CapSolver fleet readiness passed.{balance}")
        else:
            detail = f"{readiness.error_code or 'error'}: {readiness.error_description or readiness.note}"
            console.print(f"[red]FAILED[/red] CapSolver fleet readiness failed. {detail}")

    if not readiness.ready:
        raise typer.Exit(code=1)


tenants_app = typer.Typer(
    name="tenants",
    help="Manage the ats_tenants registry (login-gated ATS rollout: excluded/supervised/trusted).",
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(tenants_app)


def _tenants_list() -> None:
    from applypilot.database import get_connection
    from applypilot import tenants

    conn = get_connection()
    rows = tenants.list_tenants(conn)

    table = Table(title="ATS Tenants", show_header=True, header_style="bold cyan")
    table.add_column("Host")
    table.add_column("Status")
    table.add_column("Clean", justify="right")
    table.add_column("Failed", justify="right")
    table.add_column("Daily cap", justify="right")
    table.add_column("Halted?")
    table.add_column("Eligible jobs", justify="right")

    now_iso = _dt.now(_tz.utc).isoformat()
    for row in rows:
        host = row["host"]
        halted = tenants.is_halted(conn, host, now_iso)
        eligible = conn.execute(
            "SELECT url, application_url FROM jobs WHERE applied_at IS NULL "
            "AND (url IS NOT NULL OR application_url IS NOT NULL)"
        ).fetchall()
        eligible_count = sum(
            1 for j in eligible
            if tenants._host_of(j["application_url"] or "") == host
            or tenants._host_of(j["url"] or "") == host
        )
        table.add_row(
            host,
            row["status"],
            row.get("session_state") or "supervised",
            str(row["clean_submits"]),
            str(row["failed_submits"]),
            str(row["daily_cap"]),
            "yes" if halted else "no",
            str(eligible_count),
        )

    if rows:
        console.print(table)
        for row in rows:
            console.print(
                f"[dim]{row['host']} status={row['status']} "
                f"session={row.get('session_state') or 'supervised'}[/dim]"
            )
    else:
        console.print("[dim]No tenants registered yet.[/dim]")


@tenants_app.callback(invoke_without_command=True)
def tenants_default(ctx: typer.Context) -> None:
    """List tenants when invoked bare (`applypilot tenants`)."""
    if ctx.invoked_subcommand is not None:
        return
    _bootstrap()
    _tenants_list()


@tenants_app.command("list")
def tenants_list_command() -> None:
    """List all registered ATS tenants (host, status, submit counts, halted?, eligible jobs)."""
    _bootstrap()
    _tenants_list()


@tenants_app.command("set")
def tenants_set_command(
    host: str = typer.Argument(..., help="ATS tenant host, e.g. acme.wd1.myworkdayjobs.com."),
    status: str = typer.Argument(..., help="excluded | supervised | trusted."),
    force: bool = typer.Option(False, "--force", help="Override the >=3-clean-submits evidence requirement for 'trusted'."),
) -> None:
    """Set a tenant's rollout status (excluded/supervised/trusted)."""
    _bootstrap()
    from applypilot.database import get_connection
    from applypilot import tenants

    conn = get_connection()
    try:
        row = tenants.set_tenant(conn, host, status, force=force)
    except ValueError as e:
        console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold green]{host}[/bold green] set to [bold]{row['status']}[/bold]")


@tenants_app.command("halt")
def tenants_halt_command(
    host: str = typer.Argument(..., help="ATS tenant host to halt until end of the current UTC day."),
) -> None:
    """Halt a tenant (block submits) until the end of the current UTC day."""
    _bootstrap()
    from applypilot.database import get_connection
    from applypilot import tenants

    conn = get_connection()
    until_iso = _dt.combine(
        _dt.now(_tz.utc).date(), _time(23, 59, 59), tzinfo=_tz.utc
    ).isoformat()
    tenants.halt_tenant(conn, host, until_iso)

    console.print(f"[bold yellow]{host}[/bold yellow] halted until [bold]{until_iso}[/bold]")


@tenants_app.command("session")
def tenants_session_command(
    host: str = typer.Argument(..., help="ATS tenant host."),
    state: str = typer.Argument(..., help="ready | supervised | expired."),
    ttl_hours: int = typer.Option(12, "--ttl-hours", min=1, help="Ready-session TTL."),
    reason: Optional[str] = typer.Option(None, "--reason", help="Non-secret readiness note."),
) -> None:
    """Set this machine's tenant browser-session readiness."""
    _bootstrap()
    from applypilot.database import get_connection
    from applypilot import tenants

    conn = get_connection()
    try:
        row = tenants.set_session_state(
            conn,
            host,
            state,
            ttl_hours=ttl_hours if state == "ready" else None,
            reason=reason,
        )
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1)
    console.print(
        f"[bold green]{host}[/bold green] session set to "
        f"[bold]{row['session_state']}[/bold] (profile {row['profile_id']})"
    )


if __name__ == "__main__":
    app()
