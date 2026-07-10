"""Postgres work-queue layer for the cloud apply fleet (queue-offload design).

See docs/superpowers/specs/2026-06-24-cloud-apply-fleet-design.md (S3a / S5 / S6).

This is the ONLY state that lives in the cloud: a thin offsite-apply work queue + result
mailbox on Railway Postgres. Home SQLite stays authoritative -- if Postgres is lost, home
re-pushes and nothing important is gone.

Every function takes an OPEN psycopg connection so callers control transaction scope and the
whole layer is trivially testable against a throwaway local Postgres. The production DSN comes
from DATABASE_URL (Railway injects it for the worker; the home box uses the public URL).

Concurrency contract (the part that must never break):
  * lease_one  -> FOR UPDATE SKIP LOCKED: N workers each grab a DISTINCT row, never blocking.
  * a crashed worker's lease expires -> reclaim parks it; a job that may have hit submit is
    pinned crash_unconfirmed and NEVER re-leased (no double-submit under Jonathan's name).
  * write_result is lease_owner-guarded: only the holder may close a row.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Sequence

import psycopg
from psycopg.rows import dict_row

from applypilot import config

_SCHEMA_SQL = (Path(__file__).with_name("fleet_schema.sql")).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def get_dsn(dsn: str | None = None) -> str:
    dsn = dsn or os.environ.get("DATABASE_URL") or os.environ.get("APPLYPILOT_FLEET_DSN")
    if not dsn:
        raise RuntimeError(
            "No Postgres DSN: set DATABASE_URL (Railway) or APPLYPILOT_FLEET_DSN."
        )
    return dsn


def connect(dsn: str | None = None, *, autocommit: bool = False) -> psycopg.Connection:
    """Open a dict-row psycopg connection to the fleet Postgres."""
    return psycopg.connect(get_dsn(dsn), autocommit=autocommit, row_factory=dict_row)


def ensure_schema(conn: psycopg.Connection) -> None:
    """Idempotently create apply_queue + fleet_config (+ indexes, seed config row)."""
    with conn.cursor() as cur:
        cur.execute(_SCHEMA_SQL)
    conn.commit()


# ---------------------------------------------------------------------------
# Lease  (worker pulls the top host-polite queued job, atomically)
# ---------------------------------------------------------------------------

_LEASE_SQL = """
WITH host_recent AS (
    SELECT apply_domain, MAX(last_attempted_at) AS last_at
    FROM apply_queue
    WHERE last_attempted_at > now() - make_interval(secs => %(politeness)s)
    GROUP BY apply_domain
),
next_job AS (
    SELECT q.url
    FROM apply_queue q
    LEFT JOIN host_recent hr ON hr.apply_domain = q.apply_domain
    WHERE q.status = 'queued'
      AND q.approved_batch IS NOT NULL
      AND NOT (
          LOWER(TRIM(COALESCE(q.company,''))) = ANY(%(blocked_names)s)
          OR q.url ILIKE ANY(%(blocked_pats)s)
          OR COALESCE(q.application_url,'') ILIKE ANY(%(blocked_pats)s)
      )
      AND (hr.last_at IS NULL
           OR hr.last_at < now() - make_interval(
                  secs => (%(politeness)s * (0.7 + random() * 0.7))::int))
    ORDER BY q.score DESC, q.url            -- url tie-break = deterministic, mirrors home
    LIMIT 1
    FOR UPDATE OF q SKIP LOCKED             -- N workers each grab a distinct row, no blocking
)
UPDATE apply_queue q
SET status            = 'leased',
    lease_owner       = %(worker)s,
    lease_expires_at  = now() + make_interval(secs => %(ttl)s),
    last_attempted_at = now(),
    attempts          = q.attempts + 1,
    updated_at        = now()
