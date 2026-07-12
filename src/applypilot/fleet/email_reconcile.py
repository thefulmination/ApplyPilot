"""Fleet email-verification reconcile (Phase 1). Match crash_unconfirmed apply jobs to
application-outcome emails (email_events) and flip confirmed ones to 'applied'. Advisory/
dry-run by default; writes only to the fleet Postgres. Reuses gmail_outcomes.match_email_to_job."""
from __future__ import annotations
import re
import sqlite3
from dataclasses import dataclass
from collections.abc import Iterable, Mapping

from applypilot.gmail_outcomes import match_email_to_job
from applypilot.fleet.queue import resolve_superseded_challenges

CONFIRMING_STAGES = frozenset({"acknowledged", "screen", "assessment", "interview", "offer", "rejected"})
# Exact per-job methods that confirm a match regardless of score. Bare company_domain is
# review-only: LinkedIn/Indeed/company career domains can hold many simultaneous crash rows.
STRONG_METHODS = frozenset({"exact_job_url", "board_slug", "linkedin_job_id"})
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
    job_company: str | None = None
    job_title: str | None = None
    email_sender: str | None = None
    email_subject: str | None = None


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


_COMPANY_TOKEN_STOP = frozenset({
    "the", "a", "an", "and", "of", "for", "at", "inc", "incorporated", "llc", "ltd",
    "co", "corp", "corporation", "company", "group", "holdings", "systems",
    "ai", "bank", "global", "health", "markets", "medical", "network", "space",
})
_TITLE_TOKEN_STOP = frozenset({"the", "a", "an", "and", "of", "for", "at", "to"})


def _company_tokens(value: str | None) -> set[str]:
    text = (value or "").casefold().replace("&", " and ")
    return {t for t in re.findall(r"[a-z0-9]+", text) if t not in _COMPANY_TOKEN_STOP}


def _title_tokens(value: str | None) -> set[str]:
    text = (value or "").casefold().replace("&", " and ")
    return {t for t in re.findall(r"[a-z0-9]+", text) if t not in _TITLE_TOKEN_STOP}


def _company_agrees(email_company: str | None, job_company: str | None) -> bool:
    """Reject fuzzy/probable matches when parsed email company contradicts the queue row."""
    email_tokens = _company_tokens(email_company)
    if not email_tokens:
        return True
    job_tokens = _company_tokens(job_company)
    if not job_tokens:
        return False
    overlap = email_tokens & job_tokens
    return bool(overlap) and (len(overlap) / min(len(email_tokens), len(job_tokens))) >= 0.5


def _match_evidence_consistent(email: OutcomeEmail, job: Mapping[str, object], method: str | None) -> bool:
    if method in STRONG_METHODS:
        return True
    return _company_agrees(email.company, str(job.get("company") or ""))


def _exact_company_title_evidence(email: OutcomeEmail, job: Mapping[str, object]) -> bool:
    email_title_tokens = _title_tokens(email.title)
    job_title_tokens = _title_tokens(str(job.get("title") or ""))
    return (
        bool(email_title_tokens)
        and email_title_tokens == job_title_tokens
        and _company_agrees(email.company, str(job.get("company") or ""))
    )


