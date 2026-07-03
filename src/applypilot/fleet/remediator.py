"""Fleet remediator (Phase 2 auto-fixer). Deterministically RE-QUEUES usage-limit-casualty jobs
(provably never submitted) behind a 3-layer double-apply guard. Expansionary action lives ONLY
here (the Doctor stays conservative-pure; the diagnoser stays advisory-pure). No LLM, $0/pass.

Safety: a job is re-queued only if ALL pass -- (1) the job's OWN apply_error contains
'usage_limit' (PROVABLY never touched the form), (2) its dedup_key is NOT in applied_set,
(3) NO confirming email_events row for its url. ATS only."""
from __future__ import annotations
from dataclasses import dataclass
import sqlite3

REQUEUE_TAG = "requeued_by_remediator:usage_limit"


@dataclass
class Candidate:
    url: str
    worker_id: str
    dedup_key: str | None
    status: str
    attempts: int
    apply_error: str | None
    reason: str


def ensure_remediation_table(conn) -> None:
    """Create the audit/reversal table (additive, idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS remediation_actions ("
            "  id              BIGSERIAL PRIMARY KEY,"
            "  url             TEXT,"
            "  worker_id       TEXT,"
            "  action          TEXT,"
            "  reason          TEXT,"
            "  prior_status    TEXT,"
            "  prior_attempts  INTEGER,"
            "  prior_apply_error TEXT,"
            "  how_to_reverse  TEXT,"
            "  created_at      TIMESTAMPTZ NOT NULL DEFAULT now()"
            ")")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_remediation_url ON remediation_actions (url)")
    conn.commit()


def in_applied_set(conn, dedup_key: str | None) -> bool:
    """Guard 2 (internal ground truth): True if this job's dedup_key is already applied."""
    if not dedup_key:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM applied_set WHERE dedup_key=%s LIMIT 1", (dedup_key,))
        return cur.fetchone() is not None


def has_confirming_email(brain_path: str, url: str) -> bool:
    """Guard 3 (external ground truth): True if a recruiter email is tied to this job's url.
    Graceful: a missing brain file or absent email_events table returns False (NO veto), so the
    guarantee never drops below guards 1-2. Read-only.
    # Deliberately reads ALL email_events incl. quarantined: negative evidence stays conservative."""
    try:
        conn = sqlite3.connect(f"file:{brain_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM email_events WHERE job_url=? LIMIT 1", (url,)).fetchone()
        return row is not None
    except sqlite3.Error:
        return False  # email_events not created yet (outcomes-tracker not run) or corrupt brain
    finally:
        conn.close()


_CANDIDATE_SQL = """
SELECT q.url, q.worker_id, q.dedup_key, q.status::text AS status, q.attempts,
       q.apply_error, 'usage_limit' AS reason
FROM apply_queue q
WHERE q.lane = 'ats'
  AND q.status = 'failed'
  AND q.apply_error ILIKE '%%usage_limit%%'
  AND q.dedup_key IS NOT NULL
  AND q.updated_at > now() - make_interval(mins => %(window)s)
  AND (SELECT count(*) FROM remediation_actions ra
       WHERE ra.url = q.url AND ra.action = 'requeue') < %(maxperjob)s
ORDER BY q.updated_at DESC
LIMIT %(hardlimit)s
"""


def select_candidates(conn, *, window_minutes: int = 30, max_per_job: int = 2,
                      hard_limit: int = 500) -> list[Candidate]:
    """Usage-limit casualties: ATS jobs whose own apply_error contains 'usage_limit'
    (PROVABLY never touched the form — status='failed', not crash_unconfirmed/no_result_line),
    within the update window, not yet re-queued max_per_job times. The double-apply guards
    run later, per-candidate."""
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_SQL, {"window": window_minutes, "maxperjob": max_per_job,
                                     "hardlimit": hard_limit})
        return [Candidate(url=r["url"], worker_id=r["worker_id"], dedup_key=r["dedup_key"],
                          status=r["status"], attempts=r["attempts"],
                          apply_error=r["apply_error"], reason=r["reason"])
                for r in cur.fetchall()]


# Phase 2.4 / C12: status-keyed backfill, deliberately with NO time window. A usage_limit hit
# means the agent's OWN turn died at its quota wall before any browser tool call -- that fact is
# permanent, not time-sensitive, so an old row is exactly as provably-never-submitted as a fresh
# one. Identical WHERE clause to _CANDIDATE_SQL (status='failed' + apply_error ILIKE
# '%usage_limit%' + dedup_key IS NOT NULL + lane='ats' + per-job cap) MINUS the updated_at
# window -- so it inherits the SAME may-have-submitted exclusion (crash_unconfirmed/
# no_result_line are a different status and never match `status = 'failed'`).
_BACKFILL_SQL = """
SELECT q.url, q.worker_id, q.dedup_key, q.status::text AS status, q.attempts,
       q.apply_error, 'usage_limit_backfill' AS reason
FROM apply_queue q
WHERE q.lane = 'ats'
  AND q.status = 'failed'
  AND q.apply_error ILIKE '%%usage_limit%%'
  AND q.dedup_key IS NOT NULL
  AND (SELECT count(*) FROM remediation_actions ra
       WHERE ra.url = q.url AND ra.action = 'requeue') < %(maxperjob)s
ORDER BY q.updated_at ASC
LIMIT %(hardlimit)s
"""


def select_backfill_candidates(conn, *, max_per_job: int = 2,
                               hard_limit: int = 500) -> list[Candidate]:
    """Status-keyed counterpart to select_candidates with NO time window: selects every
    provably-never-submitted usage-limit casualty regardless of age (C12 -- the live casualties
    are ~62h old and the windowed query, even at a generous 720 min, selects zero). Same
    may-have-submitted exclusion as select_candidates (status='failed' only); the double-apply
    guards (in_applied_set / has_confirming_email) still run later, per-candidate, unchanged."""
    with conn.cursor() as cur:
        cur.execute(_BACKFILL_SQL, {"maxperjob": max_per_job, "hardlimit": hard_limit})
        return [Candidate(url=r["url"], worker_id=r["worker_id"], dedup_key=r["dedup_key"],
                          status=r["status"], attempts=r["attempts"],
                          apply_error=r["apply_error"], reason=r["reason"])
                for r in cur.fetchall()]


def requeue_job(conn, c: Candidate) -> bool:
    """Reverse the reclaim park for ONE proven-never-submitted job: status -> 'queued', attempts
    -> 0, lease cleared, apply_error tagged. Race-guarded on the prior status. Writes a reversal
    audit row. Caller MUST have passed all 3 guards before calling this. Returns True if updated.

    Phase 2.4 / C12: also deletes this job's OWN applied_set row (by dedup_key), in the SAME
    transaction. queue.write_apply_result seeds applied_set (keyed by dedup_key) whenever a job
    lands on status='applied' or 'crash_unconfirmed'; a job can cycle through crash_unconfirmed
    on one lease (seeding applied_set) and land on status='failed'+usage_limit on a LATER lease
    of the SAME dedup_key -- select_candidates only inspects the row's CURRENT status/apply_error,
    so such a row can be a legitimate candidate here. Without this delete, the lease query
    (queue.py's NOT EXISTS applied_set check) would treat the freshly re-queued row as
    already-applied and it would never be re-leased. Only THIS candidate's own dedup_key is
    touched -- every other applied_set row (a real double-apply guard) is left alone."""
    how_to_reverse = (f"UPDATE apply_queue SET status='{c.status}', attempts={c.attempts}, "
                      f"apply_error={c.apply_error!r} WHERE url={c.url!r};")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE apply_queue "
            "SET status='queued'::apply_queue_status, attempts=0, lease_owner=NULL, "
            "    lease_expires_at=NULL, apply_error=%(tag)s, updated_at=now() "
            "WHERE url=%(url)s AND status=%(prior)s::apply_queue_status",
            {"tag": REQUEUE_TAG, "url": c.url, "prior": c.status})
        if cur.rowcount != 1:
            return False  # status changed since selection (race) -> do nothing
        if c.dedup_key:
            cur.execute("DELETE FROM applied_set WHERE dedup_key=%s", (c.dedup_key,))
        cur.execute(
            "INSERT INTO remediation_actions (url, worker_id, action, reason, prior_status, "
            "prior_attempts, prior_apply_error, how_to_reverse) "
            "VALUES (%s,%s,'requeue',%s,%s,%s,%s,%s)",
            (c.url, c.worker_id, c.reason, c.status, c.attempts, c.apply_error, how_to_reverse))
    conn.commit()
    return True


def remediate(conn, *, brain_path: str | None = None, max_requeue: int = 50,
              max_per_job: int = 2, window_minutes: int = 30, backfill: bool = False) -> dict:
    """One pass: select usage-limit casualties, then re-queue each ONLY if all 3 guards pass,
    bounded by max_requeue. Guard failures and cap overflow are left parked (a recommendation,
    not an action). Returns a summary. brain_path defaults to the live brain (config.DB_PATH).

    backfill=True (Phase 2.4 / C12) swaps the candidate query to select_backfill_candidates
    (status-keyed, NO time window) instead of the windowed select_candidates -- everything
    downstream (guards 2/3, the per-pass cap, requeue_job's applied_set cleanup) is IDENTICAL,
    so a backfill pass can never be less safe than a windowed pass, only wider in what it looks
    at chronologically."""
    if brain_path is None:
        from applypilot.config import DB_PATH
        brain_path = str(DB_PATH)
    ensure_remediation_table(conn)
    if backfill:
        cands = select_backfill_candidates(conn, max_per_job=max_per_job)
    else:
        cands = select_candidates(conn, window_minutes=window_minutes, max_per_job=max_per_job)
    out = {"candidates": len(cands), "requeued": 0, "vetoed_applied_set": 0,
           "vetoed_email": 0, "capped": 0}
    for c in cands:
        if in_applied_set(conn, c.dedup_key):           # guard 2
            out["vetoed_applied_set"] += 1
            continue
        if has_confirming_email(brain_path, c.url):     # guard 3
            out["vetoed_email"] += 1
            continue
        if out["requeued"] >= max_requeue:              # per-pass blast-radius cap
            out["capped"] += 1
            continue
        if requeue_job(conn, c):                        # guard 1 already satisfied by selection
            out["requeued"] += 1
    return out
