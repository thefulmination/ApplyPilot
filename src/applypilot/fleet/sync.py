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

import pandas as pd

from applypilot import config, database
from applypilot.apply import pgqueue
from applypilot.database import THIN_DESCRIPTION_CHARS
from applypilot.fleet import dedup as _dedup
from applypilot.fleet import queue as _queue
from applypilot.discovery.jobspy import store_jobspy_results


# ===========================================================================
# APPLY -- PUSH
# ===========================================================================

# Eligibility mirrors fleet_sync._PUSH_SELECT (the offsite-apply predicate, S2/S9.3):
#   not a dedup duplicate; score floor on COALESCE(audit_score, fit_score); not dead;
#   not already applied/in-flight; a real http(s) offsite (non-LinkedIn) target.
#   The research score lane stays opt-in for apply staging via ``include_research``.
# crash_unconfirmed / no_confirmation are EXCLUDED (v1 parity): a posting that may
# already have been submitted under the user's name must never be re-pushed/re-applied.
_PUSH_APPLY_SELECT = """
SELECT url, company, title, application_url,
       CAST({score_expr} AS REAL) AS score
FROM jobs
WHERE duplicate_of_url IS NULL
  AND {score_expr} >= ?
  AND COALESCE(liveness_status, '') != 'dead'
  AND LENGTH(COALESCE(full_description,'')) >= {thin_description_chars}
  AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress', 'crash_unconfirmed'))
  AND COALESCE(apply_error, '') NOT IN ('no_confirmation', 'crash_unconfirmed')
  AND application_url LIKE 'http%'
  AND application_url NOT LIKE '%linkedin.com%'
  {company_blocklist}
ORDER BY score DESC
"""

# Extra clause appended when the home brain has an ``applications`` ledger table --
# excludes URLs already in the ledger so the fleet never re-pushes a job the home
# runner already applied to via a different lane.
_PUSH_APPLY_LEDGER_CROSS_CHECK = (
    "  AND COALESCE(application_url, url) NOT IN (\n"
    "      SELECT COALESCE(NULLIF(application_url,''), job_url) FROM applications WHERE status = 'applied')\n"
)


def _applications_table_exists(sqlite_conn: sqlite3.Connection) -> bool:
    """Return True if the home brain has an ``applications`` ledger table."""
    row = sqlite_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='applications'"
    ).fetchone()
    return row is not None


def _company_blocklist_sql() -> tuple[str, list[str]]:
    names, patterns = config.load_blocked_companies()
    clauses: list[str] = []
    params: list[str] = []
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
    return "  AND " + "\n  AND ".join(clauses), params


def _lane_filter_sql() -> tuple[str, list[str]]:
    off_needles, on_tags = config.load_lane_filter()
    params: list[str] = []
    lane_parts = [
        "COALESCE(fit_gap_category, '') != 'wrong_role_lane'",
        "COALESCE(recommended_action, '') != 'ignore'",
    ]
    if off_needles:
        tnorm = "LOWER(' ' || COALESCE(title, '') || ' ')"
        title_or = " OR ".join(f"{tnorm} LIKE ?" for _ in off_needles)
        params.extend(f"%{needle}%" for needle in off_needles)
        flag_guard = ""
        if on_tags:
            flag_guard = " AND " + " AND ".join(
                "COALESCE(audit_flags, '') NOT LIKE ?" for _ in on_tags
            )
            params.extend(f'%"{tag}"%' for tag in on_tags)
        lane_parts.append(f"NOT (({title_or}){flag_guard})")
    return (
        "\n  AND (decision_source IS NOT NULL OR ("
        + " AND ".join(lane_parts)
        + "))",
        params,
    )


def _inject_before_order_by(sql: str, clause: str) -> str:
    if not clause:
        return sql
    return sql.replace("ORDER BY score DESC", clause + "\nORDER BY score DESC")