def load_outcome_emails(conn) -> list:
    """Read submission-proving outcome emails from the home brain's email_events table.
    Caller opens the sqlite connection read-only. reconcile() re-matches these raw emails
    itself (it does NOT trust the stored attribution), so this does not filter on job_url
    or match_status='attributed' -- a quarantined-at-scan-time email may legitimately
    confirm a crash job. The ONE exclusion: rows with match_status='needs_review' AND
    match_reason='no_timestamp' -- with no reliable timestamp, the email can never be
    temporally validated as evidence, so there is no point re-matching it here.
    Returns [] if the table does not exist (brain predates the outcomes tracker)."""
    placeholders = ",".join("?" for _ in CONFIRMING_STAGES)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(email_events)").fetchall()}
    except sqlite3.OperationalError:
        cols = set()
    if not cols:
        return []
    # match_status/match_reason may be absent on a legacy brain -- COALESCE-tolerant only
    # when the columns actually exist; otherwise no row can match the exclusion, so skip it.
    if {"match_status", "match_reason"} <= cols:
        exclude_clause = (
            " AND NOT (COALESCE(match_status, '') = 'needs_review' "
            "AND COALESCE(match_reason, '') = 'no_timestamp')"
        )
    else:
        exclude_clause = ""
    try:
        cur = conn.execute(
            f"SELECT message_id, sender, subject, body_text, company, title, job_url, stage, occurred_at "
            f"FROM email_events WHERE stage IN ({placeholders}){exclude_clause}",
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


def load_crash_jobs(conn, *, limit: int | None = None) -> list[dict]:
    """Read crash_unconfirmed jobs and shape them as match_email_to_job candidates
    (site = apply_domain). Read-only."""
    sql = (
        "SELECT url, application_url, company, title, apply_domain, dedup_key, updated_at "
        "FROM apply_queue WHERE status='crash_unconfirmed' "
        "ORDER BY updated_at DESC, url"
    )
    params = None
    if limit is not None:
        sql += " LIMIT %s"
        params = (max(int(limit), 0),)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        out = []
        for r in cur.fetchall():
            out.append({
                "url": r["url"], "application_url": r["application_url"],
                "company": r["company"], "title": r["title"], "site": r["apply_domain"],
                "dedup_key": r["dedup_key"],
                "guard_after": r["updated_at"].isoformat() if hasattr(r["updated_at"], "isoformat") else r["updated_at"],
            })
    return out


def load_consumed_message_ids(conn) -> set[str]:
    """Return Gmail message IDs already used as proof for a reconcile flip.

    A single confirmation email is one piece of evidence. Once consumed, it must not
    be matched to the next same-domain crash row on a later run.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT DISTINCT message_id FROM email_reconcile_actions "
                "WHERE message_id IS NOT NULL AND message_id <> ''"
            )
            rows = cur.fetchall()
    except Exception as exc:
        if exc.__class__.__name__ in {"UndefinedTable", "UndefinedColumn"}:
            rollback = getattr(conn, "rollback", None)
            if rollback is not None:
                rollback()
            return set()
        raise
    out: set[str] = set()
    for row in rows:
        value = row.get("message_id") if isinstance(row, Mapping) else row[0]
        if value:
            out.add(str(value))
    return out


def reconcile(
    emails: list,
    jobs: list[dict],
    *,
    min_strong: float = MIN_STRONG,
    consumed_message_ids: Iterable[str] | None = None,
) -> ReconcileResult:
    """Match each outcome email to a crash job via the existing fuzzy matcher and classify the hit.
    A job is resolved at most once: the highest-scoring hit wins (a strong method scores 1.0).
    A Gmail message_id is also resolved at most once across runs; otherwise repeated dry-runs can
    walk the same confirmation email across many same-company crash rows."""
    best: dict[str, Resolution] = {}   # job_url -> best Resolution
    consumed = {str(mid) for mid in (consumed_message_ids or set()) if mid}
    seen_message_ids: set[str] = set()
    unmatched = 0
    for e in emails:
        message_id = str(e.message_id or "")
        if message_id:
            if message_id in consumed or message_id in seen_message_ids:
                continue
            seen_message_ids.add(message_id)
        r = match_email_to_job(e.sender, e.subject, e.body, jobs, occurred_at=e.occurred_at)
        if r.status != "attributed":
            unmatched += 1
            continue
        job, method, score = r.job, r.method, r.score
        cls = classify_match(method, score, min_strong=min_strong)
        if job is None or cls is None:
            unmatched += 1
            continue
        if not _match_evidence_consistent(e, job, method):
            unmatched += 1
            continue
        if cls == "probable" and _exact_company_title_evidence(e, job):
            cls = "confirmed"
        url = job["url"]
        cand = Resolution(
            job_url=url,
            message_id=e.message_id,
            method=method,
            score=float(score),
            stage=e.stage,
            occurred_at=e.occurred_at,
            classification=cls,
            job_company=str(job.get("company") or job.get("site") or "") or None,
            job_title=str(job.get("title") or "") or None,
            email_sender=e.sender,
            email_subject=e.subject,
        )
        prev = best.get(url)
        if prev is None or cand.score > prev.score:
            best[url] = cand
    confirmed = [r for r in best.values() if r.classification == "confirmed"]
    probable = [r for r in best.values() if r.classification == "probable"]
    return ReconcileResult(confirmed=confirmed, probable=probable,
                           unmatched_emails=unmatched, jobs_total=len(jobs))


def apply_resolutions(
    conn,
    result: ReconcileResult,
    *,
    include_probable: bool = False,
    max_flips: int | None = None,
) -> dict:
    """Flip confirmed (and, if opted-in, probable) jobs crash_unconfirmed -> applied, guarded on
    the current status so it is idempotent and never clobbers a row another process moved. Writes
    one audit row per flip. One transaction per job."""
    targets = list(result.confirmed) + (list(result.probable) if include_probable else [])
    if max_flips is not None:
        targets = targets[: max(int(max_flips), 0)]
    consumed_message_ids = load_consumed_message_ids(conn)
    used_this_run: set[str] = set()
    flipped = skipped = 0
    for r in targets:
        message_id = str(r.message_id or "")
        if message_id and (message_id in consumed_message_ids or message_id in used_this_run):
            skipped += 1
            continue
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
                resolve_superseded_challenges(
                    cur, r.job_url, terminal_status="applied", queue_name="apply_queue"
                )
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
                if message_id:
                    consumed_message_ids.add(message_id)
                    used_this_run.add(message_id)
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
