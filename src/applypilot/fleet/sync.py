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
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import pandas as pd

from applypilot import config, database
from applypilot.apply import pgqueue
from applypilot.apply.greenhouse_adapter import parse_greenhouse_url
from applypilot.apply import tenant_sessions as _tenant_sessions
from applypilot.database import THIN_DESCRIPTION_CHARS
from applypilot.fleet import dedup as _dedup
from applypilot.fleet import eligibility as _eligibility
from applypilot.fleet import tenant_router as _tenant_router
from applypilot.fleet import host_framework as _host_framework
from applypilot.fleet import queue as _queue
from applypilot.discovery.jobspy import store_jobspy_results


# ===========================================================================
# APPLY -- PUSH
# ===========================================================================

# Canonical decisions are the only apply authorization. Legacy score columns remain
# useful for analysis/compute work, but are deliberately absent from this selector.
# crash_unconfirmed / no_confirmation are EXCLUDED (v1 parity): a posting that may
# already have been submitted under the user's name must never be re-pushed/re-applied.
_CANONICAL_PROVENANCE_SELECT = """
SELECT j.url, j.company, j.title, {application_url} AS application_url,
       {location_expr}, j.full_description,
       CAST(d.final_score AS REAL) AS score,
       d.decision_id, d.policy_version, d.action AS decision_action,
       d.qualification_verdict, CAST(d.qualification_score AS REAL) AS qualification_score,
       CAST(json_extract(p.config_json, '$.qualificationFloor') AS REAL) AS qualification_floor,
       CAST(d.preference_score AS REAL) AS preference_score,
       CAST(d.outcome_score AS REAL) AS outcome_score,
       CAST(d.final_score AS REAL) AS final_score,
       CAST(d.confidence AS REAL) AS decision_confidence,
       d.created_at AS decision_created_at, d.expires_at AS decision_expires_at,
       d.input_hash,
       {liveness_status_expr}, {liveness_reason_expr}, {last_verified_live_expr},
       j.linkedin_resolve_status, j.linkedin_resolved_at, j.linkedin_resolve_error,
       j.linkedin_unresolved_kind, j.linkedin_next_action
FROM jobs j
JOIN job_decisions d ON d.decision_id = j.canonical_decision_id
JOIN decision_policy_versions p ON p.policy_version = d.policy_version
WHERE j.duplicate_of_url IS NULL
  AND d.lane = ?
  AND p.lane = d.lane
  AND p.status IN ('canary', 'active')
  AND d.action = 'apply'
  AND d.qualification_verdict = 'qualified'
  AND d.final_score IS NOT NULL
  AND d.qualification_score IS NOT NULL
  AND d.preference_score IS NOT NULL
  AND d.outcome_score IS NOT NULL
  AND d.confidence IS NOT NULL
  AND d.input_hash IS NOT NULL AND TRIM(d.input_hash) != ''
  AND d.created_at IS NOT NULL
  AND d.expires_at IS NOT NULL AND julianday(d.expires_at) > julianday(?)
  AND json_type(p.config_json, '$.qualificationFloor') IN ('integer', 'real')
  AND d.qualification_score >= CAST(json_extract(p.config_json, '$.qualificationFloor') AS REAL)
  AND COALESCE(j.liveness_status, '') != 'dead'
  AND LENGTH(COALESCE(j.full_description,'')) >= {thin_description_chars}
  AND (j.apply_status IS NULL OR j.apply_status NOT IN ('applied', 'in_progress', 'crash_unconfirmed'))
  AND COALESCE(j.apply_error, '') NOT IN ('no_confirmation', 'crash_unconfirmed')
  {attempts_predicate}
  {shape_predicate}
  {company_blocklist}
  {recency}
ORDER BY d.final_score DESC, j.url ASC
"""

_PUSH_APPLY_SHAPE = """
  AND TRIM(COALESCE(j.company, '')) != ''
  AND TRIM(COALESCE(j.title, '')) != ''
  AND j.application_url LIKE 'http%'
  AND j.application_url NOT LIKE '%linkedin.com%'
"""

_PUSH_LINKEDIN_SHAPE = """
  AND TRIM(COALESCE(j.company, '')) != ''
  AND TRIM(COALESCE(j.title, '')) != ''
  AND (CASE WHEN j.application_url LIKE 'http%' THEN j.application_url ELSE j.url END) LIKE '%linkedin.com%'
  AND COALESCE(j.linkedin_resolve_status, '') IN ({fresh_statuses})
  AND j.linkedin_resolved_at IS NOT NULL
  {resolve_recency}
"""