FROM next_job
WHERE q.url = next_job.url
RETURNING q.url, q.company, q.title, q.application_url, q.score, q.apply_domain, q.attempts;
"""


def lease_one(
    conn: psycopg.Connection,
    worker_id: str,
    *,
    ttl_seconds: int = 1200,
    politeness_seconds: int = 90,
) -> dict[str, Any] | None:
    """Atomically lease the top-score queued job whose apply_domain is outside the jittered
    politeness window. Bumps `attempts` AT lease time (so a row that has been handed to a worker
    is visibly attempt>=1). NOTE: attempts does NOT distinguish a never-launched lease from one
    that crashed mid-submit -- both stay at 1 -- so reclaim must park ALL expired leases
    conservatively rather than requeue on attempts (see reclaim_stale_leases). Returns the leased
    row dict, or None if nothing is leasable.

    politeness_seconds=0 disables host throttling (used by tests for determinism)."""
    blocked_names, blocked_pats = config.load_blocked_companies()
    with conn.cursor() as cur:
        cur.execute(
            _LEASE_SQL,
            {
                "worker": worker_id, "ttl": ttl_seconds, "politeness": politeness_seconds,
                "blocked_names": list(blocked_names), "blocked_pats": blocked_pats,
            },
        )
        row = cur.fetchone()
    conn.commit()
    return row


# ---------------------------------------------------------------------------
# Reclaim  (startup + periodic crash sweep)
# ---------------------------------------------------------------------------

_RECLAIM_SQL = """
WITH stale AS (
    SELECT url
    FROM apply_queue
    WHERE status = 'leased'
      AND lease_expires_at < now() - make_interval(secs => %(grace)s)
    FOR UPDATE SKIP LOCKED
)
UPDATE apply_queue q
SET status           = 'crash_unconfirmed'::apply_queue_status,
    apply_error      = 'crash_unconfirmed',
    attempts         = 99,
    lease_owner      = NULL,
    lease_expires_at = NULL,
    updated_at       = now()
FROM stale s
WHERE q.url = s.url
RETURNING q.url, q.status;
"""


def reclaim_stale_leases(conn: psycopg.Connection, *, grace_seconds: int = 30) -> list[dict[str, Any]]:
    """Sweep leased rows whose lease expired (a clean finish always writes terminal status, so
    an expired lease == a hard crash) and PARK every one as crash_unconfirmed (attempts=99),
    NEVER re-leased. Mirrors launcher.reclaim_stale_leases.

    Why park unconditionally (not requeue the 'pre-launch' ones): `attempts` is bumped ONCE,
    at lease time, so attempts=1 covers BOTH "leased but never launched" AND "leased, launched,
    crashed at the submit step before any terminal write" -- they are INDISTINGUISHABLE here.
    A live incident (2026-06-29) hit exactly the second case: a worker died at "Now submitting
    the application" with the row still at attempts=1, apply_error=NULL. Requeuing on attempts<=1
    would have applied a SECOND time under Jonathan's name. The owner's hard rule (NEVER
    double-apply) outranks the cost of re-reviewing the rare job killed genuinely pre-launch:
    crash_unconfirmed rows surface in apply-failures for a manual decision. Returns the reclaimed
    (url, status)."""
    with conn.cursor() as cur:
        cur.execute(_RECLAIM_SQL, {"grace": grace_seconds})
        rows = cur.fetchall()
    conn.commit()
    return rows


# ---------------------------------------------------------------------------
# Spend cap / kill switch
# ---------------------------------------------------------------------------

_HALT_SQL = """
SELECT
    fc.paused
    OR (fc.spend_cap_usd > 0
        AND COALESCE((SELECT SUM(cumulative_cost_usd) FROM apply_queue), 0) >= fc.spend_cap_usd)
        AS should_halt
FROM fleet_config fc
WHERE fc.id = 1;
"""


def should_halt(conn: psycopg.Connection) -> bool:
    """True if the fleet must stop leasing: globally paused OR cumulative spend >= cap.
    A soft pre-lease gate (spec S6) -- up to ~N in-flight jobs may overshoot, which is fine
    against the POC budget.

    NOTE (H1): this reads ONLY the shared kill switch (fleet_config.paused) + spend cap. It
    deliberately does NOT read ats_paused. The ATS lane uses ats_should_halt() instead
    (which OR-s in ats_paused); the LinkedIn lane uses linkedin_should_halt() to avoid
    reading apply_queue from the catastrophe lane's pre-flight tick."""
    with conn.cursor() as cur:
        cur.execute(_HALT_SQL)
        row = cur.fetchone()
    conn.rollback()  # read-only
    return bool(row["should_halt"]) if row else False


