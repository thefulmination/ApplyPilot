"""Home-side PUSH/PULL bridge between the authoritative SQLite and the fleet Postgres queue.

  PUSH: offsite-eligible jobs (SQLite) -> apply_queue (Postgres), idempotent by url.
  PULL: terminal results (Postgres) -> home jobs rows (SQLite), idempotent, never demotes a
        confirmed apply.

Only ~6 routing columns ever leave home, and url + status + cost + timing come back. The
60-col jobs schema, the user profile, and local file paths NEVER cross. See spec S3b / S4.

Eligibility mirrors acquire_job: the SQL handles score/liveness/applied/offsite-non-LinkedIn
+ posting-level dedup; the Python pass drops auth-gated / unresolved-aggregator / manual-ATS
targets (host logic that isn't SQL-expressible) and computes the politeness apply_domain.
"""
from __future__ import annotations

import sqlite3
from typing import Any

from applypilot import config
from applypilot.apply import pgqueue
from applypilot.apply.launcher import _apply_target, _throttle_host
from applypilot.database import THIN_DESCRIPTION_CHARS

# Blocked sites/patterns loaded once at import (mirrors acquire_job in launcher.py).
_BLOCKED_SITES, _BLOCKED_PATTERNS = config.load_blocked_sites()
_BLOCKED_COMPANY_NAMES, _BLOCKED_COMPANY_PATTERNS = config.load_blocked_companies()

# --- PUSH -------------------------------------------------------------------

_PUSH_SELECT = """
SELECT url, company, title, application_url,
       CAST(COALESCE(audit_score, fit_score) AS REAL) AS score
FROM jobs
WHERE duplicate_of_url IS NULL
  AND COALESCE(audit_score, fit_score) >= ?
  AND COALESCE(liveness_status, '') != 'dead'
  AND LENGTH(COALESCE(full_description,'')) >= {thin_description_chars}
  AND (apply_status IS NULL OR apply_status NOT IN ('applied', 'in_progress'))
  -- offsite ATS only: a real http(s) target, never LinkedIn (no cookies offsite)
  AND application_url LIKE 'http%'
  AND application_url NOT LIKE '%linkedin.com%'
  -- posting-level dedup: never queue a target already applied/in-flight or possibly-submitted
  AND COALESCE(application_url, url) NOT IN (
        SELECT COALESCE(application_url, url) FROM jobs
        WHERE apply_status IN ('applied', 'in_progress')
           OR apply_error IN ('no_confirmation', 'crash_unconfirmed')
  )
ORDER BY score DESC
"""


def _target(row: sqlite3.Row) -> str:
    return _apply_target({"application_url": row["application_url"], "url": row["url"]})


def _eligible(row: sqlite3.Row) -> bool:
    """Host-logic filters that SQL can't express (mirror acquire_job)."""
    t = _target(row)
    if config.is_auth_gated_application(t) or config.is_unresolved_aggregator(t) or config.is_manual_ats(t):
        return False
    # Mirror the blocked-site/pattern check from acquire_job so the fleet push lane
    # never queues jobs the home lane would skip (e.g. careers.google.com, amazon.jobs).
    t_lower = t.lower()
    if _BLOCKED_SITES and any(s.lower() in t_lower for s in _BLOCKED_SITES):
        return False
    if _BLOCKED_PATTERNS:
        for pat in _BLOCKED_PATTERNS:
            needle = pat.strip("%").lower()
            if needle and needle in t_lower:
                return False
    company = (row["company"] or "").strip().lower()
    if company and company in _BLOCKED_COMPANY_NAMES:
        return False
    url_lower = (row["url"] or "").lower()
    app_lower = (row["application_url"] or "").lower()
    for pat in _BLOCKED_COMPANY_PATTERNS:
        needle = pat.strip("%").lower()
        if needle and (needle in url_lower or needle in app_lower):
            return False
    return True


def push_offsite_jobs(
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    score_floor: int = 7,
    limit: int | None = None,
) -> int:
    """Retired score-only push path; canonical v3 sync is the only authority."""
    del sqlite_conn, pg_conn, score_floor, limit
    raise RuntimeError(
        "apply.fleet_sync.push_offsite_jobs is disabled; use "
        "fleet.sync.push_apply_eligible with canonical decisions"
    )