# Extra clause appended when the home brain has an ``applications`` ledger table --
# excludes URLs already in the ledger so the fleet never re-pushes a job the home
# runner already applied to via a different lane.
_PUSH_APPLY_LEDGER_CROSS_CHECK = (
    "  AND COALESCE(j.application_url, j.url) NOT IN (\n"
    "      SELECT COALESCE(NULLIF(application_url,''), job_url) FROM applications WHERE status = 'applied')\n"
)


def _applications_table_exists(sqlite_conn: sqlite3.Connection) -> bool:
    """Return True if the home brain has an ``applications`` ledger table."""
    row = sqlite_conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='applications'"
    ).fetchone()
    return row is not None


def _tenant_route_policy(sqlite_conn: sqlite3.Connection, host: str) -> dict:
    try:
        row = sqlite_conn.execute(
            "SELECT status, profile_id, session_state, halted_until FROM ats_tenants WHERE host = ?",
            (host,),
        ).fetchone()
    except sqlite3.OperationalError:
        row = None
    if row is None:
        return _host_framework.unregistered_host_policy(host)
    now_iso = datetime.now(timezone.utc).isoformat()
    adapter_enabled = os.environ.get("APPLYPILOT_WORKDAY_ADAPTER_ENABLED", "").strip().lower() in {
        "1", "true", "yes", "on",
    }
    adapter_supported = adapter_enabled and host.endswith(
        ("myworkdayjobs.com", "myworkdaysite.com", "workdayjobs.com")
    )
    decision = _tenant_router.route_tenant(
        tenant_status=row["status"],
        session_state=row["session_state"],
        adapter_supported=adapter_supported,
        halted=bool(row["halted_until"] and now_iso < row["halted_until"]),
    )
    return {
        "session_required": row["status"] in {"supervised", "trusted"},
        "tenant_profile_id": row["profile_id"] or _tenant_sessions.profile_id_for_host(host),
        "routing_required": decision.routing_required,
        "execution_route": decision.route,
        "host_policy": decision.reason,
    }


def _jobs_column_exists(sqlite_conn: sqlite3.Connection, column: str) -> bool:
    return any(row[1] == column for row in sqlite_conn.execute("PRAGMA table_info(jobs)").fetchall())


def _optional_job_column_expr(
    sqlite_conn: sqlite3.Connection, column: str, *, alias: str | None = None
) -> str:
    """Project an optional legacy jobs column without weakening canonical joins."""
    output_name = alias or column
    if _jobs_column_exists(sqlite_conn, column):
        return f"j.{column} AS {output_name}"
    return f"NULL AS {output_name}"


def _canonical_projection_available(sqlite_conn: sqlite3.Connection) -> bool:
    """Fail closed while an older brain is waiting for its canonical migration."""
    tables = {
        row[0]
        for row in sqlite_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('job_decisions','decision_policy_versions')"
        ).fetchall()
    }
    if tables != {"job_decisions", "decision_policy_versions"}:
        return False
    job_columns = {row[1] for row in sqlite_conn.execute("PRAGMA table_info(jobs)").fetchall()}
    return "canonical_decision_id" in job_columns


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


def _inject_before_order_by(sql: str, clause: str) -> str:
    if not clause:
        return sql
    return sql.replace("ORDER BY d.final_score DESC", clause + "\nORDER BY d.final_score DESC")


def _canonical_provenance(row: sqlite3.Row) -> dict[str, Any]:
    return {
        key: row[key]
        for key in (
            "decision_id",
            "policy_version",
            "decision_action",
            "qualification_verdict",
            "qualification_score",
            "qualification_floor",
            "preference_score",
            "outcome_score",
            "final_score",
            "decision_confidence",
            "decision_created_at",
            "decision_expires_at",
            "input_hash",
        )
    }


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