def backfill_applied_set(sqlite_conn: sqlite3.Connection, pg_conn: Any) -> int:
    """Seed PG applied_set (the lease-time R9 dedup) from the home brain's apply history,
    so the fleet never re-applies a job already applied OUTSIDE the fleet. Idempotent."""
    # Part 1: jobs with apply_status='applied' or crash-variant apply_error.
    rows: list[Any] = sqlite_conn.execute(
        "SELECT DISTINCT company, title FROM jobs "
        "WHERE apply_status = 'applied' OR apply_error IN ('no_confirmation','crash_unconfirmed')"
    ).fetchall()
    # Part 2: applications ledger (may not exist in minimal test fixtures).
    if _applications_table_exists(sqlite_conn):
        ledger_rows = sqlite_conn.execute(
            "SELECT DISTINCT j.company, j.title FROM applications a "
            "JOIN jobs j ON j.url = a.job_url "
            "WHERE a.status = 'applied'"
        ).fetchall()
        rows = list(rows) + list(ledger_rows)
    n = 0
    with pg_conn.cursor() as cur:
        for r in rows:
            dk = _dedup.dedup_key(r["company"], r["title"])
            if not dk:
                continue
            cur.execute(
                "INSERT INTO applied_set (dedup_key, company) VALUES (%s,%s) "
                "ON CONFLICT (dedup_key) DO NOTHING",
                (dk, r["company"]),
            )
            n += cur.rowcount
    pg_conn.commit()
    return n


def _target_host(application_url: str | None) -> str | None:
    """Effective apply host (the governor key) -- the application_url netloc, port + creds
    stripped. Returns None when the URL has no parseable host."""
    if not application_url:
        return None
    host = (urlsplit(application_url).hostname or "").strip().lower()
    return host or None


def push_apply_rows(
    conn: Any,
    rows: list[dict[str, Any]],
    *,
    approved_batch: str | None = None,
    enforce_host_policy: bool = False,
    trusted_hosts: dict[str, str] | None = None,
) -> dict[str, int]:
    """Push prepared ATS rows, parking rows that are not safe for unattended apply."""
    from applypilot.fleet.host_policy import decide_host_policy

    allowed: list[dict[str, Any]] = []
    parked = []
    for row in rows:
        decision = decide_host_policy(row.get("application_url"), trusted_hosts=trusted_hosts)
        if enforce_host_policy and not decision.unattended_allowed:
            parked.append((row, decision))
        else:
            allowed.append(row)

    pushed = (
        _queue.push_apply_jobs(conn, allowed, approved_batch=approved_batch, commit=False)
        if allowed
        else 0
    )
    parked_count = 0
    with conn.cursor() as cur:
        for row, decision in parked:
            host = (
                row.get("target_host")
                or row.get("apply_domain")
                or _target_host(row.get("application_url"))
            )
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, apply_domain, target_host, "
                "lane, dedup_key, approved_batch, status, apply_status, apply_error) "
                "VALUES (%(url)s,%(company)s,%(title)s,%(application_url)s,%(score)s,"
                "%(apply_domain)s,%(target_host)s,'ats',%(dedup_key)s,%(approved_batch)s,"
                "'failed'::apply_queue_status,'skipped',%(apply_error)s) "
                "ON CONFLICT (url) DO UPDATE SET company=EXCLUDED.company, "
                "title=EXCLUDED.title, application_url=EXCLUDED.application_url, "
                "score=EXCLUDED.score, apply_domain=EXCLUDED.apply_domain, "
                "target_host=EXCLUDED.target_host, lane=EXCLUDED.lane, "
                "dedup_key=EXCLUDED.dedup_key, "
                "approved_batch=COALESCE(EXCLUDED.approved_batch, apply_queue.approved_batch), "
                "status=EXCLUDED.status, apply_status=EXCLUDED.apply_status, "
                "apply_error=EXCLUDED.apply_error, updated_at=now() "
                "WHERE apply_queue.status='queued'",
                {
                    "url": row.get("url"),
                    "company": row.get("company"),
                    "title": row.get("title"),
                    "application_url": row.get("application_url"),
                    "score": row.get("score"),
                    "apply_domain": host,
                    "target_host": host,
                    "dedup_key": row.get("dedup_key"),
                    "approved_batch": approved_batch,
                    "apply_error": f"host_policy:{decision.reason}",
                },
            )
            parked_count += cur.rowcount
    conn.commit()
    return {"pushed": pushed, "parked": parked_count}


