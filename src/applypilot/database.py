"""ApplyPilot database layer: schema, migrations, stats, and connection helpers.

Single source of truth for the jobs table schema. All columns from every
pipeline stage are created up front so any stage can run independently
without migration ordering issues.
"""

import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from applypilot.config import DB_PATH

# Thread-local connection storage — each thread gets its own connection
# (required for SQLite thread safety with parallel workers)
_local = threading.local()


def get_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Get a thread-local cached SQLite connection with WAL mode enabled.

    Each thread gets its own connection (required for SQLite thread safety).
    Connections are cached and reused within the same thread.

    Args:
        db_path: Override the default DB_PATH. Useful for testing.

    Returns:
        sqlite3.Connection configured with WAL mode and row factory.
    """
    path = str(db_path or DB_PATH)

    if not hasattr(_local, 'connections'):
        _local.connections = {}

    conn = _local.connections.get(path)
    if conn is not None:
        try:
            conn.execute("SELECT 1")
            return conn
        except sqlite3.ProgrammingError:
            pass

    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.row_factory = sqlite3.Row
    _local.connections[path] = conn
    return conn


def close_connection(db_path: Path | str | None = None) -> None:
    """Close the cached connection for the current thread."""
    path = str(db_path or DB_PATH)
    if hasattr(_local, 'connections'):
        conn = _local.connections.pop(path, None)
        if conn is not None:
            conn.close()


def init_db(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Create the full jobs table with all columns from every pipeline stage.

    This is idempotent -- safe to call on every startup. Uses CREATE TABLE IF NOT EXISTS
    so it won't destroy existing data.

    Schema columns by stage:
      - Discovery:  url, title, salary, description, location, site, strategy, discovered_at
      - Enrichment: full_description, application_url, detail_scraped_at, detail_error
      - Scoring:    fit_score, score_reasoning, score_model, score_provider, scored_at
      - Diagnosis:  fit_gap_category, fit_gap_severity, resume_fixability,
                    recommended_action, fit_diagnosis, fit_diagnosis_json,
                    diagnosis_model, diagnosis_provider, diagnosed_at
      - Tailoring:  tailored_resume_path, tailored_at, tailor_attempts
      - Cover:      cover_letter_path, cover_letter_at, cover_attempts
      - Apply:      applied_at, apply_status, apply_error, apply_attempts,
                   agent_id, last_attempted_at, apply_duration_ms, apply_task_id,
                   verification_confidence

    Args:
        db_path: Override the default DB_PATH.

    Returns:
        sqlite3.Connection with the schema initialized.
    """
    path = db_path or DB_PATH

    # Ensure parent directory exists
    Path(path).parent.mkdir(parents=True, exist_ok=True)

    conn = get_connection(path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS jobs (
            -- Discovery stage (smart_extract / job_search)
            url                   TEXT PRIMARY KEY,
            title                 TEXT,
            salary                TEXT,
            description           TEXT,
            location              TEXT,
            site                  TEXT,
            company               TEXT,
            source_board          TEXT,
            strategy              TEXT,
            discovered_at         TEXT,
            dedupe_key            TEXT,
            duplicate_of_url      TEXT,
            duplicate_reason      TEXT,
            duplicate_detected_at TEXT,

            -- Enrichment stage (detail_scraper)
            full_description      TEXT,
            application_url       TEXT,
            detail_scraped_at     TEXT,
            detail_error          TEXT,

            -- Scoring stage (job_scorer)
            fit_score             INTEGER,
            score_reasoning       TEXT,
            fit_verdict           TEXT,
            score_error           TEXT,
            score_error_at        TEXT,
            score_attempts        INTEGER DEFAULT 0,
            score_model           TEXT,
            score_provider        TEXT,
            scored_at             TEXT,
            role_fit_score        INTEGER,
            audit_score           REAL,
            audit_label           TEXT,
            audit_flags           TEXT,
            audit_reason          TEXT,
            audited_at            TEXT,

            -- Diagnosis stage (fit diagnosis / resume gap analysis)
            fit_gap_category      TEXT,
            fit_gap_severity      TEXT,
            resume_fixability     TEXT,
            recommended_action    TEXT,
            fit_diagnosis         TEXT,
            fit_diagnosis_json    TEXT,
            diagnosis_model       TEXT,
            diagnosis_provider    TEXT,
            diagnosed_at          TEXT,

            -- Tailoring stage (resume tailor)
            tailored_resume_path  TEXT,
            tailored_at           TEXT,
            tailor_attempts       INTEGER DEFAULT 0,

            -- Cover letter stage
            cover_letter_path     TEXT,
            cover_letter_at       TEXT,
            cover_attempts        INTEGER DEFAULT 0,

            -- Application stage
            applied_at            TEXT,
            apply_status          TEXT,
            apply_error           TEXT,
            apply_attempts        INTEGER DEFAULT 0,
            agent_id              TEXT,
            last_attempted_at     TEXT,
            apply_duration_ms     INTEGER,
            apply_task_id         TEXT,
            verification_confidence TEXT
        )
    """)
    conn.commit()

    # Run migrations for any columns added after initial schema
    ensure_columns(conn)
    backfill_company_source_fields(conn)
    repair_retryable_score_errors(conn)
    ensure_job_indexes(conn)
    ensure_application_tables(conn)
    ensure_pipeline_tables(conn)

    return conn


# Complete column registry: column_name -> SQL type with optional default.
# This is the single source of truth. Adding a column here is all that's needed
# for it to appear in both new databases and migrated ones.
_ALL_COLUMNS: dict[str, str] = {
    # Discovery
    "url": "TEXT PRIMARY KEY",
    "title": "TEXT",
    "salary": "TEXT",
    "description": "TEXT",
    "location": "TEXT",
    "site": "TEXT",
    "company": "TEXT",
    "source_board": "TEXT",
    "strategy": "TEXT",
    "discovered_at": "TEXT",
    "dedupe_key": "TEXT",
    "duplicate_of_url": "TEXT",
    "duplicate_reason": "TEXT",
    "duplicate_detected_at": "TEXT",
    # Enrichment
    "full_description": "TEXT",
    "application_url": "TEXT",
    "detail_scraped_at": "TEXT",
    "detail_error": "TEXT",
    "detail_attempts": "INTEGER DEFAULT 0",
    # Scoring
    "fit_score": "INTEGER",
    "score_reasoning": "TEXT",
    "fit_verdict": "TEXT",
    "score_error": "TEXT",
    "score_error_at": "TEXT",
    "score_attempts": "INTEGER DEFAULT 0",
    "score_model": "TEXT",
    "score_provider": "TEXT",
    "scored_at": "TEXT",
    # Score audit / reranking
    "role_fit_score": "INTEGER",
    "audit_score": "REAL",
    "audit_label": "TEXT",
    "audit_flags": "TEXT",
    "audit_reason": "TEXT",
    "audited_at": "TEXT",
    # External recommendation-engine decision ("brainstorm"). When present, the
    # decision is authoritative for the apply gate (written into audit_score),
    # ApplyPilot's own audit stage skips the row, and its LLM fit_score is kept
    # as a benchmark datapoint to compare against external_decision_score.
    "decision_source": "TEXT",
    "decision_verdict": "TEXT",
    "external_decision_score": "REAL",
    "decision_at": "TEXT",
    # Fit diagnosis / gap analysis
    "fit_gap_category": "TEXT",
    "fit_gap_severity": "TEXT",
    "resume_fixability": "TEXT",
    "recommended_action": "TEXT",
    "fit_diagnosis": "TEXT",
    "fit_diagnosis_json": "TEXT",
    "diagnosis_model": "TEXT",
    "diagnosis_provider": "TEXT",
    "diagnosed_at": "TEXT",
    # Tailoring
    "tailored_resume_path": "TEXT",
    "tailored_at": "TEXT",
    "tailor_attempts": "INTEGER DEFAULT 0",
    # Cover letter
    "cover_letter_path": "TEXT",
    "cover_letter_at": "TEXT",
    "cover_attempts": "INTEGER DEFAULT 0",
    # Application
    "applied_at": "TEXT",
    "apply_status": "TEXT",
    "apply_error": "TEXT",
    "apply_attempts": "INTEGER DEFAULT 0",
    "agent_id": "TEXT",
    "last_attempted_at": "TEXT",
    "apply_duration_ms": "INTEGER",
    "apply_task_id": "TEXT",
    "verification_confidence": "TEXT",
    # Pre-apply posting-liveness gate (additive; NEVER deletes rows — dead jobs
    # stay in the DB with full JD/scores + this close-marker, for future training)
    "liveness_status": "TEXT",        # live | dead | uncertain (NULL = unchecked)
    "last_verified_live": "TEXT",     # ISO8601 timestamp of last liveness probe
    "liveness_reason": "TEXT",        # signal that produced the verdict
}


def ensure_columns(conn: sqlite3.Connection | None = None) -> list[str]:
    """Add any missing columns to the jobs table (forward migration).

    Reads the current table schema via PRAGMA table_info and compares against
    the full column registry. Any missing columns are added with ALTER TABLE.

    This makes it safe to upgrade the database from any previous version --
    columns are only added, never removed or renamed.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        List of column names that were added (empty if schema was already current).
    """
    if conn is None:
        conn = get_connection()

    existing = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    added = []

    for col, dtype in _ALL_COLUMNS.items():
        if col not in existing:
            # PRIMARY KEY columns can't be added via ALTER TABLE, but url
            # is always created with the table itself so this is safe
            if "PRIMARY KEY" in dtype:
                continue
            conn.execute(f"ALTER TABLE jobs ADD COLUMN {col} {dtype}")
            added.append(col)

    if added:
        conn.commit()

    return added


def backfill_company_source_fields(conn: sqlite3.Connection | None = None) -> None:
    """Populate company/source_board for databases created before those fields existed."""
    if conn is None:
        conn = get_connection()

    board_names = (
        "indeed",
        "linkedin",
        "zip_recruiter",
        "glassdoor",
        "the_muse",
        "remotejobs_org",
        "remotive",
        "arbeitnow",
    )
    placeholders = ",".join("?" for _ in board_names)
    conn.execute(
        f"""
        UPDATE jobs
           SET source_board = COALESCE(source_board, site)
         WHERE source_board IS NULL
           AND lower(COALESCE(site, '')) IN ({placeholders})
        """,
        board_names,
    )
    conn.execute(
        f"""
        UPDATE jobs
           SET company = COALESCE(company, site)
         WHERE company IS NULL
           AND lower(COALESCE(site, '')) NOT IN ({placeholders})
        """,
        board_names,
    )
    conn.execute(
        """
        UPDATE jobs
           SET source_board = COALESCE(source_board, strategy, site)
         WHERE source_board IS NULL
        """
    )
    conn.commit()


def repair_retryable_score_errors(conn: sqlite3.Connection | None = None) -> int:
    """Move legacy LLM error scores back to pending scoring.

    Older runs persisted provider failures as fit_score=0, which made them look
    completed. Those rows should remain retryable and keep the failure note.
    """
    if conn is None:
        conn = get_connection()

    cursor = conn.execute(
        """
        UPDATE jobs
           SET score_error = COALESCE(score_error, score_reasoning),
               score_error_at = COALESCE(score_error_at, scored_at),
               fit_score = NULL,
               scored_at = NULL
         WHERE fit_score = 0
           AND score_reasoning LIKE '%LLM error:%'
        """
    )
    repaired = int(cursor.rowcount or 0)
    conn.execute(
        """
        UPDATE jobs
           SET role_fit_score = NULL,
               audit_score = NULL,
               audit_label = NULL,
               audit_flags = NULL,
               audit_reason = NULL,
               audited_at = NULL,
               fit_gap_category = NULL,
               fit_gap_severity = NULL,
               resume_fixability = NULL,
               recommended_action = NULL,
               fit_diagnosis = NULL,
               fit_diagnosis_json = NULL,
               diagnosis_model = NULL,
               diagnosis_provider = NULL,
               diagnosed_at = NULL
         WHERE fit_score IS NULL
           AND score_error IS NOT NULL
        """
    )
    conn.commit()
    return repaired


def ensure_job_indexes(conn: sqlite3.Connection | None = None) -> None:
    """Create indexes used by status and resumable pipeline stages."""
    if conn is None:
        conn = get_connection()

    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_source_board ON jobs(source_board)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_discovered_at ON jobs(discovered_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dedupe_key ON jobs(dedupe_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_duplicate_of_url ON jobs(duplicate_of_url)")
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_pending_enrich
            ON jobs(site, discovered_at)
         WHERE detail_scraped_at IS NULL AND duplicate_of_url IS NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_pending_score
            ON jobs(discovered_at)
         WHERE full_description IS NOT NULL AND fit_score IS NULL AND duplicate_of_url IS NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_pending_tailor
            ON jobs(COALESCE(audit_score, fit_score), discovered_at)
         WHERE full_description IS NOT NULL
           AND tailored_resume_path IS NULL
           AND COALESCE(tailor_attempts, 0) < 5
           AND duplicate_of_url IS NULL
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_jobs_ready_to_apply
            ON jobs(COALESCE(audit_score, fit_score), discovered_at)
         WHERE tailored_resume_path IS NOT NULL AND applied_at IS NULL AND duplicate_of_url IS NULL
        """
    )
    conn.commit()


