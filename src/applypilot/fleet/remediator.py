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