def push_apply_eligible(
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    score_floor: int = 7,
    approved_batch: str | None = None,
    limit: int | None = None,
    include_research: bool = False,
    lane_filter: bool = True,
) -> int:
    """Push approved offsite-eligible jobs from the brain into ``apply_queue`` (idempotent).

    Each pushed row carries a computed ``dedup_key`` (R9) and ``target_host`` (governor key),
    and is stamped with ``approved_batch`` (R11) via ``queue.push_apply_jobs``. Returns the
    number of queued rows the UPSERT touched (re-push of in-flight rows is a no-op)."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    try:
        backfill_applied_set(sq, pg)
        _queue.suppress_applied_set_duplicates(pg)
        out: list[dict[str, Any]] = []
        # Build the eligibility SQL; append the ledger cross-check when the home brain
        # has an applications table (the check is a no-op on minimal test fixtures).
        company_blocklist, company_params = _company_blocklist_sql()
        score_expr = (
            "COALESCE(audit_score, fit_score, research_fit_score)"
            if include_research
            else "COALESCE(audit_score, fit_score)"
        )
        base_sql = _PUSH_APPLY_SELECT.format(
            score_expr=score_expr,
            company_blocklist=company_blocklist,
            thin_description_chars=THIN_DESCRIPTION_CHARS,
        )
        if _applications_table_exists(sq):
            # Inject the cross-check before the ORDER BY clause.
            base_sql = _inject_before_order_by(
                base_sql,
                _PUSH_APPLY_LEDGER_CROSS_CHECK.rstrip("\n"),
            )
        lane_params: list[str] = []
        if lane_filter:
            lane_sql, lane_params = _lane_filter_sql()
            base_sql = _inject_before_order_by(base_sql, lane_sql)
        # Push the limit into SQL so we fetch only the top-N (not the whole eligible
        # set on a 70k+ job brain); keep the Python break as belt-and-suspenders.
        sql, params = base_sql, [score_floor] + company_params + lane_params
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        for r in sq.execute(sql, params).fetchall():
            host = _target_host(r["application_url"])
            out.append({
                "url": r["url"], "company": r["company"], "title": r["title"],
                "application_url": r["application_url"], "score": r["score"],
                "target_host": host, "apply_domain": host,
                "dedup_key": _dedup.dedup_key(r["company"], r["title"]),
            })
            if limit and len(out) >= limit:
                break
        # Phase 1 keeps tenant trust loading out of the SQLite push path; untrusted
        # Workday rows stay supervised until a trusted-host source is wired here.
        result = push_apply_rows(pg, out, approved_batch=approved_batch, enforce_host_policy=True)
        return result["pushed"]
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
    return _pull_results("apply_queue", sqlite_conn=sqlite_conn, pg_conn=pg_conn, batch=batch)


def pull_linkedin_results(
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    batch: int = 500,
) -> dict[str, int]:
    """Ingest terminal linkedin_queue results into the brain -- same contract as
    pull_apply_results (linkedin_queue is schema-identical). Before this existed the
    LinkedIn lane's ``pull`` was a report-only stub, so LinkedIn applies never reached
    the brain: any brain-driven path saw those employers as never-applied."""
    return _pull_results("linkedin_queue", sqlite_conn=sqlite_conn, pg_conn=pg_conn, batch=batch)


def _pull_results(
    table: str,
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    batch: int = 500,
) -> dict[str, int]:
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    counts: dict[str, int] = {}
    try:
        for res in pgqueue.fetch_pending_results(pg, limit=batch, table=table):
            url, status = res["url"], res["status"]
            if not sq.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone():
                counts["skipped"] = counts.get("skipped", 0) + 1
                pgqueue.mark_synced(pg, url, table=table)   # home is source of truth; drop stragglers
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
            pgqueue.mark_synced(pg, url, table=table)
            counts[status] = counts.get(status, 0) + 1
        return counts
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


# ===========================================================================
# LINKEDIN -- PUSH
# ===========================================================================

# Eligibility mirrors _PUSH_APPLY_SELECT but with the INVERSE effective-host predicate:
# only postings whose effective apply URL (application_url if http, else url) contains
# 'linkedin.com'. Stages UNAPPROVED (approval is a separate gated step via the LinkedIn
# canary gate). crash_unconfirmed is excluded (same rule as the offsite lane).
# {recency} is filled by push_linkedin_eligible with an optional "discovered within N
# days" clause. LinkedIn is a network-probe BLOCKED host (probing it from the apply IP is
# the ban risk -- see apply.liveness.BLOCKED_HOSTS), so posting RECENCY is the only safe
# pre-flight liveness proxy; apply-time detection (the logged-in worker sees a closed page
# and never phantom-applies) remains the real gate. company AND title are REQUIRED: a
# posting with neither is unapplyable AND collapses onto the (company,title) dedup_key, so
# hundreds of them would silently share a single leasable slot.
_PUSH_LINKEDIN_SELECT = """
SELECT url, company, title,
       CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END AS application_url,
       CAST(COALESCE(audit_score, fit_score) AS REAL) AS score