def _dead_urls(sqlite_conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """Return URL + effective URL lists for local jobs marked dead."""
    rows = sqlite_conn.execute(
        "SELECT url, application_url FROM jobs WHERE COALESCE(liveness_status, '') = 'dead'"
    ).fetchall()
    urls = [r["url"] for r in rows if r["url"]]
    app_urls = [r["application_url"] for r in rows if r["application_url"]]
    return urls, app_urls


def _stale_linkedin_urls(
    sqlite_conn: sqlite3.Connection,
    *,
    max_age_days: int | None,
) -> tuple[list[str], list[str]]:
    if not max_age_days or int(max_age_days) <= 0:
        return [], []

    rows = sqlite_conn.execute(
        """
        SELECT url,
               CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END AS application_url
          FROM jobs
         WHERE (CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END) LIKE '%linkedin.com%'
           AND (
                discovered_at IS NULL
                OR julianday(discovered_at) IS NULL
                OR julianday(discovered_at) < julianday('now', ?)
           )
        """,
        (f"-{int(max_age_days)} days",),
    ).fetchall()
    urls = [r["url"] for r in rows if r["url"]]
    app_urls = [r["application_url"] for r in rows if r["application_url"]]
    return urls, app_urls


def _target_host(application_url: str | None) -> str | None:
    """Effective apply host (the governor key) -- the application_url netloc, port + creds
    stripped. Returns None when the URL has no parseable host."""
    if not application_url:
        return None
    host = (urlsplit(application_url).hostname or "").strip().lower()
    return host or None


def _routing_company(company: str | None, application_url: str | None) -> str | None:
    """Recover a stable employer key from a hosted Greenhouse board.

    Aggregators frequently omit company even though the application URL carries
    the tenant. Keeping company blank makes unrelated generic roles collide in
    posting-level dedup, so use the public board token when it is available.
    """
    if str(company or "").strip():
        return company
    parsed = parse_greenhouse_url(application_url or "")
    return parsed[0] if parsed else company


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
    score_floor: float = 7.0,
    approved_batch: str | None = None,
    limit: int | None = None,
    lane_filter: bool = True,
    diagnostics: bool = False,
) -> int | dict[str, int]:
    """Push approved offsite-eligible jobs from the brain into ``apply_queue`` (idempotent).

    Each pushed row carries a computed ``dedup_key`` (R9) and ``target_host`` (governor key),
    and is stamped with ``approved_batch`` (R11) via ``queue.push_apply_jobs``. Returns the
    number of queued rows the UPSERT touched (re-push of in-flight rows is a no-op)."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    try:
        if not _canonical_projection_available(sq):
            return 0
        backfill_applied_set(sq, pg)
        dead_urls, dead_app_urls = _dead_urls(sq)
        if dead_urls or dead_app_urls:
            _queue.retire_queued_dead_jobs(
                pg,
                dead_urls=dead_urls,
                dead_application_urls=dead_app_urls,
            )
        _queue.suppress_applied_set_duplicates(pg)
        out: list[dict[str, Any]] = []
        search_config = config.load_search_config()
        try:
            profile = config.load_profile()
        except (FileNotFoundError, ValueError, OSError):
            profile = {}
        work_authorization = profile.get("work_authorization", {}) if isinstance(profile, dict) else {}
        # score_floor and lane_filter remain accepted for CLI compatibility, but neither
        # authorizes work. The immutable canonical action and policy lane do that.
        del score_floor, lane_filter
        company_blocklist, company_params = _company_blocklist_sql()
        base_sql = _CANONICAL_PROVENANCE_SELECT.format(
            application_url="j.application_url",
            liveness_status_expr=_optional_job_column_expr(sq, "liveness_status"),
            liveness_reason_expr=_optional_job_column_expr(sq, "liveness_reason"),
            last_verified_live_expr=_optional_job_column_expr(sq, "last_verified_live"),
            shape_predicate=_PUSH_APPLY_SHAPE,
            location_expr="j.location AS location" if _jobs_column_exists(sq, "location") else "NULL AS location",
            company_blocklist=company_blocklist,
            recency="",
            thin_description_chars=THIN_DESCRIPTION_CHARS,
            attempts_predicate=(
                f"AND COALESCE(j.apply_attempts, 0) < {int(config.DEFAULTS['max_apply_attempts'])}"
                if _jobs_column_exists(sq, "apply_attempts") else ""
            ),
        )
        if _applications_table_exists(sq):
            # Inject the cross-check before the ORDER BY clause.
            base_sql = _inject_before_order_by(
                base_sql,
                _PUSH_APPLY_LEDGER_CROSS_CHECK.rstrip("\n"),
            )
        sql, params = base_sql, ["ats", datetime.now(timezone.utc).isoformat()] + company_params
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        for r in sq.execute(sql, params).fetchall():
            host = _target_host(r["application_url"])
            eligibility_status, eligibility_reason = _eligibility.evaluate_job_eligibility(
                location=r["location"],
                description=r["full_description"],
                location_policy=search_config.get("location", {}),
                work_authorization=work_authorization,
            )
            route_policy = _tenant_route_policy(sq, host)
            out.append({
                "url": r["url"], "company": r["company"], "title": r["title"],
                "application_url": r["application_url"], "score": r["score"],
                "target_host": host, "apply_domain": host,
                "liveness_status": r["liveness_status"],
                "liveness_reason": r["liveness_reason"],
                "liveness_checked_at": r["last_verified_live"],
                "dedup_key": _dedup.dedup_key(r["company"], r["title"]),
                "eligibility_status": eligibility_status,
                "eligibility_reason": eligibility_reason,
                **route_policy,
                **_canonical_provenance(r),
            })
            if limit and len(out) >= limit:
                break
        return _queue.push_apply_jobs(
            pg,
            out,
            approved_batch=approved_batch,
            require_liveness=True,
            require_eligibility=True,
        )
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

_PULL_MARK_DEAD = """
UPDATE jobs
SET liveness_status   = 'dead',
    liveness_reason   = :liveness_reason,
    last_verified_live = :last_verified_live
