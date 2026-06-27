"""v3 home-brain <-> coordination-Postgres bridge (Topology A).

The v3 analogue of ``apply.fleet_sync``, layered on the v3 queues:

  APPLY   PUSH: approved offsite-eligible jobs (SQLite brain) -> apply_queue (PG),
                carrying ``dedup_key`` (R9) + ``target_host`` (governor key) +
                the owner ``approved_batch`` token (R11). Idempotent by url.
          PULL: terminal apply_queue rows -> brain ``jobs.apply_status`` idempotently,
                NEVER demoting a confirmed apply.
  COMPUTE PUSH: jobs needing a compute task (score/audit/tailor/enrich) -> compute_queue.
          PULL: compute results -> brain as ADVISORY ONLY (``research_fit_score`` /
                ``research_decision``); NEVER auto-promoted to fit_score / audit_score
                (the unified-brain rule -- the owner promotes explicitly).

This mirrors ``apply.fleet_sync``'s home-conn ownership + write-home-then-mark-synced
idempotency contract exactly. Only routing columns leave the brain; the 60-col jobs
schema, profile, and local paths never cross. See spec S2 / S8 / S8.5 / S10.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any
from urllib.parse import urlsplit

from applypilot import config
from applypilot.apply import pgqueue
from applypilot.fleet import dedup as _dedup
from applypilot.fleet import queue as _queue


# ===========================================================================
# APPLY -- PUSH
# ===========================================================================

# Eligibility mirrors fleet_sync._PUSH_SELECT (the offsite-apply predicate, S2/S9.3):
#   not a dedup duplicate; score floor on COALESCE(audit_score, fit_score); not dead;
#   not already applied/in-flight; a real http(s) offsite (non-LinkedIn) target.
_PUSH_APPLY_SELECT = """
SELECT url, company, title, application_url,
       CAST(COALESCE(audit_score, fit_score) AS REAL) AS score
FROM jobs
WHERE duplicate_of_url IS NULL
  AND COALESCE(audit_score, fit_score) >= ?
  AND COALESCE(liveness_status, '') != 'dead'
  AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress'))
  AND application_url LIKE 'http%'
  AND application_url NOT LIKE '%linkedin.com%'
ORDER BY score DESC
"""


def _target_host(application_url: str | None) -> str | None:
    """Effective apply host (the governor key) -- the application_url netloc, port + creds
    stripped. Returns None when the URL has no parseable host."""
    if not application_url:
        return None
    host = (urlsplit(application_url).hostname or "").strip().lower()
    return host or None


def push_apply_eligible(
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    score_floor: int = 7,
    approved_batch: str | None = None,
    limit: int | None = None,
) -> int:
    """Push approved offsite-eligible jobs from the brain into ``apply_queue`` (idempotent).

    Each pushed row carries a computed ``dedup_key`` (R9) and ``target_host`` (governor key),
    and is stamped with ``approved_batch`` (R11) via ``queue.push_apply_jobs``. Returns the
    number of queued rows the UPSERT touched (re-push of in-flight rows is a no-op)."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    try:
        out: list[dict[str, Any]] = []
        for r in sq.execute(_PUSH_APPLY_SELECT, (score_floor,)).fetchall():
            host = _target_host(r["application_url"])
            out.append({
                "url": r["url"], "company": r["company"], "title": r["title"],
                "application_url": r["application_url"], "score": r["score"],
                "target_host": host, "apply_domain": host,
                "dedup_key": _dedup.dedup_key(r["company"], r["title"]),
            })
            if limit and len(out) >= limit:
                break
        return _queue.push_apply_jobs(pg, out, approved_batch=approved_batch)
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


# ===========================================================================
# APPLY -- PULL
# ===========================================================================

# Terminal apply rows the fleet wrote that home hasn't ingested yet. Same projection
# as pgqueue._PULL_SQL; the v3 worker (queue.write_apply_result) closes rows with these
# same statuses, so the existing fetch_pending_results / mark_synced surface works as-is.

_PULL_APPLIED = """
UPDATE jobs
SET apply_status            = 'applied',
    applied_at              = COALESCE(:applied_at, applied_at),
    apply_error             = NULL,
    agent_id                = NULL,
    verification_confidence = :verification_confidence,
    apply_duration_ms       = :apply_duration_ms
WHERE url = :url
  AND COALESCE(apply_status, '') != 'applied'
"""