FROM jobs
WHERE duplicate_of_url IS NULL
  AND COALESCE(audit_score, fit_score) >= ?
  AND COALESCE(liveness_status, '') != 'dead'
  AND LENGTH(COALESCE(full_description,'')) >= {thin_description_chars}
  AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress', 'crash_unconfirmed'))
  AND COALESCE(apply_error, '') NOT IN ('no_confirmation', 'crash_unconfirmed')
  AND TRIM(COALESCE(company, '')) != ''
  AND TRIM(COALESCE(title, '')) != ''
  AND (CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END) LIKE '%linkedin.com%'
  {company_blocklist}
  {recency}
ORDER BY score DESC
"""


def push_linkedin_eligible(
    *,
    sqlite_conn=None,
    pg_conn=None,
    score_floor: int = 7,
    max_age_days: int | None = None,
    approved_batch=None,
    limit=None,
    lane_filter: bool = True,
) -> int:
    """Push LinkedIn-eligible jobs from the brain into ``linkedin_queue`` (idempotent).

    The effective-host predicate is the INVERSE of the offsite lane: only postings
    whose effective apply URL (application_url when it starts with 'http', else url)
    contains 'linkedin.com'. Excludes postings with no company or title (unapplyable +
    dedup_key collapse). Stages rows UNAPPROVED (approval is a separate gated step via
    the LinkedIn canary gate). Returns the number of rows the UPSERT touched.

    ``max_age_days``: when > 0, only push postings discovered within that many days.
    LinkedIn can't be network-probed for liveness (blocked host), so recency is the
    safest pre-flight filter against stale/expired postings; None/0 disables it."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    try:
        out = []
        params = [score_floor]
        recency_params: list[str] = []
        recency = ""
        if max_age_days and int(max_age_days) > 0:
            recency = "AND discovered_at IS NOT NULL AND julianday(discovered_at) >= julianday('now', ?)"
            recency_params.append(f"-{int(max_age_days)} days")
        company_blocklist, company_params = _company_blocklist_sql()
        sql = _PUSH_LINKEDIN_SELECT.format(
            company_blocklist=company_blocklist,
            recency=recency,
            thin_description_chars=THIN_DESCRIPTION_CHARS,
        )
        lane_params: list[str] = []
        if lane_filter:
            lane_sql, lane_params = _lane_filter_sql()
            sql = _inject_before_order_by(sql, lane_sql)
        params.extend(company_params)
        params.extend(recency_params)
        params.extend(lane_params)
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        for r in sq.execute(sql, params).fetchall():
            out.append({
                "url": r["url"], "company": r["company"], "title": r["title"],
                "application_url": r["application_url"], "score": r["score"],
                "dedup_key": _dedup.dedup_key(r["company"], r["title"]),
            })
            if limit and len(out) >= limit:
                break
        return _queue.push_linkedin_jobs(pg, out, approved_batch=approved_batch)
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


# Apply-shaped LinkedIn postings held out of the push SOLELY for want of a score.
# Mirrors _PUSH_LINKEDIN_SELECT's apply-shape predicates but inverts the score check
# (both audit_score AND fit_score NULL). Not network-dependent.
_COUNT_LINKEDIN_UNSCORED = """
SELECT COUNT(*) FROM jobs
WHERE duplicate_of_url IS NULL
  AND audit_score IS NULL AND fit_score IS NULL
  AND COALESCE(liveness_status, '') != 'dead'
  AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress', 'crash_unconfirmed'))
  AND COALESCE(apply_error, '') NOT IN ('no_confirmation', 'crash_unconfirmed')
  AND TRIM(COALESCE(company, '')) != ''
  AND TRIM(COALESCE(title, '')) != ''
  AND (CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END) LIKE '%linkedin.com%'
"""