def ensure_application_tables(conn: sqlite3.Connection | None = None) -> None:
    """Create first-class application tracking tables.

    jobs.applied_at is the quick pipeline flag. These tables are the durable
    tracker: current application status plus an append-only status history.
    """
    if conn is None:
        conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS applications (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url               TEXT NOT NULL UNIQUE,
            title                 TEXT,
            company               TEXT,
            source                TEXT,
            source_url            TEXT,
            application_url       TEXT,
            status                TEXT NOT NULL DEFAULT 'shortlisted',
            channel               TEXT,
            applied_at            TEXT,
            last_status_at        TEXT,
            next_follow_up_at     TEXT,
            resume_path           TEXT,
            cover_letter_path     TEXT,
            contact_name          TEXT,
            contact_email         TEXT,
            contact_url           TEXT,
            notes                 TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            FOREIGN KEY(job_url) REFERENCES jobs(url)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_events (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url               TEXT NOT NULL,
            status                TEXT NOT NULL,
            event_type            TEXT NOT NULL,
            channel               TEXT,
            happened_at           TEXT NOT NULL,
            notes                 TEXT,
            FOREIGN KEY(job_url) REFERENCES jobs(url)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_status ON applications(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_applied_at ON applications(applied_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_applications_followup ON applications(next_follow_up_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_events_job_url ON application_events(job_url)")

    # Backfill current applied jobs into the tracker once, preserving dates.
    rows = conn.execute("""
        SELECT j.*
        FROM jobs j
        LEFT JOIN applications a ON a.job_url = j.url
        WHERE j.applied_at IS NOT NULL AND a.job_url IS NULL
    """).fetchall()
    for row in rows:
        applied_at = row["applied_at"]
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("""
            INSERT INTO applications (
                job_url, title, company, source, source_url, application_url,
                status, channel, applied_at, last_status_at, resume_path,
                cover_letter_path, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 'applied', 'legacy', ?, ?, ?, ?, ?, ?)
        """, (
            row["url"], row["title"], row["company"] or row["site"], row["source_board"] or row["site"], row["url"],
            row["application_url"], applied_at, applied_at,
            row["tailored_resume_path"], row["cover_letter_path"], now, now,
        ))
        conn.execute("""
            INSERT INTO application_events (
                job_url, status, event_type, channel, happened_at, notes
            )
            VALUES (?, 'applied', 'backfill', 'legacy', ?, 'Backfilled from jobs.applied_at')
        """, (row["url"], applied_at))
    conn.commit()


def ensure_pipeline_tables(conn: sqlite3.Connection | None = None) -> None:
    """Create durable pipeline run/checkpoint tables."""
    if conn is None:
        conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_runs (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            stages                TEXT NOT NULL,
            mode                  TEXT NOT NULL,
            status                TEXT NOT NULL,
            min_score             INTEGER,
            batch_size            INTEGER,
            workers               INTEGER,
            validation_mode       TEXT,
            started_at            TEXT NOT NULL,
            ended_at              TEXT,
            error                 TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pipeline_stage_runs (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id                INTEGER NOT NULL,
            stage                 TEXT NOT NULL,
            status                TEXT NOT NULL,
            pending_before        INTEGER,
            pending_after         INTEGER,
            elapsed_seconds       REAL,
            started_at            TEXT NOT NULL,
            ended_at              TEXT,
            error                 TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            FOREIGN KEY(run_id) REFERENCES pipeline_runs(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llm_usage (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            stage                 TEXT,
            model                 TEXT,
            provider              TEXT,
            prompt_tokens         INTEGER DEFAULT 0,
            completion_tokens     INTEGER DEFAULT 0,
            thinking_tokens       INTEGER DEFAULT 0,
            total_tokens          INTEGER DEFAULT 0,
            est_cost_usd          REAL,
            prompt_variant        TEXT,
            created_at            TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_runs_started_at ON pipeline_runs(started_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status ON pipeline_runs(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_stage_runs_run_id ON pipeline_stage_runs(run_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_stage_runs_stage ON pipeline_stage_runs(stage)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_stage ON llm_usage(stage)")

    # Forward-migrate prompt_variant into existing llm_usage tables BEFORE
    # indexing it: an older DB's llm_usage predates this column, so creating the
    # index first throws "no such column: prompt_variant" and aborts init_db.
    existing_llm_cols = {row[1] for row in conn.execute("PRAGMA table_info(llm_usage)").fetchall()}
    if "prompt_variant" not in existing_llm_cols:
        conn.execute("ALTER TABLE llm_usage ADD COLUMN prompt_variant TEXT")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_llm_usage_variant ON llm_usage(prompt_variant)")

    conn.commit()


def record_llm_usage(
    stage: str | None,
    model: str | None,
    provider: str | None,
    usage: dict | None,
    est_cost_usd: float | None = None,
    conn: sqlite3.Connection | None = None,
    prompt_variant: str | None = None,
) -> None:
    """Persist one LLM call's token usage (best-effort; never raises).

    `usage` is the LLMClient.last_usage dict (prompt/completion/thinking/total).
    `prompt_variant` identifies which A/B prompt template was used (MAB tracking).
    """
    if not usage:
        return
    if conn is None:
        conn = get_connection()
    try:
        conn.execute(
            """
            INSERT INTO llm_usage (stage, model, provider, prompt_tokens,
                completion_tokens, thinking_tokens, total_tokens, est_cost_usd,
                prompt_variant, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                stage, model, provider,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("thinking_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                est_cost_usd,
                prompt_variant,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    except Exception:
        pass  # usage logging must never break a real LLM call


def get_llm_usage_summary(conn: sqlite3.Connection | None = None) -> dict:
    """Aggregate LLM token usage and estimated cost by stage, model, and prompt variant."""
    if conn is None:
        conn = get_connection()
    by_stage = [
        dict(zip(("stage", "calls", "prompt", "completion", "thinking", "total", "cost"), row))
        for row in conn.execute(
            """
            SELECT COALESCE(stage, '?'), COUNT(*), SUM(prompt_tokens), SUM(completion_tokens),
                   SUM(thinking_tokens), SUM(total_tokens), SUM(est_cost_usd)
              FROM llm_usage GROUP BY stage ORDER BY SUM(total_tokens) DESC
            """
        ).fetchall()
    ]
    by_model = [
        dict(zip(("model", "calls", "total", "cost"), row))
        for row in conn.execute(
            """
            SELECT COALESCE(model, '?'), COUNT(*), SUM(total_tokens), SUM(est_cost_usd)
              FROM llm_usage GROUP BY model ORDER BY SUM(total_tokens) DESC
            """
        ).fetchall()
    ]
    by_variant = [
        dict(zip(("variant", "stage", "calls", "total", "cost"), row))
        for row in conn.execute(
            """
            SELECT COALESCE(prompt_variant, '(default)'), COALESCE(stage, '?'),
                   COUNT(*), SUM(total_tokens), SUM(est_cost_usd)
              FROM llm_usage
             WHERE prompt_variant IS NOT NULL
             GROUP BY prompt_variant, stage
             ORDER BY prompt_variant, SUM(total_tokens) DESC
            """
        ).fetchall()
    ]
    totals = conn.execute(
        "SELECT COUNT(*), SUM(total_tokens), SUM(est_cost_usd) FROM llm_usage"
    ).fetchone()
    return {
        "by_stage": by_stage,
        "by_model": by_model,
        "by_variant": by_variant,
        "total_calls": totals[0] or 0,
        "total_tokens": totals[1] or 0,
        "total_cost_usd": totals[2] or 0.0,
    }


def get_scrape_quality_report(conn: sqlite3.Connection | None = None) -> dict:
    """Per-board null-rate report computed from the live jobs table.

    Null rate = fraction of non-duplicate jobs on that board that are missing
    at least one of: title, full_description, location, company.  High null
    rates indicate selector drift or anti-bot placeholder substitution.

    Implemented as a single GROUP BY query to avoid N+1 database round-trips.
    """
    if conn is None:
        conn = get_connection()

    rows = conn.execute(
        """
        SELECT
            COALESCE(source_board, strategy, site, 'unknown') AS board,
            COUNT(*)                                           AS total,
            SUM(CASE WHEN title IS NULL OR trim(title) = ''                         THEN 1 ELSE 0 END) AS null_title,
            SUM(CASE WHEN full_description IS NULL OR trim(full_description) = ''   THEN 1 ELSE 0 END) AS null_full_description,
            SUM(CASE WHEN location IS NULL OR trim(location) = ''                   THEN 1 ELSE 0 END) AS null_location,
            SUM(CASE WHEN company IS NULL OR trim(company) = ''                     THEN 1 ELSE 0 END) AS null_company,
            SUM(CASE WHEN
                    title IS NULL OR trim(title) = '' OR
                    full_description IS NULL OR trim(full_description) = '' OR
                    location IS NULL OR trim(location) = '' OR
                    company IS NULL OR trim(company) = ''
                THEN 1 ELSE 0 END)                           AS with_any_null
          FROM jobs
         WHERE duplicate_of_url IS NULL
         GROUP BY COALESCE(source_board, strategy, site, 'unknown')
        """
    ).fetchall()

    reports = []
    for row in rows:
        board, total, n_title, n_desc, n_loc, n_co, with_any_null = row
        if not total:
            continue
        reports.append({
            "board": board,
            "total": total,
            "null_rate": round(with_any_null / total, 4),
            "null_counts": {
                "title": n_title or 0,
                "full_description": n_desc or 0,
                "location": n_loc or 0,
                "company": n_co or 0,
            },
        })

    return {
        "boards": sorted(reports, key=lambda r: r["null_rate"], reverse=True),
    }


def get_apply_analytics(conn: sqlite3.Connection | None = None) -> dict:
    """Apply funnel + success-rate analytics from the jobs/applications tables."""
    if conn is None:
        conn = get_connection()

    funnel = {
        (row[0] or "(unattempted)"): row[1]
        for row in conn.execute(
            "SELECT apply_status, COUNT(*) FROM jobs WHERE duplicate_of_url IS NULL GROUP BY apply_status"
        ).fetchall()
    }
    applied = funnel.get("applied", 0)
    terminal_fail = sum(
        n for s, n in funnel.items()
        if s not in ("applied", "in_progress", "(unattempted)", "manual")
    )
    attempted = applied + terminal_fail
    success_rate = (applied / attempted) if attempted else None

    by_site = [
        dict(zip(("site", "applied", "failed"), row))
        for row in conn.execute(
            """
            SELECT site,
                   SUM(CASE WHEN apply_status = 'applied' THEN 1 ELSE 0 END),
                   SUM(CASE WHEN apply_status IS NOT NULL AND apply_status NOT IN
                            ('applied', 'in_progress', 'manual') THEN 1 ELSE 0 END)
              FROM jobs
             WHERE apply_status IS NOT NULL AND apply_status != 'in_progress'
             GROUP BY site
             ORDER BY 2 DESC, 3 DESC
             LIMIT 15
            """
        ).fetchall()
    ]
    fail_reasons = [
        dict(zip(("reason", "count"), row))
        for row in conn.execute(
            """
            SELECT COALESCE(apply_error, 'unknown'), COUNT(*)
              FROM jobs
             WHERE apply_status IS NOT NULL
               AND apply_status NOT IN ('applied', 'in_progress', 'manual')
             GROUP BY apply_error ORDER BY 2 DESC LIMIT 12
            """
        ).fetchall()
    ]
    avg_ms = conn.execute(
        "SELECT AVG(apply_duration_ms) FROM jobs WHERE apply_duration_ms IS NOT NULL"
    ).fetchone()[0]

    # Outcome funnel from the application tracker (interview/offer/rejected/...)
    outcomes: dict[str, int] = {}
    try:
        outcomes = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT status, COUNT(*) FROM applications GROUP BY status"
            ).fetchall()
        }
    except sqlite3.OperationalError:
        pass  # applications table may not exist yet

    return {
        "funnel": funnel,
        "applied": applied,
        "attempted": attempted,
        "success_rate": success_rate,
        "by_site": by_site,
        "fail_reasons": fail_reasons,
        "avg_apply_seconds": (avg_ms / 1000.0) if avg_ms else None,
        "outcomes": outcomes,
    }


_COMPANY_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "inc",
    "incorporated",
    "llc",
    "llp",
    "lp",
    "ltd",
    "limited",
    "plc",
}
_STATE_NAMES = {
    "alabama": "al",
    "alaska": "ak",
    "arizona": "az",
    "arkansas": "ar",
    "california": "ca",
    "colorado": "co",
    "connecticut": "ct",
    "delaware": "de",
    "florida": "fl",
    "georgia": "ga",
    "hawaii": "hi",
    "idaho": "id",
    "illinois": "il",
    "indiana": "in",
    "iowa": "ia",
    "kansas": "ks",
    "kentucky": "ky",
    "louisiana": "la",
    "maine": "me",
    "maryland": "md",
    "massachusetts": "ma",
    "michigan": "mi",
    "minnesota": "mn",
    "mississippi": "ms",
    "missouri": "mo",
    "montana": "mt",
    "nebraska": "ne",
    "nevada": "nv",
    "new hampshire": "nh",
    "new jersey": "nj",
    "new mexico": "nm",
    "new york": "ny",
    "north carolina": "nc",
    "north dakota": "nd",
    "ohio": "oh",
    "oklahoma": "ok",
    "oregon": "or",
    "pennsylvania": "pa",
    "rhode island": "ri",
    "south carolina": "sc",
    "south dakota": "sd",
    "tennessee": "tn",
    "texas": "tx",
    "utah": "ut",
    "vermont": "vt",
    "virginia": "va",
    "washington": "wa",
    "west virginia": "wv",
    "wisconsin": "wi",
    "wyoming": "wy",
}
_LOCATION_ALIASES = {
    "nyc": "new york ny",
    "new york city": "new york ny",
    "new york ny": "new york ny",
    "sf": "san francisco ca",
    "bay area": "san francisco ca",
    "san francisco ca": "san francisco ca",
}
_DUPLICATE_REASON = "same_company_title_location"


def _fold_ascii(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    return text.encode("ascii", "ignore").decode("ascii")


def _normalize_tokens(value: Any) -> list[str]:
    text = _fold_ascii(value)
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return [token for token in text.split() if token]


def _normalize_company(value: Any) -> str:
    tokens = _normalize_tokens(value)
    while tokens and tokens[-1] in _COMPANY_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def _normalize_title(value: Any) -> str:
    return " ".join(_normalize_tokens(value))


def _normalize_location(value: Any) -> str:
    tokens = _normalize_tokens(value)
    if not tokens:
        return "unknown"
    if "remote" in tokens:
        return "remote"

    text = " ".join(token for token in tokens if token not in {"usa", "us", "united", "states"})
    if text in _LOCATION_ALIASES:
        return _LOCATION_ALIASES[text]

    for state_name, abbr in sorted(_STATE_NAMES.items(), key=lambda item: len(item[0]), reverse=True):
        text = re.sub(rf"\b{re.escape(state_name)}\b", abbr, text)
    text = _LOCATION_ALIASES.get(text, text)
    return re.sub(r"\s+", " ", text).strip() or "unknown"


def build_job_dedupe_key(job: Mapping[str, Any]) -> str | None:
    """Build a conservative same-job fingerprint from company, title, and location."""
    title = _normalize_title(job.get("title"))
    company = _normalize_company(job.get("company") or job.get("site"))
    if not title or not company:
        return None
    location = _normalize_location(job.get("location"))
    return f"v1|{company}|{title}|{location}"


def _row_quality(row: Mapping[str, Any]) -> tuple[int, int, str, str]:
    has_application = 0 if row.get("application_url") else 1
    has_description = 0 if row.get("full_description") else 1
    return has_application, has_description, str(row.get("discovered_at") or ""), str(row.get("url") or "")


def _canonical_duplicate(
    conn: sqlite3.Connection,
    dedupe_key: str,
    *,
    exclude_url: str | None = None,
) -> sqlite3.Row | None:
    params: list[Any] = [dedupe_key]
    where = "dedupe_key = ? AND duplicate_of_url IS NULL"
    if exclude_url:
        where += " AND url != ?"
        params.append(exclude_url)
    return conn.execute(
        f"""
        SELECT *
          FROM jobs
         WHERE {where}
         ORDER BY
              CASE WHEN application_url IS NULL OR application_url = '' THEN 1 ELSE 0 END,
              CASE WHEN full_description IS NULL OR full_description = '' THEN 1 ELSE 0 END,
              discovered_at,
              url
         LIMIT 1
        """,
        params,
    ).fetchone()


def insert_discovered_job(
    conn: sqlite3.Connection,
    job: Mapping[str, Any],
    *,
    site: str | None = None,
    strategy: str | None = None,
    source_board: str | None = None,
    discovered_at: str | None = None,
) -> str:
    """Insert one discovered job and mark same-job duplicates non-destructively.

    Returns one of: "new", "duplicate", "existing", or "skipped".
    """
    url = str(job.get("url") or "").strip()
    if not url:
        return "skipped"

    now = discovered_at or datetime.now(timezone.utc).isoformat()
    site_value = site or job.get("site") or job.get("company")
    company = job.get("company") or site_value
    board = source_board or job.get("source_board") or site_value
    dedupe_key = build_job_dedupe_key({**dict(job), "site": site_value, "company": company})
    canonical = _canonical_duplicate(conn, dedupe_key, exclude_url=url) if dedupe_key else None
    duplicate_of_url = canonical["url"] if canonical else None
    duplicate_reason = _DUPLICATE_REASON if canonical else None
    duplicate_detected_at = now if canonical else None

    try:
        conn.execute(
            """
            INSERT INTO jobs (
                url, title, salary, description, location, site,
                company, source_board, strategy, discovered_at,
                dedupe_key, duplicate_of_url, duplicate_reason, duplicate_detected_at,
                full_description, application_url, detail_scraped_at, detail_error
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                url,
                job.get("title"),
                job.get("salary"),
                job.get("description"),
                job.get("location"),
                site_value,
                company,
                board,
                strategy or job.get("strategy"),
                now,
                dedupe_key,
                duplicate_of_url,
                duplicate_reason,
                duplicate_detected_at,
                job.get("full_description"),
                job.get("application_url"),
                job.get("detail_scraped_at"),
                job.get("detail_error"),
            ),
        )
    except sqlite3.IntegrityError:
        if dedupe_key:
            conn.execute(
                "UPDATE jobs SET dedupe_key = COALESCE(dedupe_key, ?) WHERE url = ?",
                (dedupe_key, url),
            )
        return "existing"

    return "duplicate" if duplicate_of_url else "new"


def dedupe_existing_jobs(conn: sqlite3.Connection | None = None, *, dry_run: bool = False) -> dict[str, int | bool]:
    """Recompute dedupe keys and mark same-job duplicates already in the DB."""
    if conn is None:
        conn = get_connection()

    rows = [dict(row) for row in conn.execute("SELECT * FROM jobs ORDER BY discovered_at, url").fetchall()]
    desired: dict[str, dict[str, str | None]] = {}
    groups: dict[str, list[dict[str, Any]]] = {}

    for row in rows:
        key = build_job_dedupe_key(row)
        desired[row["url"]] = {
            "dedupe_key": key,
            "duplicate_of_url": None,
            "duplicate_reason": None,
            "duplicate_detected_at": None,
        }
        if key:
            groups.setdefault(key, []).append(row)

    duplicate_groups = 0
    duplicate_count = 0
    now = datetime.now(timezone.utc).isoformat()

    for key, group in groups.items():
        if len(group) <= 1:
            continue
        duplicate_groups += 1
        canonical = sorted(group, key=_row_quality)[0]
        for row in group:
            if row["url"] == canonical["url"]:
                continue
            desired[row["url"]] = {
                "dedupe_key": key,
                "duplicate_of_url": canonical["url"],
                "duplicate_reason": _DUPLICATE_REASON,
                "duplicate_detected_at": row.get("duplicate_detected_at") or now,
            }
            duplicate_count += 1

    if not dry_run:
        for url, values in desired.items():
            conn.execute(
                """
                UPDATE jobs
                   SET dedupe_key = ?,
                       duplicate_of_url = ?,
                       duplicate_reason = ?,
                       duplicate_detected_at = ?
                 WHERE url = ?
                """,
                (
                    values["dedupe_key"],
                    values["duplicate_of_url"],
                    values["duplicate_reason"],
                    values["duplicate_detected_at"],
                    url,
                ),
            )
        conn.commit()

    return {
        "processed": len(rows),
        "keys": sum(1 for values in desired.values() if values["dedupe_key"]),
        "duplicates": duplicate_count,
        "groups": duplicate_groups,
        "dry_run": dry_run,
    }


def create_pipeline_run(
    conn: sqlite3.Connection | None = None,
    *,
    stages: list[str],
    mode: str,
    min_score: int,
    batch_size: int | None,
    workers: int,
    validation_mode: str,
) -> int:
    """Record a new pipeline run and return its id."""
    if conn is None:
        conn = get_connection()
    ensure_pipeline_tables(conn)

    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO pipeline_runs (
            stages, mode, status, min_score, batch_size, workers,
            validation_mode, started_at, created_at, updated_at
        )
        VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ",".join(stages),
            mode,
            min_score,
            batch_size,
            workers,
            validation_mode,
            now,
            now,
            now,
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_pipeline_run(
    conn: sqlite3.Connection | None,
    run_id: int,
    *,
    status: str,
    error: str | None = None,
) -> None:
    """Mark a pipeline run complete, partial, or failed."""
    if conn is None:
        conn = get_connection()

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE pipeline_runs
           SET status = ?,
               ended_at = ?,
               error = ?,
               updated_at = ?
         WHERE id = ?
        """,
        (status, now, error, now, run_id),
    )
    conn.commit()


def start_pipeline_stage(
    conn: sqlite3.Connection | None,
    run_id: int,
    stage: str,
    pending_before: int | None = None,
) -> int:
    """Record the start of one pipeline stage and return its stage-run id."""
    if conn is None:
        conn = get_connection()

    now = datetime.now(timezone.utc).isoformat()
    cursor = conn.execute(
        """
        INSERT INTO pipeline_stage_runs (
            run_id, stage, status, pending_before,
            started_at, created_at, updated_at
        )
        VALUES (?, ?, 'running', ?, ?, ?, ?)
        """,
        (run_id, stage, pending_before, now, now, now),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_pipeline_stage(
    conn: sqlite3.Connection | None,
    stage_run_id: int,
    *,
    status: str,
    pending_after: int | None = None,
    elapsed_seconds: float | None = None,
    error: str | None = None,
) -> None:
    """Record the end state for one pipeline stage."""
    if conn is None:
        conn = get_connection()

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        UPDATE pipeline_stage_runs
           SET status = ?,
               pending_after = ?,
               elapsed_seconds = ?,
               ended_at = ?,
               error = ?,
               updated_at = ?
         WHERE id = ?
        """,
        (status, pending_after, elapsed_seconds, now, error, now, stage_run_id),
    )
    conn.commit()


def get_stats(conn: sqlite3.Connection | None = None) -> dict:
    """Return job counts by pipeline stage.

    Provides a snapshot of how many jobs are at each stage, useful for
    dashboard display and pipeline progress tracking.

    Args:
        conn: Database connection. Uses get_connection() if None.

    Returns:
        Dictionary with keys:
            total, by_site, pending_detail, with_description,
            scored, unscored, tailored, untailored_eligible,
            with_cover_letter, applied, score_distribution
    """
    if conn is None:
        conn = get_connection()

    stats: dict = {}

    # Total jobs
    stats["total"] = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    stats["duplicates"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE duplicate_of_url IS NOT NULL"
    ).fetchone()[0]
    stats["active_total"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE duplicate_of_url IS NULL"
    ).fetchone()[0]

    # By site breakdown
    rows = conn.execute(
        """
        SELECT COALESCE(source_board, strategy, site, 'Unknown') AS source, COUNT(*) as cnt
          FROM jobs
         WHERE duplicate_of_url IS NULL
         GROUP BY COALESCE(source_board, strategy, site, 'Unknown')
         ORDER BY cnt DESC
        """
    ).fetchall()
    stats["by_site"] = [(row[0], row[1]) for row in rows]

    # Enrichment stage
    stats["pending_detail"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_scraped_at IS NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["with_description"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE full_description IS NOT NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["detail_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE detail_error IS NOT NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    # Scoring stage
    stats["scored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE fit_score IS NOT NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["unscored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE full_description IS NOT NULL AND fit_score IS NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    # Score distribution
    dist_rows = conn.execute(
        "SELECT fit_score, COUNT(*) as cnt FROM jobs "
        "WHERE fit_score IS NOT NULL AND duplicate_of_url IS NULL "
        "GROUP BY fit_score ORDER BY fit_score DESC"
    ).fetchall()
    stats["score_distribution"] = [(row[0], row[1]) for row in dist_rows]

    stats["audited"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE audited_at IS NOT NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["recommended"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE audit_label IN ('priority', 'recommended') AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["audit_excluded"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE audit_label = 'exclude' AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["diagnosed"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE diagnosed_at IS NOT NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["undiagnosed"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE fit_score IS NOT NULL AND full_description IS NOT NULL "
        "AND duplicate_of_url IS NULL "
        "AND (diagnosed_at IS NULL "
        "OR (scored_at IS NOT NULL AND diagnosed_at < scored_at) "
        "OR (audited_at IS NOT NULL AND diagnosed_at < audited_at))"
    ).fetchone()[0]

    # Tailoring stage
    stats["tailored"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE tailored_resume_path IS NOT NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["untailored_eligible"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(audit_score, fit_score) >= 7 AND full_description IS NOT NULL "
        "AND duplicate_of_url IS NULL AND tailored_resume_path IS NULL"
    ).fetchone()[0]

    stats["tailor_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(tailor_attempts, 0) >= 5 "
        "AND tailored_resume_path IS NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    # Cover letter stage
    stats["with_cover_letter"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE cover_letter_path IS NOT NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["cover_exhausted"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE COALESCE(cover_attempts, 0) >= 5 "
        "AND duplicate_of_url IS NULL "
        "AND (cover_letter_path IS NULL OR cover_letter_path = '')"
    ).fetchone()[0]

    # Application stage
    stats["applied"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["apply_errors"] = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE apply_error IS NOT NULL AND duplicate_of_url IS NULL"
    ).fetchone()[0]

    stats["ready_to_apply"] = conn.execute(
        "SELECT COUNT(*) FROM jobs "
        "WHERE tailored_resume_path IS NOT NULL "
        "AND duplicate_of_url IS NULL "
        "AND applied_at IS NULL "
        "AND (apply_status IS NULL OR apply_status = 'failed') "
        "AND COALESCE(audit_score, fit_score, 0) >= 7"
    ).fetchone()[0]

    stats["applications_tracked"] = conn.execute(
        "SELECT COUNT(*) FROM applications"
    ).fetchone()[0]

    stats["interviews"] = conn.execute(
        "SELECT COUNT(*) FROM applications "
        "WHERE status IN ('recruiter_screen', 'hiring_manager', 'assessment', 'final')"
    ).fetchone()[0]

    stats["rejections"] = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status = 'rejected'"
    ).fetchone()[0]

    today = datetime.now(timezone.utc).date().isoformat()
    stats["followups_due"] = conn.execute(
        "SELECT COUNT(*) FROM applications "
        "WHERE next_follow_up_at IS NOT NULL AND substr(next_follow_up_at, 1, 10) <= ? "
        "AND status NOT IN ('rejected', 'closed', 'offer', 'withdrawn')",
        (today,),
    ).fetchone()[0]

    return stats


def store_jobs(conn: sqlite3.Connection, jobs: list[dict],
               site: str, strategy: str) -> tuple[int, int]:
    """Store discovered jobs, skipping exact URLs and marking same-job duplicates.

    Args:
        conn: Database connection.
        jobs: List of job dicts with keys: url, title, salary, description, location.
        site: Source site name (e.g. "RemoteOK", "Dice").
        strategy: Extraction strategy used (e.g. "json_ld", "api_response", "css_selectors").

    Returns:
        Tuple of (new_count, duplicate_count).
    """
    new = 0
    existing = 0

    for job in jobs:
        status = insert_discovered_job(conn, job, site=job.get("company") or site, strategy=strategy)
        if status == "new":
            new += 1
        elif status in {"existing", "duplicate"}:
            existing += 1

    conn.commit()
    return new, existing


def get_jobs_by_stage(conn: sqlite3.Connection | None = None,
                      stage: str = "discovered",
                      min_score: int | None = None,
                      limit: int = 100) -> list[dict]:
    """Fetch jobs filtered by pipeline stage.

    Args:
        conn: Database connection. Uses get_connection() if None.
        stage: One of "discovered", "enriched", "scored", "tailored", "applied".
        min_score: Minimum fit_score filter (only relevant for scored+ stages).
        limit: Maximum number of rows to return.

    Returns:
        List of job dicts.
    """
    if conn is None:
        conn = get_connection()

    conditions = {
        "discovered": "1=1",
        "pending_detail": "detail_scraped_at IS NULL AND duplicate_of_url IS NULL",
        "enriched": "full_description IS NOT NULL AND duplicate_of_url IS NULL",
        "pending_score": "full_description IS NOT NULL AND fit_score IS NULL AND duplicate_of_url IS NULL",
        "scored": "fit_score IS NOT NULL AND duplicate_of_url IS NULL",
        "pending_diagnosis": (
            "fit_score IS NOT NULL AND full_description IS NOT NULL "
            "AND duplicate_of_url IS NULL "
            "AND (diagnosed_at IS NULL "
            "OR (scored_at IS NOT NULL AND diagnosed_at < scored_at) "
            "OR (audited_at IS NOT NULL AND diagnosed_at < audited_at))"
        ),
        "pending_tailor": (
            "COALESCE(audit_score, fit_score) >= ? AND full_description IS NOT NULL "
            "AND duplicate_of_url IS NULL "
            "AND tailored_resume_path IS NULL AND COALESCE(tailor_attempts, 0) < 5"
        ),
        "tailored": "tailored_resume_path IS NOT NULL AND duplicate_of_url IS NULL",
        "pending_apply": (
            "tailored_resume_path IS NOT NULL AND applied_at IS NULL "
            "AND duplicate_of_url IS NULL "
            "AND (apply_status IS NULL OR apply_status = 'failed') "
            "AND COALESCE(audit_score, fit_score, 0) >= 7"
        ),
        "applied": "applied_at IS NOT NULL AND duplicate_of_url IS NULL",
    }

    where = conditions.get(stage, "1=1")
    params: list = []

    if "?" in where and min_score is not None:
        params.append(min_score)
    elif "?" in where:
        params.append(7)  # default min_score

    if min_score is not None and "fit_score" not in where and stage in ("scored", "tailored", "applied"):
        where += " AND COALESCE(audit_score, fit_score) >= ?"
        params.append(min_score)

    query = (
        f"SELECT * FROM jobs WHERE {where} "
        "ORDER BY COALESCE(audit_score, fit_score) DESC NULLS LAST, "
        "fit_score DESC NULLS LAST, discovered_at DESC"
    )
    if limit > 0:
        query += " LIMIT ?"
        params.append(limit)

    rows = conn.execute(query, params).fetchall()

    # Convert sqlite3.Row objects to dicts
    if rows:
        columns = rows[0].keys()
        return [dict(zip(columns, row)) for row in rows]
    return []