WHERE url = :url
  AND COALESCE(apply_status, '') != 'applied'
"""


def _iso(v: Any) -> Any:
    return v.isoformat() if hasattr(v, "isoformat") else v


def _result_marks_dead(res: Any) -> bool:
    status = str(res.get("status") or "").strip().lower()
    apply_status = str(res.get("apply_status") or "").strip().lower()
    apply_error = str(res.get("apply_error") or "").strip().lower()
    return "expired" in {status, apply_status, apply_error}


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
        fetched = pgqueue.fetch_pending_results(pg, limit=batch, table=table)
        for res in fetched:
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
                if _result_marks_dead(res):
                    sq.execute(_PULL_MARK_DEAD, {
                        "url": url,
                        "liveness_reason": "fleet_result_expired",
                        "last_verified_live": datetime.now(timezone.utc).isoformat(),
                    })
            sq.commit()
            pgqueue.mark_synced(pg, url, table=table)
            counts[status] = counts.get(status, 0) + 1
        if len(fetched) >= batch:
            with pg.cursor() as cur:
                cur.execute(
                    f"SELECT COUNT(*) AS n FROM {table} "
                    "WHERE status IN ('applied','failed','blocked','crash_unconfirmed') "
                    "AND synced_to_home_at IS NULL"
                )
                pending_remaining = int(cur.fetchone()["n"] or 0)
            pg.rollback()
            counts["pending_remaining"] = pending_remaining
            counts["batch_limited"] = int(pending_remaining > 0)
        return counts
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


# ===========================================================================
# LINKEDIN -- PUSH
# ===========================================================================

# Eligibility uses the same canonical authority with the inverse effective-host predicate:
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
def push_linkedin_eligible(
    *,
    sqlite_conn=None,
    pg_conn=None,
    score_floor: float = 7.0,
    max_age_days: int | None = None,
    approved_batch=None,
    limit=None,
    lane_filter: bool = True,
    max_resolved_age_days: int | None = _queue.LINKEDIN_FRESH_MAX_AGE_DAYS,
) -> int:
    """Push LinkedIn-eligible jobs from the brain into ``linkedin_queue`` (idempotent).

    The effective-host predicate is the INVERSE of the offsite lane: only postings
    whose effective apply URL (application_url when it starts with 'http', else url)
    contains 'linkedin.com'. Excludes postings with no company or title (unapplyable +
    dedup_key collapse). Stages rows UNAPPROVED (approval is a separate gated step via
    the LinkedIn canary gate). Returns the number of rows the UPSERT touched.

    ``max_age_days``: when > 0, only push postings discovered within that many days.
    ``max_resolved_age_days``: when > 0, require a recent logged-in LinkedIn resolver
    decision (``easy_apply`` or ``resolved_offsite``). The resolver check is the
    authoritative freshness gate because anonymous network liveness probes are blocked
    for LinkedIn."""
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    try:
        if not _canonical_projection_available(sq):
            return 0
        dead_urls, dead_app_urls = _dead_urls(sq)
        if dead_urls or dead_app_urls:
            _queue.retire_queued_dead_jobs(
                pg,
                dead_urls=dead_urls,
                dead_application_urls=dead_app_urls,
                table="linkedin_queue",
            )
        stale_urls, stale_app_urls = _stale_linkedin_urls(sq, max_age_days=max_age_days)
        if stale_urls or stale_app_urls:
            _queue.retire_queued_dead_jobs(
                pg,
                dead_urls=stale_urls,
                dead_application_urls=stale_app_urls,
                table="linkedin_queue",
            )
        out = []
        del score_floor, lane_filter
        resolve_recency_params: list[str] = []
        resolve_recency = ""
        if max_resolved_age_days and int(max_resolved_age_days) > 0:
            resolve_recency = "AND julianday(j.linkedin_resolved_at) >= julianday('now', ?)"
            resolve_recency_params.append(f"-{int(max_resolved_age_days)} days")
        recency_params: list[str] = []
        recency = ""
        if max_age_days and int(max_age_days) > 0:
            recency = "AND j.discovered_at IS NOT NULL AND julianday(j.discovered_at) >= julianday('now', ?)"
            recency_params.append(f"-{int(max_age_days)} days")
        company_blocklist, company_params = _company_blocklist_sql()
        sql = _CANONICAL_PROVENANCE_SELECT.format(
            application_url="CASE WHEN j.application_url LIKE 'http%' THEN j.application_url ELSE j.url END",
            liveness_status_expr=_optional_job_column_expr(sq, "liveness_status"),
            liveness_reason_expr=_optional_job_column_expr(sq, "liveness_reason"),
            last_verified_live_expr=_optional_job_column_expr(sq, "last_verified_live"),
            shape_predicate=_PUSH_LINKEDIN_SHAPE.format(
                fresh_statuses=",".join("?" for _ in _queue.LINKEDIN_FRESH_STATUSES),
                resolve_recency=resolve_recency,
            ),
            location_expr="j.location AS location" if _jobs_column_exists(sq, "location") else "NULL AS location",
            company_blocklist=company_blocklist,
            fresh_statuses=",".join("?" for _ in _queue.LINKEDIN_FRESH_STATUSES),
            resolve_recency=resolve_recency,
            recency=recency,
            thin_description_chars=THIN_DESCRIPTION_CHARS,
            attempts_predicate=(
                f"AND COALESCE(j.apply_attempts, 0) < {int(config.DEFAULTS['max_apply_attempts'])}"
                if _jobs_column_exists(sq, "apply_attempts") else ""
            ),
        )
        params = ["linkedin", datetime.now(timezone.utc).isoformat()]
        params.extend(_queue.LINKEDIN_FRESH_STATUSES)
        params.extend(resolve_recency_params)
        params.extend(company_params)
        params.extend(recency_params)
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        for r in sq.execute(sql, params).fetchall():
            out.append({
                "url": r["url"], "company": r["company"], "title": r["title"],
                "application_url": r["application_url"], "score": r["score"],
                "dedup_key": _dedup.dedup_key(r["company"], r["title"]),
                "linkedin_resolve_status": r["linkedin_resolve_status"],
                "linkedin_resolved_at": r["linkedin_resolved_at"],
                "linkedin_resolve_error": r["linkedin_resolve_error"],
                "linkedin_unresolved_kind": r["linkedin_unresolved_kind"],
                "linkedin_next_action": r["linkedin_next_action"],
                **_canonical_provenance(r),
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
  AND {missing_score_predicate}
  AND COALESCE(liveness_status, '') != 'dead'
  AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress', 'crash_unconfirmed'))
  AND COALESCE(apply_error, '') NOT IN ('no_confirmation', 'crash_unconfirmed')
  AND TRIM(COALESCE(company, '')) != ''
  AND TRIM(COALESCE(title, '')) != ''
  AND (CASE WHEN application_url LIKE 'http%' THEN application_url ELSE url END) LIKE '%linkedin.com%'
"""


def count_linkedin_unscored(sqlite_conn=None, *, include_research: bool = False) -> int:
    """Count apply-shaped LinkedIn postings excluded from the push SOLELY because they
    are unscored (audit_score AND fit_score both NULL). These are correctly NOT applied
    to -- you don't apply to a job you haven't scored for fit -- but the count makes the
    backlog VISIBLE instead of silently lost: run the scorer to fold them into the pool."""
    own = sqlite_conn is None
    sq = sqlite_conn or _home_conn()
    try:
        missing_score_predicate = "audit_score IS NULL AND fit_score IS NULL"
        if include_research:
            missing_score_predicate += " AND research_fit_score IS NULL"
        sql = _COUNT_LINKEDIN_UNSCORED.format(
            missing_score_predicate=missing_score_predicate,
        )
        return int(sq.execute(sql).fetchone()[0])
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
