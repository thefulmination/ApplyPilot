"""ApplyPilot Pipeline Orchestrator.

Runs pipeline stages in sequence or concurrently (streaming mode).

Usage (via CLI):
    applypilot run                        # all stages, sequential
    applypilot run --stream               # all stages, concurrent
    applypilot run discover enrich        # specific stages
    applypilot run score tailor cover     # LLM-only stages
    applypilot run --dry-run              # preview without executing
"""

from __future__ import annotations

import logging
import inspect
import copy
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from applypilot.config import DEFAULTS, load_env, ensure_dirs
from applypilot.database import (
    create_pipeline_run,
    finish_pipeline_run,
    finish_pipeline_stage,
    get_connection,
    get_stats,
    init_db,
    start_pipeline_stage,
)

log = logging.getLogger(__name__)
console = Console()


# ---------------------------------------------------------------------------
# Stage definitions
# ---------------------------------------------------------------------------

STAGE_ORDER = ("discover", "enrich", "score", "audit", "diagnose", "tailor", "cover", "pdf")

STAGE_META: dict[str, dict] = {
    "discover": {"desc": "Job discovery (JobSpy + public boards + HiringCafe + corporate ATS + Workday + smart extract)"},
    "enrich":   {"desc": "Detail enrichment (full descriptions + apply URLs)"},
    "score":    {"desc": "LLM scoring (fit 1-10)"},
    "audit":    {"desc": "Score audit and reranking"},
    "diagnose": {"desc": "Fit diagnosis (why weak/strong, resume gap analysis)"},
    "tailor":   {"desc": "Resume tailoring (LLM + validation)"},
    "cover":    {"desc": "Cover letter generation"},
    "pdf":      {"desc": "PDF conversion (tailored resumes + cover letters)"},
}

# Upstream dependency: a stage only finishes when its upstream is done AND
# it has no remaining pending work.
_UPSTREAM: dict[str, str | None] = {
    "discover": None,
    "enrich":   "discover",
    "score":    "enrich",
    "audit":    "score",
    "diagnose": "audit",
    "tailor":   "diagnose",
    "cover":    "tailor",
    "pdf":      "cover",
}


# ---------------------------------------------------------------------------
# Individual stage runners
# ---------------------------------------------------------------------------

def _configured_workers(search_cfg: dict, section_name: str, fallback: int) -> int:
    """Return source-specific worker count from searches.yaml, with CLI fallback."""
    section = search_cfg.get(section_name, {}) or {}
    value = None
    if isinstance(section, dict):
        value = section.get("workers")
    if value is None:
        value = search_cfg.get(f"{section_name}_workers")
    if value is None:
        value = fallback
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return max(1, int(fallback))


