"""Fleet email-verification reconcile (Phase 1). Match crash_unconfirmed apply jobs to
application-outcome emails (email_events) and flip confirmed ones to 'applied'. Advisory/
dry-run by default; writes only to the fleet Postgres. Reuses gmail_outcomes.match_email_to_job."""
from __future__ import annotations
from dataclasses import dataclass

CONFIRMING_STAGES = frozenset({"acknowledged", "screen", "assessment", "interview", "offer", "rejected"})
STRONG_METHODS = frozenset({"board_slug", "linkedin_job_id", "company_domain"})
MIN_STRONG = 0.6


@dataclass
class OutcomeEmail:
    message_id: str
    sender: str
    subject: str
    body: str
    company: str
    title: str
    job_url: str | None
    stage: str
    occurred_at: str | None


@dataclass
class Resolution:
    job_url: str
    message_id: str
    method: str
    score: float
    stage: str
    occurred_at: str | None
    classification: str  # "confirmed" | "probable"


@dataclass
class ReconcileResult:
    confirmed: list
    probable: list
    unmatched_emails: int
    jobs_total: int


def classify_match(method: str | None, score: float | None, *, min_strong: float = MIN_STRONG) -> str | None:
    """confirmed if a strong (exact-ish) method or a fuzzy score >= min_strong; probable for a
    weaker fuzzy hit; None when there was no match at all."""
    if method is None:
        return None
    if method in STRONG_METHODS or (score is not None and score >= min_strong):
        return "confirmed"
    return "probable"


def load_outcome_emails(conn) -> list:
    """Read submission-proving outcome emails from the home brain's email_events table.
    Caller opens the sqlite connection read-only."""
    placeholders = ",".join("?" for _ in CONFIRMING_STAGES)
    cur = conn.execute(
        f"SELECT message_id, sender, subject, body_text, company, title, job_url, stage, occurred_at "
        f"FROM email_events WHERE stage IN ({placeholders})",
        tuple(sorted(CONFIRMING_STAGES)),
    )
    out = []
    for r in cur.fetchall():
        out.append(OutcomeEmail(
            message_id=r[0], sender=r[1] or "", subject=r[2] or "", body=r[3] or "",
            company=r[4] or "", title=r[5] or "", job_url=r[6], stage=r[7], occurred_at=r[8],
        ))
    return out