def count_linkedin_unscored(sqlite_conn=None) -> int:
    """Count apply-shaped LinkedIn postings excluded from the push SOLELY because they
    are unscored (audit_score AND fit_score both NULL). These are correctly NOT applied
    to -- you don't apply to a job you haven't scored for fit -- but the count makes the
    backlog VISIBLE instead of silently lost: run the scorer to fold them into the pool."""
    own = sqlite_conn is None
    sq = sqlite_conn or _home_conn()
    try:
        return int(sq.execute(_COUNT_LINKEDIN_UNSCORED).fetchone()[0])
    finally:
        if own:
            sq.close()


# ===========================================================================
# COMPUTE -- PUSH
# ===========================================================================

# Compute is IP-free pure work (S8): score/audit/tailor/enrich. We enqueue brain jobs
# that lack the corresponding result. The eligibility is intentionally light (no liveness
# / approval gate -- scoring a job is harmless): a real job url at/above an optional floor.
_PUSH_COMPUTE_SELECT = """
SELECT url, company, title, application_url, full_description,
       CAST(COALESCE(audit_score, fit_score) AS REAL) AS score
FROM jobs
WHERE duplicate_of_url IS NULL
  AND COALESCE(COALESCE(audit_score, fit_score), 0) >= ?
ORDER BY score DESC
"""

_PUSH_COMPUTE_UNSCORED = """
SELECT url, company, title, application_url, full_description,
       CAST(NULL AS REAL) AS score
FROM jobs
WHERE duplicate_of_url IS NULL
  AND audit_score IS NULL
  AND fit_score IS NULL
  AND research_fit_score IS NULL
  AND TRIM(COALESCE(full_description, '')) != ''
ORDER BY discovered_at DESC
"""


