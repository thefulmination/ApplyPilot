"""Fleet remediator (Phase 2 auto-fixer). Deterministically RE-QUEUES usage-limit-casualty jobs
(provably never submitted) behind a 3-layer double-apply guard. Expansionary action lives ONLY
here (the Doctor stays conservative-pure; the diagnoser stays advisory-pure). No LLM, $0/pass.

Safety: a job is re-queued only if ALL pass -- (1) its worker has a Tier-0 usage_limit diagnosis,
(2) its dedup_key is NOT in applied_set, (3) NO confirming email_events row for its url. ATS only."""
from __future__ import annotations
from dataclasses import dataclass

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