# failed / blocked / crash_unconfirmed: pin attempts so the home loop never re-acquires
# the posting; map blocked -> failed; keep crash_unconfirmed (it drives posting-level dedup).
_PULL_TERMINAL = """
UPDATE jobs
SET apply_status      = CASE WHEN :status = 'blocked' THEN 'failed' ELSE :status END,
    apply_error       = :apply_error,
    apply_attempts    = 99,
    agent_id          = NULL,
    apply_duration_ms = :apply_duration_ms
WHERE url = :url
  AND COALESCE(apply_status, '') != 'applied'   -- never demote a confirmed apply
"""


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def pull_apply_results(
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    batch: int = 500,
) -> dict[str, int]:
    """Ingest terminal apply_queue results into the brain, idempotently. Returns a
    status->count summary. Per row: write home FIRST, then stamp the PG row synced -- a
    crash in between just re-pulls next time (the WHERE guards make the replay a no-op,
    and a confirmed apply is never demoted). Mirrors ``fleet_sync.pull_results``."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    counts: dict[str, int] = {}
    try:
        for res in pgqueue.fetch_pending_results(pg, limit=batch):
            url, status = res["url"], res["status"]
            if not sq.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone():
                counts["skipped"] = counts.get("skipped", 0) + 1
                pgqueue.mark_synced(pg, url)        # home is source of truth; drop stragglers
                continue
            if status == "applied":
                sq.execute(_PULL_APPLIED, {
                    "url": url, "applied_at": _iso(res["applied_at"]),
                    "verification_confidence": res["verification_confidence"],
                    "apply_duration_ms": res["apply_duration_ms"],
                })
            else:
                sq.execute(_PULL_TERMINAL, {
                    "url": url, "status": status, "apply_error": res["apply_error"],
                    "apply_duration_ms": res["apply_duration_ms"],
                })
            sq.commit()
            pgqueue.mark_synced(pg, url)
            counts[status] = counts.get(status, 0) + 1
        return counts
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


# ===========================================================================
# COMPUTE -- PUSH
# ===========================================================================

# Compute is IP-free pure work (S8): score/audit/tailor/enrich. We enqueue brain jobs
# that lack the corresponding result. The eligibility is intentionally light (no liveness
# / approval gate -- scoring a job is harmless): a real job url at/above an optional floor.
_PUSH_COMPUTE_SELECT = """
SELECT url, company, title, application_url,
       CAST(COALESCE(audit_score, fit_score) AS REAL) AS score
FROM jobs
WHERE duplicate_of_url IS NULL
  AND COALESCE(COALESCE(audit_score, fit_score), 0) >= ?
ORDER BY score DESC
"""


def push_compute_eligible(
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    task: str,
    score_floor: int = 0,
    limit: int | None = None,
) -> int:
    """Push brain jobs needing a compute ``task`` (score|audit|tailor|enrich) into
    ``compute_queue`` (S8). Each row carries the minimal ``payload`` the task needs
    (url + company + title + application_url). Idempotent by url. Returns the number
    of queued rows the UPSERT touched."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    try:
        out: list[dict[str, Any]] = []
        for r in sq.execute(_PUSH_COMPUTE_SELECT, (score_floor,)).fetchall():
            out.append({
                "url": r["url"], "task": task, "est_cost_usd": 0,
                "payload": {
                    "url": r["url"], "company": r["company"],
                    "title": r["title"], "application_url": r["application_url"],
                },
            })
            if limit and len(out) >= limit:
                break
        return _queue.push_compute_jobs(pg, out)
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


# ===========================================================================
# COMPUTE -- PULL (ADVISORY ONLY)
# ===========================================================================

# Compute results are ADVISORY (unified-brain rule, S8): they land in research_*
# columns and NEVER touch fit_score / audit_score. The owner promotes explicitly.
_PULL_COMPUTE_ADVISORY = """
UPDATE jobs
SET research_fit_score = COALESCE(:research_fit_score, research_fit_score),
    research_decision  = COALESCE(:research_decision, research_decision)
WHERE url = :url
"""

_PULL_COMPUTE_PENDING = """
SELECT url, result
FROM compute_queue
WHERE status IN ('done', 'failed')
  AND synced_to_home_at IS NULL
ORDER BY updated_at
LIMIT %(limit)s
"""


def _advisory_fields(result: Any) -> tuple[Any, Any]:
    """Extract the advisory (research_fit_score, research_decision) from a compute result
    JSONB. Tolerant of None / str / dict and a couple of synonym keys."""
    if result is None:
        return None, None
    if isinstance(result, (str, bytes, bytearray)):
        try:
            result = json.loads(result)
        except (ValueError, TypeError):
            return None, None
    if not isinstance(result, dict):
        return None, None
    score = result.get("research_fit_score")
    if score is None:
        score = result.get("fit_score", result.get("score"))
    decision = result.get("research_decision")
    if decision is None:
        decision = result.get("decision", result.get("verdict"))
    return score, decision


def pull_compute_results(
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    batch: int = 500,
) -> int:
    """Ingest compute results into the brain as ADVISORY rows (S8). Writes
    ``research_fit_score`` / ``research_decision`` ONLY -- NEVER fit_score / audit_score.
    Write-home-then-mark-synced; idempotent on re-pull. Returns the count ingested."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    n = 0
    try:
        with pg.cursor() as cur:
            cur.execute(_PULL_COMPUTE_PENDING, {"limit": batch})
            pending = cur.fetchall()
        pg.rollback()  # read-only fetch
        for res in pending:
            url, result = res["url"], res["result"]
            if sq.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone():
                score, decision = _advisory_fields(result)
                sq.execute(_PULL_COMPUTE_ADVISORY, {
                    "url": url, "research_fit_score": score, "research_decision": decision,
                })
                sq.commit()
                n += 1
            _mark_compute_synced(pg, url)
        return n
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


def _mark_compute_synced(pg_conn: Any, url: str) -> None:
    """Stamp a compute_queue row ingested-home so the compute PULL is idempotent."""
    with pg_conn.cursor() as cur:
        cur.execute("UPDATE compute_queue SET synced_to_home_at = now() WHERE url = %s", (url,))
    pg_conn.commit()


# ===========================================================================
# Connection
# ===========================================================================

def _home_conn() -> sqlite3.Connection:
    """Open an isolated connection to the authoritative home SQLite (not the app's shared
    singleton, so the sync never contends with a live run's connection)."""
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn
