"""Fleet remediator (Phase 2 auto-fixer). Deterministically RE-QUEUES usage-limit-casualty jobs
(provably never submitted) behind a 3-layer double-apply guard. Expansionary action lives ONLY
here (the Doctor stays conservative-pure; the diagnoser stays advisory-pure). No LLM, $0/pass.

Safety: a job is re-queued only if ALL pass -- (1) its worker has a Tier-0 usage_limit diagnosis,
(2) its dedup_key is NOT in applied_set, (3) NO confirming email_events row for its url. ATS only."""
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
    guarantee never drops below guards 1-2. Read-only."""
    try:
        conn = sqlite3.connect(f"file:{brain_path}?mode=ro", uri=True)
    except sqlite3.OperationalError:
        return False
    try:
        row = conn.execute(
            "SELECT 1 FROM email_events WHERE job_url=? LIMIT 1", (url,)).fetchone()
        return row is not None
    except sqlite3.OperationalError:
        return False  # email_events not created yet (outcomes-tracker not run)
    finally:
        conn.close()


_CANDIDATE_SQL = """
SELECT q.url, q.worker_id, q.dedup_key, q.status::text AS status, q.attempts,
       q.apply_error, 'usage_limit' AS reason
FROM apply_queue q
JOIN (
    SELECT DISTINCT machine FROM fleet_diagnoses
    WHERE reason = 'usage_limit' AND cluster_key LIKE 'logdiag:%%'
      AND status IN ('recommended', 'open', 'auto_applied')
      AND created_at > now() - make_interval(mins => %(window)s)
) d ON d.machine = q.worker_id
WHERE q.lane = 'ats'
  AND q.status IN ('failed', 'crash_unconfirmed')
  AND (q.status = 'crash_unconfirmed' OR q.apply_error ILIKE '%%no_result_line%%')
  AND q.updated_at > now() - make_interval(mins => %(window)s)
  AND (SELECT count(*) FROM remediation_actions ra
       WHERE ra.url = q.url AND ra.action = 'requeue') < %(maxperjob)s
ORDER BY q.updated_at DESC
LIMIT %(hardlimit)s
"""


def select_candidates(conn, *, window_minutes: int = 30, max_per_job: int = 2,
                      hard_limit: int = 500) -> list[Candidate]:
    """Usage-limit casualties: ATS jobs parked/failed (no_result_line / crash_unconfirmed) by a
    worker that has a recent Tier-0 usage_limit diagnosis, within the diagnosis window, not yet
    re-queued max_per_job times. The double-apply guards run later, per-candidate."""
    with conn.cursor() as cur:
        cur.execute(_CANDIDATE_SQL, {"window": window_minutes, "maxperjob": max_per_job,
                                     "hardlimit": hard_limit})
        return [Candidate(url=r["url"], worker_id=r["worker_id"], dedup_key=r["dedup_key"],
                          status=r["status"], attempts=r["attempts"],
                          apply_error=r["apply_error"], reason=r["reason"])
                for r in cur.fetchall()]


def requeue_job(conn, c: Candidate) -> bool:
    """Reverse the reclaim park for ONE proven-never-submitted job: status -> 'queued', attempts
    -> 0, lease cleared, apply_error tagged. Race-guarded on the prior status. Writes a reversal
    audit row. Caller MUST have passed all 3 guards before calling this. Returns True if updated."""
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
        cur.execute(
            "INSERT INTO remediation_actions (url, worker_id, action, reason, prior_status, "
            "prior_attempts, prior_apply_error, how_to_reverse) "
            "VALUES (%s,%s,'requeue',%s,%s,%s,%s,%s)",
            (c.url, c.worker_id, c.reason, c.status, c.attempts, c.apply_error, how_to_reverse))
    conn.commit()
    return True