def push_compute_eligible(
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    task: str,
    score_floor: int = 0,
    limit: int | None = None,
    unscored_only: bool = False,
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
        sql, params = (_PUSH_COMPUTE_UNSCORED, []) if unscored_only else (_PUSH_COMPUTE_SELECT, [score_floor])
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        for r in sq.execute(sql, params).fetchall():
            out.append({
                "url": r["url"], "task": task, "est_cost_usd": 0,
                "payload": {
                    "url": r["url"], "company": r["company"], "title": r["title"],
                    "application_url": r["application_url"], "full_description": r["full_description"],
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
SELECT url, task, result
FROM compute_queue
WHERE status IN ('done', 'failed')
  AND synced_to_home_at IS NULL
ORDER BY updated_at
LIMIT %(limit)s
"""

_REOPEN_COMPUTE_RESULTS = """
UPDATE compute_queue
SET synced_to_home_at = NULL,
    updated_at = now()
WHERE status = ANY(%(statuses)s)
  AND synced_to_home_at IS NOT NULL
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
            url, task, result = res["url"], res["task"], res["result"]
            if sq.execute("SELECT 1 FROM jobs WHERE url = ?", (url,)).fetchone():
                score, decision = _advisory_fields(result)
                sq.execute(_PULL_COMPUTE_ADVISORY, {
                    "url": url, "research_fit_score": score, "research_decision": decision,
                })
                sq.commit()
                n += 1
            _mark_compute_synced(pg, url, task)
        return n
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


def reopen_compute_results(*, pg_conn: Any | None = None, statuses: tuple[str, ...] = ("done", "failed")) -> int:
    """Re-serve completed compute results for a later home pull without touching the brain."""
    own_pg = pg_conn is None
    pg = pg_conn or pgqueue.connect()
    try:
        with pg.cursor() as cur:
            cur.execute(_REOPEN_COMPUTE_RESULTS, {"statuses": list(statuses)})
            n = cur.rowcount
        pg.commit()
        return n
    finally:
        if own_pg:
            pg.close()


def _mark_compute_synced(pg_conn: Any, url: str, task: str) -> None:
    """Stamp a compute_queue row ingested-home so the compute PULL is idempotent."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "UPDATE compute_queue SET synced_to_home_at = now() WHERE url = %s AND task = %s",
            (url, task),
        )
    pg_conn.commit()


# ===========================================================================
# DISCOVERY -- PULL
# ===========================================================================


def pull_discovered(*, sqlite_conn=None, pg_conn=None, batch=500) -> int:
    """Ingest staged discovery postings into the shared brain via store_jobspy_results.
    Group unsynced rows by source_label, rebuild a DataFrame per group, dedup-insert,
    then mark synced. Idempotent: synced rows are skipped; store_jobspy_results dedups by url."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    n = 0
    try:
        with pg.cursor() as cur:
            cur.execute(
                "SELECT id, source_label, posting FROM discovered_postings "
                "WHERE synced_to_home_at IS NULL ORDER BY discovered_at LIMIT %s",
                (batch,),
            )
            rows = cur.fetchall()
        if not rows:
            return 0
        by_label: dict[str, list] = {}
        ids: list[int] = []
        for r in rows:
            by_label.setdefault(r["source_label"] or "", []).append(r["posting"])
            ids.append(r["id"])
        for label, postings in by_label.items():
            store_jobspy_results(sq, pd.DataFrame(postings), label)
            n += len(postings)
        with pg.cursor() as cur:
            cur.execute("UPDATE discovered_postings SET synced_to_home_at = now() WHERE id = ANY(%s)", (ids,))
        pg.commit()
        return n
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


# ===========================================================================
# INBOX OUTCOMES -- PUSH (read-only summary of the brain's email_events)
# ===========================================================================

_PUSH_INBOX_SELECT = (
    "SELECT message_id, job_url, company, title, stage, outcome, "
    "sender_domain, confidence, occurred_at FROM email_events ORDER BY occurred_at"
)

_UPSERT_INBOX_OUTCOME = (
    "INSERT INTO inbox_outcomes "
    "(message_id, dedup_key, job_url, company, title, stage, outcome, sender_domain, confidence, occurred_at) "
    "VALUES (%(message_id)s,%(dedup_key)s,%(job_url)s,%(company)s,%(title)s,%(stage)s,"
    "%(outcome)s,%(sender_domain)s,%(confidence)s,%(occurred_at)s) "
    "ON CONFLICT (message_id) DO UPDATE SET "
    "dedup_key=EXCLUDED.dedup_key, job_url=EXCLUDED.job_url, company=EXCLUDED.company, "
    "title=EXCLUDED.title, stage=EXCLUDED.stage, outcome=EXCLUDED.outcome, "
    "sender_domain=EXCLUDED.sender_domain, confidence=EXCLUDED.confidence, "
    "occurred_at=EXCLUDED.occurred_at, updated_at=now()"
)


def push_inbox_outcomes(*, sqlite_conn=None, pg_conn=None, limit: int | None = None) -> int:
    """Push the brain's email_events outcome summaries into PG inbox_outcomes
    (idempotent by message_id). Read-only on the brain; only the thin summary +
    the R9 dedup_key cross (no body_text/PII). Returns rows the UPSERT touched."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    n = 0
    try:
        sql = _PUSH_INBOX_SELECT
        params: list = []
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        with pg.cursor() as cur:
            for r in sq.execute(sql, params).fetchall():
                cur.execute(_UPSERT_INBOX_OUTCOME, {
                    "message_id": r["message_id"],
                    "dedup_key": _dedup.dedup_key(r["company"], r["title"]),
                    "job_url": r["job_url"], "company": r["company"], "title": r["title"],
                    "stage": r["stage"], "outcome": r["outcome"],
                    "sender_domain": r["sender_domain"], "confidence": r["confidence"],
                    "occurred_at": r["occurred_at"],
                })
                n += cur.rowcount
        pg.commit()
        return n
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


# ===========================================================================
# Connection
# ===========================================================================

def _home_conn() -> sqlite3.Connection:
    """Open an isolated connection to the authoritative home SQLite (not the app's shared
    singleton, so the sync never contends with a live run's connection)."""
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=60000")
    conn.row_factory = sqlite3.Row
    database.ensure_columns(conn)
    database.ensure_job_indexes(conn)
    return conn