_LINKEDIN_HALT_SQL = """
SELECT COALESCE(fc.paused, FALSE) AS should_halt
FROM fleet_config fc
WHERE fc.id = 1;
"""


def linkedin_should_halt(conn: psycopg.Connection) -> bool:
    """LinkedIn-lane pre-lease halt gate.

    This intentionally reads only the shared operator kill switch. LinkedIn's lane-specific
    blast-radius controls (account halt, canary, daily cap, min-gap, and mutex) are enforced
    atomically by queue.lease_linkedin(). The pre-flight tick must not read apply_queue:
    that couples LinkedIn to ATS spend state and can deadlock with schema maintenance.
    """
    with conn.cursor() as cur:
        cur.execute(_LINKEDIN_HALT_SQL)
        row = cur.fetchone()
    conn.rollback()  # read-only
    return bool(row["should_halt"]) if row else False


_ATS_HALT_SQL = """
SELECT
    fc.paused
    OR COALESCE(fc.ats_paused, FALSE)
    OR (fc.spend_cap_usd > 0
        AND COALESCE((SELECT SUM(cumulative_cost_usd) FROM apply_queue), 0) >= fc.spend_cap_usd)
        AS should_halt
FROM fleet_config fc
WHERE fc.id = 1;
"""


def ats_should_halt(conn: psycopg.Connection) -> bool:
    """ATS-lane pre-lease halt gate: the shared kill switch (paused/spend cap) OR the
    Fleet Doctor's ATS-only ats_paused (H1). The LinkedIn worker must NEVER call this -- it
    uses linkedin_should_halt() so a Doctor ATS pause can never stop the LinkedIn lane."""
    with conn.cursor() as cur:
        cur.execute(_ATS_HALT_SQL)
        row = cur.fetchone()
    conn.rollback()  # read-only
    return bool(row["should_halt"]) if row else False


def set_ats_paused(conn: psycopg.Connection, paused: bool, *, source: str | None = None) -> None:
    """Set the ATS-only pause flag (H1). The Fleet Doctor routes its lane-pause HERE, never to
    set_paused(). source ('doctor'|NULL) records provenance so the console can label it and the
    Doctor only ever auto-reverts its OWN pause. Clearing the pause also clears the source."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET ats_paused = %s, "
            "ats_pause_source = CASE WHEN %s THEN %s ELSE NULL END, updated_at = now() WHERE id = 1",
            (paused, paused, source),
        )
    conn.commit()


def set_spend_cap(conn: psycopg.Connection, cap_usd: float) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET spend_cap_usd = %s, updated_at = now() WHERE id = 1",
            (cap_usd,),
        )
    conn.commit()


def set_paused(conn: psycopg.Connection, paused: bool) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fleet_config SET paused = %s, updated_at = now() WHERE id = 1",
            (paused,),
        )
    conn.commit()


# ---------------------------------------------------------------------------
# Result write  (releases the lease; lease_owner-guarded)
# ---------------------------------------------------------------------------

_RESULT_SQL = """
UPDATE apply_queue
SET status                  = %(status)s::apply_queue_status,
    apply_status            = %(apply_status)s,
    apply_error             = %(apply_error)s,
    verification_confidence = %(verification_confidence)s,
    agent_model             = %(agent_model)s,
    est_cost_usd            = %(est_cost_usd)s,
    cumulative_cost_usd     = COALESCE(cumulative_cost_usd, 0) + %(est_cost_usd)s,
    applied_at              = CASE WHEN %(status)s = 'applied' THEN now() ELSE applied_at END,
    worker_id               = %(worker)s,
    apply_duration_ms       = %(apply_duration_ms)s,
    lease_owner             = NULL,
    lease_expires_at        = NULL,
    updated_at              = now()
WHERE url = %(url)s
  AND lease_owner = %(worker)s          -- only the lease holder may close it
