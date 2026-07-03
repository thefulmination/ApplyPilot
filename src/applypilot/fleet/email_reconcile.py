"""Fleet email-verification reconcile (Phase 1). Match crash_unconfirmed apply jobs to
application-outcome emails (email_events) and flip confirmed ones to 'applied'. Advisory/
dry-run by default; writes only to the fleet Postgres. Reuses gmail_outcomes.match_email_to_job."""
from __future__ import annotations
import sqlite3
from dataclasses import dataclass

from applypilot.gmail_outcomes import match_email_to_job

CONFIRMING_STAGES = frozenset({"acknowledged", "screen", "assessment", "interview", "offer", "rejected"})
# Exact/near-exact methods that confirm a match regardless of score.
STRONG_METHODS = frozenset({"board_slug", "linkedin_job_id", "company_domain"})
# Fuzzy methods strong ENOUGH to auto-confirm when they clear MIN_STRONG. Only `ats_domain`
# qualifies: an ATS sender (Greenhouse/Lever/...) plus a matching extracted employer name is
# materially stronger than a bare token overlap. `company_name` and `title` are NOT here — a
# company/title token overlap can conflate same-company-different-role across a large candidate
# pool, and a wrong auto-flip permanently drops a wanted job from the apply surface. They are
# always classified "probable" (review-only via --apply-probable). See spec §6.
CONFIRMABLE_FUZZY_METHODS = frozenset({"ats_domain"})
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
    """confirmed if an exact-ish method, or a confirmable fuzzy method (ats_domain) clearing
    min_strong; probable for any other (weaker) fuzzy hit; None when there was no match at all.
    company_name/title are always probable — too collision-prone for an irreversible auto-flip."""
    if method is None:
        return None
    if method in STRONG_METHODS:
        return "confirmed"
    if method in CONFIRMABLE_FUZZY_METHODS and score is not None and score >= min_strong:
        return "confirmed"
    return "probable"


def load_outcome_emails(conn) -> list:
    """Read submission-proving outcome emails from the home brain's email_events table.
    Caller opens the sqlite connection read-only.
    Returns [] if the table does not exist (brain predates the outcomes tracker)."""
    placeholders = ",".join("?" for _ in CONFIRMING_STAGES)
    try:
        cur = conn.execute(
            f"SELECT message_id, sender, subject, body_text, company, title, job_url, stage, occurred_at "
            f"FROM email_events WHERE stage IN ({placeholders})",
            tuple(sorted(CONFIRMING_STAGES)),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
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
            "SELECT url, application_url, company, title, apply_domain, dedup_key "
            "FROM apply_queue WHERE status='crash_unconfirmed' AND apply_error='failed:no_result_line'"
        )
        out = []
        for r in cur.fetchall():
            out.append({
                "url": r["url"], "application_url": r["application_url"],
                "company": r["company"], "title": r["title"], "site": r["apply_domain"],
                "dedup_key": r["dedup_key"],
            })
    return out


def reconcile(emails: list, jobs: list[dict], *, min_strong: float = MIN_STRONG) -> ReconcileResult:
    """Match each outcome email to a crash job via the existing fuzzy matcher and classify the hit.
    A job is resolved at most once: the highest-scoring hit wins (a strong method scores 1.0)."""
    best: dict[str, Resolution] = {}   # job_url -> best Resolution
    unmatched = 0
    for e in emails:
        job, method, score = match_email_to_job(e.sender, e.subject, e.body, jobs).astuple()
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
        try:
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
                # The email that proved the flip IS the response -- mark applied_set.got_response
                # for feedback/response-rate reporting. applied_set has no url column; key via
                # apply_queue.dedup_key (the same join queue.write_apply_result uses to seed it).
                cur.execute(
                    "UPDATE applied_set SET got_response=true "
                    "WHERE dedup_key = (SELECT dedup_key FROM apply_queue WHERE url=%s)",
                    (r.job_url,),
                )
                flipped += 1
        except Exception:
            conn.rollback()
            raise
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