def _cfg_bool(value, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _cfg_int_value(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _discovery_config(search_cfg: dict) -> dict:
    section = search_cfg.get("discovery", {}) or {}
    return section if isinstance(section, dict) else {}


def _source_enabled(search_cfg: dict, section_name: str, default: bool = True) -> bool:
    section = search_cfg.get(section_name, {}) or {}
    if isinstance(section, dict) and "enabled" in section:
        return _cfg_bool(section.get("enabled"), default)
    return default


def _fast_discovery_cfg(search_cfg: dict) -> dict:
    """Narrow expensive broad crawls for daily fast discovery."""
    cfg = copy.deepcopy(search_cfg)
    hiring_cfg = cfg.setdefault("hiring_cafe", {})
    if isinstance(hiring_cfg, dict):
        hiring_cfg["company_watchlist_enabled"] = False
        hiring_cfg["max_pages"] = min(_cfg_int_value(hiring_cfg.get("max_pages"), 2), 1)
        hiring_cfg["results_per_site"] = min(_cfg_int_value(hiring_cfg.get("results_per_site"), 50), 30)
    cfg["workday_max_pages"] = min(_cfg_int_value(cfg.get("workday_max_pages"), 10), 5)
    cfg["workday_max_results_per_employer_query"] = min(
        _cfg_int_value(cfg.get("workday_max_results_per_employer_query"), 150),
        100,
    )
    return cfg


def _discover_source_tasks(search_cfg: dict, workers: int, discover_mode: str = "safe") -> list[dict]:
    """Build source-level discovery tasks with source-specific risk controls."""
    mode = (discover_mode or "safe").strip().lower()
    if mode not in {"safe", "fast", "full"}:
        mode = "safe"

    base_cfg = _fast_discovery_cfg(search_cfg) if mode == "fast" else copy.deepcopy(search_cfg)
    discovery_cfg = _discovery_config(base_cfg)
    ats_fallback = 8 if mode in {"safe", "fast"} else workers
    workday_fallback = 4 if mode in {"safe", "fast"} else min(workers, 4)

    def task(name: str, label: str, enabled: bool, serial: bool, runner, cfg: dict, source_workers: int = 1) -> dict:
        return {
            "name": name,
            "label": label,
            "enabled": enabled,
            "serial": serial,
            "runner": runner,
            "cfg": cfg,
            "workers": max(1, source_workers),
        }

    tasks: list[dict] = []

    tasks.append(task(
        "jobspy",
        "JobSpy full crawl",
        _source_enabled(base_cfg, "jobspy", True),
        True,
        lambda cfg=base_cfg: __import__(
            "applypilot.discovery.jobspy", fromlist=["run_discovery"]
        ).run_discovery(cfg=cfg),
        base_cfg,
        1,
    ))
    tasks.append(task(
        "public_boards",
        "Public job-board APIs",
        _source_enabled(base_cfg, "public_boards", True),
        False,
        lambda cfg=base_cfg: __import__(
            "applypilot.discovery.public_boards", fromlist=["run_public_boards_discovery"]
        ).run_public_boards_discovery(cfg=cfg),
        base_cfg,
        1,
    ))
    tasks.append(task(
        "hiringcafe",
        "HiringCafe crawl",
        _source_enabled(base_cfg, "hiring_cafe", True),
        False,
        lambda cfg=base_cfg: __import__(
            "applypilot.discovery.hiringcafe", fromlist=["run_hiringcafe_discovery"]
        ).run_hiringcafe_discovery(cfg=cfg),
        base_cfg,
        1,
    ))
    ats_workers = _configured_workers(base_cfg, "corporate_ats", ats_fallback)
    tasks.append(task(
        "corporate_ats",
        "Corporate ATS crawl (Greenhouse + Lever + Ashby)",
        _source_enabled(base_cfg, "corporate_ats", True),
        False,
        lambda cfg=base_cfg, ats_workers=ats_workers: __import__(
            "applypilot.discovery.corporate_ats", fromlist=["run_corporate_ats_discovery"]
        ).run_corporate_ats_discovery(cfg=cfg, workers=ats_workers),
        base_cfg,
        ats_workers,
    ))
    workday_workers = _configured_workers(base_cfg, "workday", workday_fallback)
    tasks.append(task(
        "workday",
        "Workday corporate scraper",
        True,
        False,
        lambda workday_workers=workday_workers: __import__(
            "applypilot.discovery.workday", fromlist=["run_workday_discovery"]
        ).run_workday_discovery(workers=workday_workers),
        base_cfg,
        workday_workers,
    ))
    tasks.append(task(
        "smartextract",
        "Smart extract (AI-powered scraping)",
        _source_enabled(base_cfg, "smartextract", True),
        True,
        lambda smart_workers=_configured_workers(base_cfg, "smartextract", 1): __import__(
            "applypilot.discovery.smartextract", fromlist=["run_smart_extract"]
        ).run_smart_extract(workers=smart_workers),
        base_cfg,
        _configured_workers(base_cfg, "smartextract", 1),
    ))

    include = discovery_cfg.get("include_sources") or []
    exclude = set(discovery_cfg.get("exclude_sources") or [])
    if include:
        include_set = set(str(name).strip() for name in include)
        tasks = [t for t in tasks if t["name"] in include_set]
    if exclude:
        tasks = [t for t in tasks if t["name"] not in exclude]

    return tasks


def _discover_parallelism(search_cfg: dict, workers: int, discover_mode: str) -> int:
    discovery_cfg = _discovery_config(search_cfg)
    configured = discovery_cfg.get("source_parallelism")
    if configured is None:
        configured = 3 if discover_mode in {"safe", "fast"} else min(4, workers)
    return max(1, min(6, _cfg_int_value(configured, 3)))


def _run_discover_task(task: dict) -> tuple[str, str]:
    name = task["name"]
    label = task.get("label") or name
    console.print(f"  [cyan]{label}...[/cyan]")
    try:
        result = task["runner"]()
        status = "ok"
        if isinstance(result, dict):
            status = str(result.get("status") or "ok")
            if status == "ok" and int(result.get("errors") or 0) > 0:
                status = "partial"
        return name, status
    except Exception as e:
        log.error("%s failed: %s", label, e)
        console.print(f"  [red]{label} error:[/red] {e}")
        return name, f"error: {e}"


def _run_discover(workers: int = 1, discover_mode: str = "safe", search_cfg: dict | None = None) -> dict:
    """Stage: Job discovery with source-level scheduling and risk controls."""
    from applypilot import config

    search_cfg = search_cfg or config.load_search_config()
    mode = (discover_mode or "safe").strip().lower()
    tasks = [task for task in _discover_source_tasks(search_cfg, workers, mode) if task.get("enabled", True)]
    serial_tasks = [task for task in tasks if task.get("serial")]
    parallel_tasks = [task for task in tasks if not task.get("serial")]
    source_parallelism = _discover_parallelism(search_cfg, workers, mode)
    stats: dict[str, str] = {}

    console.print(f"  [dim]Discovery mode:[/dim] {mode} | source parallelism: {source_parallelism}")

    for task in serial_tasks:
        name, status = _run_discover_task(task)
        stats[name] = status

    if parallel_tasks:
        with ThreadPoolExecutor(max_workers=min(source_parallelism, len(parallel_tasks))) as pool:
            futures = {pool.submit(_run_discover_task, task): task for task in parallel_tasks}
            for future in as_completed(futures):
                name, status = future.result()
                stats[name] = status

    # Finalize: run the authoritative dedupe pass. Insert-time dedupe is
    # best-effort (parallel discovery threads can race, and a higher-quality row
    # can arrive after the row that became canonical). This reconciles the whole
    # table so downstream stages never enrich/score/apply to a duplicate.
    try:
        from applypilot.database import dedupe_existing_jobs, get_connection
        dd = dedupe_existing_jobs(get_connection())
        if dd.get("duplicates"):
            console.print(
                f"  [dim]Dedupe: {dd['duplicates']} duplicate(s) across {dd['groups']} group(s)[/dim]"
            )
    except Exception as e:
        log.warning("Dedupe finalize failed: %s", e)

    # Scrape quality check: warn on boards with high null rates so selector
    # drift is surfaced immediately in the same run that caused it.
    try:
        from applypilot.database import get_scrape_quality_report, get_connection
        qr = get_scrape_quality_report(get_connection())
        warn_boards = [
            b for b in qr.get("boards", [])
            if b.get("null_rate", 0) >= 0.20 and b.get("total", 0) >= 5
        ]
        if warn_boards:
            console.print("  [yellow]Scrape quality warnings (≥20% null rate):[/yellow]")
            for b in warn_boards:
                pct = round(b["null_rate"] * 100)
                console.print(f"    [yellow]• {b['board']}: {pct}% of {b['total']} jobs missing a signal field[/yellow]")
    except Exception as e:
        log.warning("Scrape quality check failed: %s", e)

    return stats


def _run_enrich(workers: int = 1) -> dict:
    """Stage: Detail enrichment — scrape full descriptions and apply URLs."""
    try:
        from applypilot.enrichment.detail import run_enrichment
        run_enrichment(workers=workers)
        return {"status": "ok"}
    except Exception as e:
        log.error("Enrichment failed: %s", e)
        return {"status": f"error: {e}"}


def _run_score(workers: int = 1) -> dict:
    """Stage: LLM scoring — assign fit scores 1-10."""
    try:
        from applypilot.scoring.scorer import run_scoring
        run_scoring(workers=workers)
        return {"status": "ok"}
    except Exception as e:
        log.error("Scoring failed: %s", e)
        return {"status": f"error: {e}"}


def _accepts_kwarg(fn: callable, name: str) -> bool:
    """Return true when a stage runner can receive a keyword argument."""
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return True
    return name in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )


def _run_audit() -> dict:
    """Stage: Score audit — demote false positives and promote target-lane roles."""
    try:
        from applypilot.scoring.audit import run_score_audit
        run_score_audit(write_reports=False)
        return {"status": "ok"}
    except Exception as e:
        log.error("Score audit failed: %s", e)
        return {"status": f"error: {e}"}


def _run_diagnose(batch_size: int | None = None) -> dict:
    """Stage: Fit diagnosis — explain gaps and whether they are resume-fixable."""
    try:
        from applypilot.scoring.diagnosis import run_diagnostics
        limit = DEFAULTS["generation_batch_size"] if batch_size is None else batch_size
        run_diagnostics(limit=limit)
        return {"status": "ok"}
    except Exception as e:
        log.error("Fit diagnosis failed: %s", e)
        return {"status": f"error: {e}"}


def _run_tailor(
    min_score: int = 7,
    validation_mode: str = "normal",
    batch_size: int | None = None,
) -> dict:
    """Stage: Resume tailoring — generate tailored resumes for high-fit jobs."""
    try:
        from applypilot.scoring.tailor import run_tailoring
        limit = DEFAULTS["generation_batch_size"] if batch_size is None else batch_size
        run_tailoring(min_score=min_score, limit=limit, validation_mode=validation_mode)
        return {"status": "ok"}
    except Exception as e:
        log.error("Tailoring failed: %s", e)
        return {"status": f"error: {e}"}


def _run_cover(
    min_score: int = 7,
    validation_mode: str = "normal",
    batch_size: int | None = None,
) -> dict:
    """Stage: Cover letter generation."""
    try:
        from applypilot.scoring.cover_letter import run_cover_letters
        limit = DEFAULTS["generation_batch_size"] if batch_size is None else batch_size
        run_cover_letters(min_score=min_score, limit=limit, validation_mode=validation_mode)
        return {"status": "ok"}
    except Exception as e:
        log.error("Cover letter generation failed: %s", e)
        return {"status": f"error: {e}"}


def _run_pdf(batch_size: int | None = None) -> dict:
    """Stage: PDF conversion — convert tailored resumes and cover letters to PDF."""
    try:
        from applypilot.scoring.pdf import batch_convert
        limit = DEFAULTS["generation_batch_size"] if batch_size is None else batch_size
        batch_convert(limit=limit)
        return {"status": "ok"}
    except Exception as e:
        log.error("PDF conversion failed: %s", e)
        return {"status": f"error: {e}"}


# Map stage names to their runner functions
_STAGE_RUNNERS: dict[str, callable] = {
    "discover": _run_discover,
    "enrich":   _run_enrich,
    "score":    _run_score,
    "audit":    _run_audit,
    "diagnose": _run_diagnose,
    "tailor":   _run_tailor,
    "cover":    _run_cover,
    "pdf":      _run_pdf,
}


# ---------------------------------------------------------------------------
# Stage resolution
# ---------------------------------------------------------------------------

def _resolve_stages(stage_names: list[str]) -> list[str]:
    """Resolve 'all' and validate/order stage names."""
    if "all" in stage_names:
        return list(STAGE_ORDER)

    resolved = []
    for name in stage_names:
        if name not in STAGE_META:
            console.print(
                f"[red]Unknown stage:[/red] '{name}'. "
                f"Available: {', '.join(STAGE_ORDER)}, all"
            )
            raise SystemExit(1)
        if name not in resolved:
            resolved.append(name)

    # Maintain canonical order
    return [s for s in STAGE_ORDER if s in resolved]


# ---------------------------------------------------------------------------
# Streaming pipeline helpers
# ---------------------------------------------------------------------------

class _StageTracker:
    """Thread-safe tracker for which stages have finished producing work."""

    def __init__(self):
        self._events: dict[str, threading.Event] = {
            stage: threading.Event() for stage in STAGE_ORDER
        }
        self._results: dict[str, dict] = {}
        self._lock = threading.Lock()

    def mark_done(self, stage: str, result: dict | None = None) -> None:
        with self._lock:
            self._results[stage] = result or {"status": "ok"}
        self._events[stage].set()

    def is_done(self, stage: str) -> bool:
        return self._events[stage].is_set()

    def wait(self, stage: str, timeout: float | None = None) -> bool:
        return self._events[stage].wait(timeout=timeout)

    def get_results(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._results)


# SQL to count pending work for each stage
_PENDING_SQL: dict[str, str] = {
    "enrich": "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL AND duplicate_of_url IS NULL",
    "score": (
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL "
        "AND fit_score IS NULL AND duplicate_of_url IS NULL"
    ),
    "audit":  (
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL "
        "AND duplicate_of_url IS NULL "
        "AND decision_source IS NULL "
        "AND (audited_at IS NULL OR (scored_at IS NOT NULL AND audited_at < scored_at))"
    ),
    "diagnose": (
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND full_description IS NOT NULL "
        "AND duplicate_of_url IS NULL "
        "AND (diagnosed_at IS NULL "
        "OR (scored_at IS NOT NULL AND diagnosed_at < scored_at) "
        "OR (audited_at IS NOT NULL AND diagnosed_at < audited_at))"
    ),
    "tailor": (
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(audit_score, fit_score) >= ? "
        "AND full_description IS NOT NULL "
        "AND duplicate_of_url IS NULL "
        "AND tailored_resume_path IS NULL "
        "AND COALESCE(tailor_attempts, 0) < 5"
    ),
    "cover": (
        "SELECT COUNT(*) FROM jobs WHERE COALESCE(audit_score, fit_score) >= ? "
        "AND tailored_resume_path IS NOT NULL "
        "AND duplicate_of_url IS NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '') "
        "AND COALESCE(cover_attempts, 0) < 5"
    ),
    "pdf": (
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL "
        "AND duplicate_of_url IS NULL "
        "AND tailored_resume_path LIKE '%.txt'"
    ),
}

# How long to sleep between polling loops in streaming mode (seconds)
_STREAM_POLL_INTERVAL = 10


def _count_pending(stage: str, min_score: int = 7) -> int:
    """Count pending work items for a stage."""
    sql = _PENDING_SQL.get(stage)
    if sql is None:
        return 0
    conn = get_connection()
    if "?" in sql:
        return conn.execute(sql, (min_score,)).fetchone()[0]
    return conn.execute(sql).fetchone()[0]


def _run_stage_streaming(
    stage: str,
    tracker: _StageTracker,
    stop_event: threading.Event,
    min_score: int = 7,
    batch_size: int | None = None,
    workers: int = 1,
    validation_mode: str = "normal",
    discover_mode: str = "safe",
    run_id: int | None = None,
) -> None:
    """Run a single stage in streaming mode: loop until upstream done + no work.

    For discover: runs once, then marks done.
    For all others: polls DB for pending work, runs the batch processor,
    and repeats until upstream is done and no pending work remains.
    """
    runner = _STAGE_RUNNERS[stage]
    conn = get_connection() if run_id is not None else None
    pending_before = _count_pending(stage, min_score) if run_id is not None else None
    stage_run_id = (
        start_pipeline_stage(conn, run_id, stage, pending_before=pending_before)
        if run_id is not None
        else None
    )
    stage_start = time.time()
    stage_status = "ok"
    stage_error: str | None = None
    kwargs: dict = {}
    if stage in ("tailor", "cover"):
        kwargs["min_score"] = min_score
        kwargs["validation_mode"] = validation_mode
        kwargs["batch_size"] = batch_size
    if stage in ("diagnose", "pdf"):
        kwargs["batch_size"] = batch_size
    if stage in ("discover", "enrich", "score") and _accepts_kwarg(runner, "workers"):
        kwargs["workers"] = workers
    if stage == "discover" and _accepts_kwarg(runner, "discover_mode"):
        kwargs["discover_mode"] = discover_mode

    upstream = _UPSTREAM[stage]

    if stage == "discover":
        # Discover runs once (its sub-scrapers already do their full crawl)
        try:
            result = runner(**kwargs)
            if isinstance(result, dict):
                sub_errors = [
                    f"{k}: {v}" for k, v in result.items()
                    if isinstance(v, str) and v.startswith("error")
                ]
                if sub_errors:
                    stage_status = "partial"
                    stage_error = "; ".join(sub_errors)
            tracker.mark_done(stage, result)
        except Exception as e:
            log.exception("Stage '%s' crashed", stage)
            stage_status = f"error: {e}"
            stage_error = str(e)
            tracker.mark_done(stage, {"status": stage_status})
        finally:
            if stage_run_id is not None:
                finish_pipeline_stage(
                    conn,
                    stage_run_id,
                    status=stage_status,
                    pending_after=_count_pending(stage, min_score),
                    elapsed_seconds=time.time() - stage_start,
                    error=stage_error,
                )
        return

    # For downstream stages: loop until upstream done + no pending work
    passes = 0
    errors: list[str] = []
    while not stop_event.is_set():
        # Wait for upstream to start producing work (first pass only)
        if passes == 0 and upstream and not tracker.is_done(upstream):
            # Wait a bit for upstream to produce some work before first run
            tracker.wait(upstream, timeout=_STREAM_POLL_INTERVAL)

        pending_before = _count_pending(stage, min_score)
        upstream_done = upstream is None or tracker.is_done(upstream)

        if pending_before <= 0:
            # No work right now.
            if upstream_done:
                # No work and upstream is done — this stage is finished.
                break
            # Upstream still running, wait and retry.
            if stop_event.wait(timeout=_STREAM_POLL_INTERVAL):
                break  # Stop requested
            continue

        # There is pending work — run one pass.
        try:
            result = runner(**kwargs)
            if isinstance(result, dict):
                status = result.get("status", "ok")
                if status not in ("ok", "partial"):
                    errors.append(status)
                elif status == "partial":
                    errors.append("partial")
            passes += 1
        except Exception as e:
            log.error("Stage '%s' error (pass %d): %s", stage, passes, e)
            errors.append(str(e))
            passes += 1

        # No-progress guard. The pending counter can include rows the runner
        # intentionally skips (e.g. enrich skips SKIP_DETAIL_SITES that this
        # SQL still counts). Without this guard, pending stays > 0 forever, the
        # loop never reaches the sleeping branch, and the stage spins at 100%
        # CPU and never terminates. If a pass fails to reduce the backlog, back
        # off — and if upstream is already done, stop rather than loop forever.
        pending_after = _count_pending(stage, min_score)
        if pending_after >= pending_before:
            if upstream_done:
                break
            if stop_event.wait(timeout=_STREAM_POLL_INTERVAL):
                break

    if errors:
        stage_status = "partial"
        stage_error = "; ".join(errors[-3:])
    tracker.mark_done(stage, {"status": stage_status, "passes": passes})
    if stage_run_id is not None:
        finish_pipeline_stage(
            conn,
            stage_run_id,
            status=stage_status,
            pending_after=_count_pending(stage, min_score),
            elapsed_seconds=time.time() - stage_start,
            error=stage_error,
        )


# ---------------------------------------------------------------------------
# Pipeline orchestrators
# ---------------------------------------------------------------------------

def _run_sequential(
    ordered: list[str],
    min_score: int,
    batch_size: int | None = None,
    workers: int = 1,
    validation_mode: str = "normal",
    discover_mode: str = "safe",
    run_id: int | None = None,
) -> dict:
    """Execute stages one at a time (original behavior)."""
    results: list[dict] = []
    errors: dict[str, str] = {}
    pipeline_start = time.time()
    checkpoint_conn = get_connection() if run_id is not None else None

    for name in ordered:
        meta = STAGE_META[name]
        console.print(f"\n{'=' * 70}")
        console.print(f"  [bold]STAGE: {name}[/bold] — {meta['desc']}")
        console.print(f"  Started: {datetime.now().strftime('%H:%M:%S')}")
        console.print(f"{'=' * 70}")

        t0 = time.time()
        runner = _STAGE_RUNNERS[name]
        pending_before = _count_pending(name, min_score) if run_id is not None else None
        stage_run_id = (
            start_pipeline_stage(checkpoint_conn, run_id, name, pending_before=pending_before)
            if run_id is not None
            else None
        )
        status = "ok"
        error_text: str | None = None

        try:
            kwargs: dict = {}
            if name in ("tailor", "cover"):
                kwargs["min_score"] = min_score
                kwargs["validation_mode"] = validation_mode
                kwargs["batch_size"] = batch_size
            if name in ("diagnose", "pdf"):
                kwargs["batch_size"] = batch_size
            if name in ("discover", "enrich", "score") and _accepts_kwarg(runner, "workers"):
                kwargs["workers"] = workers
            if name == "discover" and _accepts_kwarg(runner, "discover_mode"):
                kwargs["discover_mode"] = discover_mode
            result = runner(**kwargs)
            elapsed = time.time() - t0

            if isinstance(result, dict):
                status = result.get("status", "ok")
                if name == "discover":
                    sub_errors = [
                        f"{k}: {v}" for k, v in result.items()
                        if isinstance(v, str) and v.startswith("error")
                    ]
                    if sub_errors:
                        status = "partial"
                        error_text = "; ".join(sub_errors)
                elif status not in ("ok", "partial"):
                    error_text = status

        except Exception as e:
            elapsed = time.time() - t0
            status = f"error: {e}"
            error_text = str(e)
            log.exception("Stage '%s' crashed", name)
            console.print(f"\n  [red]STAGE FAILED:[/red] {e}")
        finally:
            if stage_run_id is not None:
                finish_pipeline_stage(
                    checkpoint_conn,
                    stage_run_id,
                    status=status,
                    pending_after=_count_pending(name, min_score),
                    elapsed_seconds=time.time() - t0,
                    error=error_text,
                )

        results.append({"stage": name, "status": status, "elapsed": elapsed})
        if status not in ("ok", "partial"):
            errors[name] = status

        console.print(f"\n  Stage '{name}' completed in {elapsed:.1f}s — {status}")

    total_elapsed = time.time() - pipeline_start
    return {"stages": results, "errors": errors, "elapsed": total_elapsed}


def _run_streaming(
    ordered: list[str],
    min_score: int,
    batch_size: int | None = None,
    workers: int = 1,
    validation_mode: str = "normal",
    discover_mode: str = "safe",
    run_id: int | None = None,
) -> dict:
    """Execute stages concurrently with DB as conveyor belt."""
    tracker = _StageTracker()
    stop_event = threading.Event()
    pipeline_start = time.time()

    console.print("\n  [bold cyan]STREAMING MODE[/bold cyan] — stages run concurrently")
    console.print(f"  Poll interval: {_STREAM_POLL_INTERVAL}s\n")

    # Mark stages NOT in `ordered` as done so downstream doesn't wait for them
    for stage in STAGE_ORDER:
        if stage not in ordered:
            tracker.mark_done(stage, {"status": "skipped"})

    # Launch each stage in its own thread
    threads: dict[str, threading.Thread] = {}
    start_times: dict[str, float] = {}

    for name in ordered:
        start_times[name] = time.time()
        t = threading.Thread(
            target=_run_stage_streaming,
            args=(name, tracker, stop_event, min_score, batch_size, workers, validation_mode, discover_mode, run_id),
            name=f"stage-{name}",
            daemon=True,
        )
        threads[name] = t
        t.start()
        console.print(f"  [dim]Started thread:[/dim] {name}")

    # Wait for all threads to finish
    try:
        for name in ordered:
            threads[name].join()
            elapsed = time.time() - start_times[name]
            console.print(
                f"  [green]Completed:[/green] {name} ({elapsed:.1f}s)"
            )
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted — stopping stages...[/yellow]")
        stop_event.set()
        for t in threads.values():
            t.join(timeout=10)

    total_elapsed = time.time() - pipeline_start

    # Build results from tracker
    all_results = tracker.get_results()
    results: list[dict] = []
    errors: dict[str, str] = {}

    for name in ordered:
        r = all_results.get(name, {"status": "unknown"})
        elapsed = time.time() - start_times.get(name, pipeline_start)
        status = r.get("status", "ok")

        results.append({"stage": name, "status": status, "elapsed": elapsed})
        if status not in ("ok", "partial", "skipped"):
            errors[name] = status

    return {"stages": results, "errors": errors, "elapsed": total_elapsed}


def run_pipeline(
    stages: list[str] | None = None,
    min_score: int = 7,
    batch_size: int | None = None,
    dry_run: bool = False,
    stream: bool = False,
    workers: int = 1,
    validation_mode: str = "normal",
    discover_mode: str = "safe",
) -> dict:
    """Run pipeline stages.

    Args:
        stages: List of stage names, or None / ["all"] for full pipeline.
        min_score: Minimum fit score for tailor/cover stages.
        batch_size: Maximum jobs per tailor/cover/pdf stage run. 0 means all.
        dry_run: If True, preview stages without executing.
        stream: If True, run stages concurrently (streaming mode).
        workers: Number of parallel threads for discovery/enrichment stages.
        discover_mode: Discovery breadth mode: safe, fast, or full.

    Returns:
        Dict with keys: stages (list of result dicts), errors (dict), elapsed (float).
    """
    # Bootstrap
    load_env()
    ensure_dirs()
    conn = init_db()

    # Resolve stages
    if stages is None:
        stages = ["all"]
    ordered = _resolve_stages(stages)

    # Banner
    mode = "streaming" if stream else "sequential"
    effective_batch_size = DEFAULTS["generation_batch_size"] if batch_size is None else batch_size
    console.print()
    console.print(Panel.fit(
        f"[bold]ApplyPilot Pipeline[/bold] ({mode})",
        border_style="blue",
    ))
    console.print(f"  Min score:  {min_score}")
    console.print(
        f"  Batch size: {'all' if effective_batch_size == 0 else effective_batch_size}"
    )
    console.print(f"  Workers:    {workers}")
    console.print(f"  Discovery:  {discover_mode}")
    console.print(f"  Validation: {validation_mode}")
    console.print(f"  Stages:     {' -> '.join(ordered)}")

    # Pre-run stats
    pre_stats = get_stats()
    console.print(f"  DB:        {pre_stats['total']} jobs, {pre_stats['pending_detail']} pending enrichment")

    if dry_run:
        console.print(f"\n  [yellow]DRY RUN[/yellow] — would execute ({mode}):")
        for name in ordered:
            meta = STAGE_META[name]
            console.print(f"    {name:<12s}  {meta['desc']}")
        console.print("\n  No changes made.")
        return {"stages": [], "errors": {}, "elapsed": 0.0}

    # Execute
    run_id = create_pipeline_run(
        conn,
        stages=ordered,
        mode=mode,
        min_score=min_score,
        batch_size=effective_batch_size,
        workers=workers,
        validation_mode=validation_mode,
    )
    try:
        if stream:
            result = _run_streaming(ordered, min_score, batch_size=effective_batch_size, workers=workers,
                                    validation_mode=validation_mode, discover_mode=discover_mode, run_id=run_id)
        else:
            result = _run_sequential(ordered, min_score, batch_size=effective_batch_size, workers=workers,
                                     validation_mode=validation_mode, discover_mode=discover_mode, run_id=run_id)
        stage_statuses = [stage_result.get("status") for stage_result in result.get("stages", [])]
        run_status = "partial" if result.get("errors") or any(s == "partial" for s in stage_statuses) else "ok"
        run_error = "; ".join(f"{stage}: {error}" for stage, error in result.get("errors", {}).items()) or None
        finish_pipeline_run(conn, run_id, status=run_status, error=run_error)
    except Exception as e:
        finish_pipeline_run(conn, run_id, status="error", error=str(e))
        raise

    # Summary table
    console.print(f"\n{'=' * 70}")
    summary = Table(title="Pipeline Summary", show_header=True, header_style="bold")
    summary.add_column("Stage", style="bold")
    summary.add_column("Status")
    summary.add_column("Time", justify="right")

    for r in result["stages"]:
        elapsed_str = f"{r['elapsed']:.1f}s"
        status_display = r["status"][:30]
        if r["status"] == "ok":
            style = "green"
        elif r["status"] in ("partial", "skipped"):
            style = "yellow"
        else:
            style = "red"
        summary.add_row(r["stage"], f"[{style}]{status_display}[/{style}]", elapsed_str)

    summary.add_row("", "", "")
    summary.add_row("[bold]Total[/bold]", "", f"[bold]{result['elapsed']:.1f}s[/bold]")
    console.print(summary)

    # Final DB stats
    final = get_stats()
    console.print("\n  [bold]DB Final State:[/bold]")
    console.print(f"    Total jobs:     {final['total']}")
    console.print(f"    Duplicates:     {final.get('duplicates', 0)}")
    console.print(f"    With desc:      {final['with_description']}")
    console.print(f"    Scored:         {final['scored']}")
    console.print(f"    Audited:        {final.get('audited', 0)}")
    console.print(f"    Diagnosed:      {final.get('diagnosed', 0)}")
    console.print(f"    Tailored:       {final['tailored']}")
    console.print(f"    Cover letters:  {final['with_cover_letter']}")
    console.print(f"    Ready to apply: {final['ready_to_apply']}")
    console.print(f"    Applied:        {final['applied']}")
    console.print(f"{'=' * 70}\n")

    return result