RETURNING url;
"""


def write_result(
    conn: psycopg.Connection,
    worker_id: str,
    url: str,
    *,
    status: str,
    apply_status: str | None = None,
    apply_error: str | None = None,
    verification_confidence: str | None = None,
    agent_model: str | None = None,
    est_cost_usd: float | None = 0,
    apply_duration_ms: int | None = None,
) -> bool:
    """Write a terminal result and release the lease. est_cost_usd is written UNCONDITIONALLY
    (0 when the CLI reports none) so the cap SUM stays consistent. Returns True only if this
    worker held the lease (a reclaimed row, now owned by no one, is never clobbered)."""
    if est_cost_usd is None:
        est_cost_usd = 0
    with conn.cursor() as cur:
        cur.execute(
            _RESULT_SQL,
            {
                "status": status,
                "apply_status": apply_status,
                "apply_error": apply_error,
                "verification_confidence": verification_confidence,
                "agent_model": agent_model,
                "est_cost_usd": est_cost_usd,
                "worker": worker_id,
                "apply_duration_ms": apply_duration_ms,
                "url": url,
            },
        )
        landed = cur.fetchone() is not None
    conn.commit()
    return landed


# ---------------------------------------------------------------------------
# Re-queue  (a RETRYABLE non-submit: back to 'queued', lease released; lease_owner-guarded)
# ---------------------------------------------------------------------------

_REQUEUE_SQL = """
UPDATE apply_queue
SET status           = 'queued'::apply_queue_status,
    apply_error      = %(apply_error)s,
    -- Undo the lease-time attempt bump: a usage/quota wall hit on turn 1 PROVABLY never
    -- touched the page, so this lease did nothing -- don't penalize the job. NOT pinned to
    -- 99 (that's crash_unconfirmed / reclaim). GREATEST guards against underflow.
    attempts         = GREATEST(attempts - 1, 0),
    lease_owner      = NULL,
    lease_expires_at = NULL,
    updated_at       = now()
WHERE url = %(url)s
  AND lease_owner = %(worker)s          -- only the lease holder may re-queue it
RETURNING url;
"""


def requeue_job(
    conn: psycopg.Connection,
    worker_id: str,
    url: str,
    *,
    apply_error: str | None = None,
) -> bool:
    """Return a leased job to 'queued' so it can be re-leased -- the RETRYABLE counterpart
    to write_result. Use ONLY when the run provably never touched the application form
    (e.g. an agent usage/quota wall hit before any browser tool call): this re-queue can
    NEVER cause a double-submit. attempts is NOT pinned (the lease bump is undone). Mirrors
    write_result's lease_owner guard, so a reclaimed/foreign row is never clobbered. Returns
    True only if this worker held the lease."""
    with conn.cursor() as cur:
        cur.execute(
            _REQUEUE_SQL,
            {"apply_error": apply_error, "worker": worker_id, "url": url},
        )
        landed = cur.fetchone() is not None
    conn.commit()
    return landed


# ---------------------------------------------------------------------------
# PUSH / PULL sync surface (home box drives these; see fleet_sync.py)
# ---------------------------------------------------------------------------

_PUSH_SQL = """
INSERT INTO apply_queue (url, company, title, application_url, score, apply_domain, status)
VALUES (%(url)s, %(company)s, %(title)s, %(application_url)s, %(score)s, %(apply_domain)s, 'queued')
ON CONFLICT (url) DO UPDATE
SET company         = EXCLUDED.company,
    title           = EXCLUDED.title,
    application_url = EXCLUDED.application_url,
    score           = EXCLUDED.score,
    apply_domain    = EXCLUDED.apply_domain,
    updated_at      = now()
WHERE apply_queue.status = 'queued';   -- only refresh+requeue still-pending rows
"""


def push_jobs(conn: psycopg.Connection, rows: Iterable[dict[str, Any]]) -> int:
    """Idempotently UPSERT offsite-eligible jobs by url (spec S3b). Re-runnable; never disturbs
    a leased/terminal row. `rows` need keys: url, company, title, application_url, score,
    apply_domain. Returns the number of rows submitted."""
    rows = list(rows)
    if not rows:
        return 0
    with conn.cursor() as cur:
        cur.executemany(_PUSH_SQL, rows)
    conn.commit()
    return len(rows)


# PULL works for both lanes: linkedin_queue is schema-identical to apply_queue
# (LIKE apply_queue). Table names come ONLY from this allowlist -- never from input.
_PULL_TABLES = ("apply_queue", "linkedin_queue")

_PULL_SQL = """
SELECT url, status, apply_status, apply_error, verification_confidence,
       agent_model, est_cost_usd, applied_at, worker_id, apply_duration_ms
FROM {table}
WHERE status IN ('applied','failed','blocked','crash_unconfirmed')
  AND synced_to_home_at IS NULL
ORDER BY updated_at
LIMIT %(limit)s;
"""


def _check_pull_table(table: str) -> str:
    if table not in _PULL_TABLES:
        raise ValueError(f"unknown pull table {table!r} (allowed: {_PULL_TABLES})")
    return table


def fetch_pending_results(conn: psycopg.Connection, *, limit: int = 500,
                          table: str = "apply_queue") -> list[dict[str, Any]]:
    """PULL step A: terminal rows not yet ingested into home SQLite."""
    with conn.cursor() as cur:
        cur.execute(_PULL_SQL.format(table=_check_pull_table(table)), {"limit": limit})
        rows = cur.fetchall()
    conn.rollback()
    return rows


def mark_synced(conn: psycopg.Connection, url: str, *, table: str = "apply_queue") -> None:
    """Stamp a row ingested-home so PULL is idempotent."""
    with conn.cursor() as cur:
        cur.execute(f"UPDATE {_check_pull_table(table)} SET synced_to_home_at = now() WHERE url = %s",
                    (url,))
    conn.commit()


# ---------------------------------------------------------------------------
# Observability  (POC metrics, spec S7)
# ---------------------------------------------------------------------------

_STATS_SQL = """
SELECT
    COUNT(*) FILTER (WHERE status='applied')                                              AS applied,
    COUNT(*) FILTER (WHERE status IN ('applied','failed','blocked','crash_unconfirmed'))  AS attempted,
    COUNT(*) FILTER (WHERE status='queued')                                               AS queued,
    COUNT(*) FILTER (WHERE status='leased')                                               AS leased,
    COUNT(*) FILTER (WHERE status='blocked' OR apply_error ILIKE '%%captcha%%')           AS blocked_or_captcha,
    COUNT(*) FILTER (WHERE status='crash_unconfirmed')                                    AS crash_unconfirmed,
    ROUND(100.0 * COUNT(*) FILTER (WHERE status='applied')
          / NULLIF(COUNT(*) FILTER (WHERE status IN ('applied','failed','blocked','crash_unconfirmed')),0), 1) AS success_pct,
    COALESCE(SUM(cumulative_cost_usd),0)                                                   AS total_cost,
    ROUND(COALESCE(SUM(cumulative_cost_usd),0)
          / NULLIF(COUNT(*) FILTER (WHERE status='applied'),0), 4)                         AS cost_per_apply
FROM apply_queue
{group};
"""


def put_asset(conn: psycopg.Connection, name: str, data: bytes) -> None:
    """Store a worker asset (profile.json / resume.pdf) in Postgres for the fleet to hydrate."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_assets (name, data) VALUES (%s, %s) "
            "ON CONFLICT (name) DO UPDATE SET data = EXCLUDED.data, updated_at = now()",
            (name, data),
        )
    conn.commit()


def get_asset(conn: psycopg.Connection, name: str) -> bytes | None:
    with conn.cursor() as cur:
        cur.execute("SELECT data FROM fleet_assets WHERE name = %s", (name,))
        row = cur.fetchone()
    conn.rollback()
    return bytes(row["data"]) if row else None


def queue_stats(conn: psycopg.Connection, *, by_model: bool = False) -> list[dict[str, Any]]:
    """POC metrics (spec S7). by_model=True breaks the numbers down by agent_model for the
    Sonnet-vs-DeepSeek A/B."""
    if by_model:
        sql = _STATS_SQL.replace("FROM apply_queue\n{group}", "FROM apply_queue\nGROUP BY agent_model")
        sql = sql.replace("SELECT\n", "SELECT\n    agent_model,\n")
    else:
        sql = _STATS_SQL.format(group="")
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    conn.rollback()
    return rows
