"""Fleet email-verification reconcile (Phase 1). Match crash_unconfirmed apply jobs to
application-outcome emails (email_events) and flip confirmed ones to 'applied'. Advisory/
dry-run by default; writes only to the fleet Postgres. Reuses gmail_outcomes.match_email_to_job."""
from __future__ import annotations
from dataclasses import dataclass

from applypilot.gmail_outcomes import match_email_to_job

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


def load_crash_jobs(conn) -> list[dict]:
    """Read the crash_unconfirmed / no_result_line jobs and shape them as match_email_to_job
    candidates (site = apply_domain). Read-only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url, application_url, company, title, apply_domain "
            "FROM apply_queue WHERE status='crash_unconfirmed' AND apply_error='failed:no_result_line'"
        )
        out = []
        for r in cur.fetchall():
            out.append({
                "url": r["url"], "application_url": r["application_url"],
                "company": r["company"], "title": r["title"], "site": r["apply_domain"],
            })
    return out


def reconcile(emails: list, jobs: list[dict], *, min_strong: float = MIN_STRONG) -> ReconcileResult:
    """Match each outcome email to a crash job via the existing fuzzy matcher and classify the hit.
    A job is resolved at most once: the highest-scoring hit wins (a strong method scores 1.0)."""
    best: dict[str, Resolution] = {}   # job_url -> best Resolution
    unmatched = 0
    for e in emails:
        job, method, score = match_email_to_job(e.sender, e.subject, e.body, jobs)
        cls = classify_match(method, score, min_strong=min_strong)
        if job is None or cls is None:
            unmatched += 1
            continue
        url = job["url"]
        cand = Resolution(job_url=url, message_id=e.message_id, method=method, score=float(score),
                          stage=e.stage, occurred_at=e.occurred_at, classification=cls)
        prev = best.get(url)
        if prev is None or cand.score > prev.score:
            best[url] = cand
    confirmed = [r for r in best.values() if r.classification == "confirmed"]
    probable = [r for r in best.values() if r.classification == "probable"]
    return ReconcileResult(confirmed=confirmed, probable=probable,
                           unmatched_emails=unmatched, jobs_total=len(jobs))


def apply_resolutions(conn, result: ReconcileResult, *, include_probable: bool = False) -> dict:
    """Flip confirmed (and, if opted-in, probable) jobs crash_unconfirmed -> applied, guarded on
    the current status so it is idempotent and never clobbers a row another process moved. Writes
    one audit row per flip. One transaction per job."""
    targets = list(result.confirmed) + (list(result.probable) if include_probable else [])
    flipped = skipped = 0
    for r in targets:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET status='applied', apply_status='applied', apply_error=NULL, "
                "applied_at=COALESCE(applied_at, %s), updated_at=now() "
                "WHERE url=%s AND status='crash_unconfirmed'",
                (r.occurred_at, r.job_url),
            )
            if cur.rowcount == 0:
                skipped += 1
                continue
            cur.execute(
                "INSERT INTO email_reconcile_actions (url, message_id, match_method, match_score, "
                "stage, prior_status, how_to_reverse) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (r.job_url, r.message_id, r.method, r.score, r.stage, "crash_unconfirmed",
                 "Set apply_queue.status back to 'crash_unconfirmed', apply_status='crash_unconfirmed', "
                 "apply_error='failed:no_result_line' WHERE url matches."),
            )
            flipped += 1
        conn.commit()
    return {"flipped": flipped, "skipped": skipped}


def format_report(result: ReconcileResult) -> str:
    lines = [
        f"crash jobs considered: {result.jobs_total}",
        f"confirmed: {len(result.confirmed)}",
        f"probable: {len(result.probable)}",
        f"unmatched emails: {result.unmatched_emails}",
    ]
    for r in sorted(result.confirmed, key=lambda x: x.method):
        lines.append(f"  [confirmed] {r.method} {r.score:.2f} {r.stage} -> {r.job_url}")
    for r in sorted(result.probable, key=lambda x: -x.score):
        lines.append(f"  [probable]  {r.method} {r.score:.2f} {r.stage} -> {r.job_url}")
    return "\n".join(lines)