def _retired_push_offsite_jobs(
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    score_floor: int = 7,
    limit: int | None = None,
) -> int:
    own_sq, own_pg = sqlite_conn is None, pg_conn is None
    sq = sqlite_conn or _home_conn()
    pg = pg_conn or pgqueue.connect()
    try:
        out: list[dict[str, Any]] = []
        sql = _PUSH_SELECT.format(thin_description_chars=THIN_DESCRIPTION_CHARS)
        for r in sq.execute(sql, (score_floor,)).fetchall():
            if not _eligible(r):
                continue
            out.append({
                "url": r["url"], "company": r["company"], "title": r["title"],
                "application_url": r["application_url"], "score": r["score"],
                "apply_domain": _throttle_host(_target(r)),
            })
            if limit and len(out) >= limit:
                break
        return pgqueue.push_jobs(pg, out)
    finally:
        if own_sq:
            sq.close()
        if own_pg:
            pg.close()


# --- PULL -------------------------------------------------------------------

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

# failed / blocked / crash_unconfirmed: pin attempts so the home loop never re-acquires the
# posting; map blocked -> failed; keep crash_unconfirmed (it drives posting-level dedup).
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


def pull_results(
    *,
    sqlite_conn: sqlite3.Connection | None = None,
    pg_conn: Any | None = None,
    batch: int = 500,
) -> dict[str, int]:
    """Ingest terminal fleet results into home SQLite, idempotently. Returns a status->count
    summary. Per row: write home FIRST, then stamp the PG row synced -- a crash in between
    just re-pulls it next time (the WHERE guards make the replay a no-op)."""
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


# --- connection -------------------------------------------------------------

def _home_conn() -> sqlite3.Connection:
    """Open an isolated connection to the authoritative home SQLite (WAL-friendly, not the
    app's shared singleton, so the sync never contends with a live apply run's connection)."""
    conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


# --- home-side fleet ops CLI ------------------------------------------------
# Run on the home box with DATABASE_URL = the Railway Postgres PUBLIC url, e.g.:
#   DATABASE_URL=postgres://... python -m applypilot.apply.fleet_sync push --limit 800
def _cli() -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="fleet_sync", description="Cloud apply-fleet home ops.")
    ap.add_argument("cmd", choices=["migrate", "set-cap", "push", "pull", "stats",
                                    "pause", "resume", "upload-assets"])
    ap.add_argument("--cap", type=float, default=None, help="spend cap USD (set-cap)")
    ap.add_argument("--score-floor", type=int, default=7, help="min score to push")
    ap.add_argument("--limit", type=int, default=None, help="max jobs to push")
    a = ap.parse_args()

    pg = pgqueue.connect()
    try:
        if a.cmd == "migrate":
            pgqueue.ensure_schema(pg); print("schema ensured (apply_queue + fleet_config)")
        elif a.cmd == "set-cap":
            if a.cap is None:
                ap.error("--cap is required for set-cap")
            pgqueue.set_spend_cap(pg, a.cap); print(f"spend_cap_usd = ${a.cap:.2f}")
        elif a.cmd == "push":
            n = push_offsite_jobs(pg_conn=pg, score_floor=a.score_floor, limit=a.limit)
            print(f"pushed {n} offsite-eligible jobs to the queue")
        elif a.cmd == "pull":
            print("pulled results:", pull_results(pg_conn=pg))
        elif a.cmd == "stats":
            for row in pgqueue.queue_stats(pg):
                print(row)
        elif a.cmd == "pause":
            pgqueue.set_paused(pg, True); print("fleet PAUSED (workers will drain + stop)")
        elif a.cmd == "resume":
            pgqueue.set_paused(pg, False); print("fleet resumed")
        elif a.cmd == "upload-assets":
            import pathlib
            appdir = pathlib.Path(str(config.APP_DIR))
            for fname in ("profile.json", "resume.pdf"):
                fp = appdir / fname
                if fp.exists():
                    pgqueue.put_asset(pg, fname, fp.read_bytes())
                    print(f"uploaded {fname} ({fp.stat().st_size} bytes)")
                else:
                    print(f"MISSING {fp} -- skipped")
    finally:
        pg.close()
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(_cli())
