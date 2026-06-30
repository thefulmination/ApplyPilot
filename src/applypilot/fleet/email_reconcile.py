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
