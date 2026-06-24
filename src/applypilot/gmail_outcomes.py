"""Gmail-based outcome auto-detection for the brainstorm learning loop.

STANDALONE MODULE — deliberately NOT imported from anywhere in the pipeline.
Invoke only via `applypilot scan-gmail`.

Required optional deps (not in pyproject.toml):
    pip install google-auth-oauthlib google-api-python-client

Credentials setup (one-time):
    1. console.cloud.google.com → APIs & Services → Enable APIs → Gmail API
    2. Credentials → Create OAuth 2.0 Client ID (Desktop app) → Download JSON
    3. Save as ~/.applypilot/gmail_credentials.json
    4. First `scan-gmail` run opens a browser for read-only consent
    5. Token cached at ~/.applypilot/gmail_token.json (auto-refreshed)

Scope: gmail.readonly — never modifies or sends email.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]

# ---------------------------------------------------------------------------
# Classification signal lists
# ---------------------------------------------------------------------------

# Priority: offer > interview > rejected.  Each match adds weight; subject
# matches count double (stronger signal than body mentions).

_OFFER_SUBJECT = [
    "offer of employment", "job offer", "offer letter",
    "pleased to offer", "congratulations",
]
_OFFER_BODY = [
    "offer of employment", "pleased to offer", "formal offer",
    "we'd like to offer", "we would like to offer", "extend an offer",
    "offer package", "compensation package", "salary of",
    "sign your offer", "start date of", "please review the attached offer",
]

_INTERVIEW_SUBJECT = [
    "interview invitation", "phone screen", "interview with",
    "next steps", "schedule", "meet with",
]
_INTERVIEW_BODY = [
    "would like to schedule", "schedule a call", "schedule an interview",
    "phone screen", "phone interview", "video interview", "zoom interview",
    "calendly.com", "next steps", "next round",
    "move you forward", "we'd like to move forward",
    "we would like to move forward", "excited to learn more",
    "let's set up", "let's chat", "interview invitation",
    "please schedule", "booking link", "hackerrank", "codility",
    "hireview", "take-home assessment", "technical assessment",
]

_REJECTION_SUBJECT = [
    "unfortunately", "thank you for your interest",
    "update on your application", "your application status",
]
_REJECTION_BODY = [
    "unfortunately", "not moving forward", "not selected",
    "not a fit", "other candidates", "decided not to",
    "will not be moving forward", "position has been filled",
    "no longer considering", "we've decided", "we have decided",
    "decided to move forward with other", "regret to inform",
    "after careful consideration", "not the right fit",
    "not progress", "withdrawn from consideration",
    "pursue other candidates", "gone with another",
]

# Known ATS sender domains — high-confidence job emails
_ATS_DOMAINS: frozenset[str] = frozenset({
    "greenhouse.io", "lever.co", "workday.com", "icims.com",
    "brassring.com", "smartrecruiters.com", "workable.com",
    "taleo.net", "jobvite.com", "recruitee.com", "ashbyhq.com",
    "myworkdayjobs.com", "successfactors.com", "applytojob.com",
    "bamboohr.com", "rippling.com", "eightfold.ai", "beamery.com",
    "paradox.ai", "fountain.com", "dover.com", "dover.io",
    "teamtailor.com", "pinpointhq.com", "comeet.co",
})

# Generic webmail domains that give no company signal
_GENERIC_DOMAINS: frozenset[str] = frozenset({
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "protonmail.com", "icloud.com", "mail.com",
})

_CONFIDENCE_RANK = {"high": 2, "medium": 1, "low": 0}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class EmailOutcome:
    """A classified application-related email with an optional matched job."""
    message_id: str
    date: str
    sender: str
    subject: str
    outcome: str           # "offer" | "interview" | "rejected" | "ambiguous"
    confidence: str        # "high" | "medium" | "low"
    signals_found: list[str] = field(default_factory=list)
    matched_job_url: str | None = None
    matched_job_title: str | None = None
    matched_job_company: str | None = None
    match_method: str | None = None   # "ats_domain"|"company_domain"|"company_name"|"title"
    match_score: float | None = None
    sender_domain: str | None = None
    body_snippet: str | None = None   # first 200 chars, for dry-run display


# ---------------------------------------------------------------------------
# Classification (pure — no I/O)
# ---------------------------------------------------------------------------

def classify_email_outcome(
    subject: str,
    body: str,
    sender: str = "",
) -> tuple[str, str, list[str]]:
    """Classify an email as offer / interview / rejected / ambiguous.

    Returns (outcome, confidence, signals_found).
    """
    subj = subject.lower()
    text = subj + " " + body.lower()
    signals: list[str] = []

    offer_w = 0
    interview_w = 0
    reject_w = 0

    for phrase in _OFFER_SUBJECT:
        if phrase in subj:
            offer_w += 2
            signals.append(f"[subj] {phrase}")
    for phrase in _OFFER_BODY:
        if phrase in text:
            offer_w += 1
            signals.append(phrase)

    for phrase in _INTERVIEW_SUBJECT:
        if phrase in subj:
            interview_w += 2
            signals.append(f"[subj] {phrase}")
    for phrase in _INTERVIEW_BODY:
        if phrase in text:
            interview_w += 1
            signals.append(phrase)

    for phrase in _REJECTION_SUBJECT:
        if phrase in subj:
            reject_w += 2
            signals.append(f"[subj] {phrase}")
    for phrase in _REJECTION_BODY:
        if phrase in text:
            reject_w += 1
            signals.append(phrase)

    # Priority cascade: offer > interview > rejected
    if offer_w > 0:
        conf = "high" if offer_w >= 4 else "medium" if offer_w >= 2 else "low"
        return "offer", conf, signals

    if interview_w > reject_w:
        conf = "high" if interview_w >= 4 else "medium" if interview_w >= 2 else "low"
        return "interview", conf, signals

    if reject_w > 0:
        conf = "high" if reject_w >= 4 else "medium" if reject_w >= 2 else "low"
        return "rejected", conf, signals

    return "ambiguous", "low", signals


# ---------------------------------------------------------------------------
# Job matching helpers (pure — no I/O)
# ---------------------------------------------------------------------------

def _extract_domain(addr: str) -> str | None:
    """Extract the domain part from an email address string."""
    m = re.search(r"@([\w.-]+)", addr)
    return m.group(1).lower() if m else None


def _url_domain(url: str) -> str | None:
    """Extract hostname from a URL, stripping www."""
    m = re.match(r"https?://([^/?#]+)", url or "")
    if not m:
        return None
    return m.group(1).lower().removeprefix("www.")


_STOP_TOKENS = frozenset({"a", "an", "the", "of", "for", "to", "at", "in", "and", "or",
                           "inc", "llc", "ltd", "co", "corp", "company"})


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower())) - _STOP_TOKENS


def _token_overlap(a: str, b: str) -> float:
    """Jaccard-style token overlap; 0.0 if either string is empty."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _extract_company_from_subject(subject: str) -> str | None:
    """Pull a company name from common ATS subject patterns."""
    patterns = [
        r"(?:application|applied)\s+(?:at|to|with)\s+(.+?)(?:\s+for|\s+-|\.|,|$)",
        r"interview\s+with\s+(.+?)(?:\s+for|\s+-|\.|,|$)",
        r"(?:update|status)\s+(?:on|for|from)\s+(.+?)(?:\s+application|\s+-|\.|,|$)",
        r"^(.+?)\s+(?:is reviewing|has received|wants to|would like to)",
        r"from\s+(.+?)\s+(?:re:|regarding|about)\s",
    ]
    for pat in patterns:
        m = re.search(pat, subject, re.IGNORECASE)
        if m:
            company = m.group(1).strip().strip("\"'")
            if 2 <= len(company) <= 60:
                return company
    return None


def _extract_company_from_body(body: str) -> str | None:
    """Pull a company name from the first 400 chars of the email body."""
    snippet = body[:400]
    patterns = [
        r"at\s+([A-Z][A-Za-z0-9 &.,'-]{1,40}?)(?:\s+for|\s*[,.]|\s+is|\s+has)",
        r"from\s+([A-Z][A-Za-z0-9 &.,'-]{1,40}?)(?:\s+re:|\s*[,.]|\s+regarding)",
    ]
    for pat in patterns:
        m = re.search(pat, snippet)
        if m:
            company = m.group(1).strip()
            if 2 <= len(company) <= 60:
                return company
    return None


def _extract_title_from_subject(subject: str) -> str | None:
    """Pull a job title from the subject line."""
    patterns = [
        r"(?:for the|for a|for)\s+(.+?)\s+(?:role|position|opening|opportunity)",
        r"application\s+for\s+(.+?)(?:\s+at|\s+-|\.|$)",
        r"re:\s+(.+?)\s+(?:at|application|\||-)[^-]",
    ]
    for pat in patterns:
        m = re.search(pat, subject, re.IGNORECASE)
        if m:
            title = m.group(1).strip().strip("\"'")
            if 2 <= len(title) <= 80:
                return title
    return None


def _best_name_match(
    hint: str,
    jobs: list[dict[str, Any]],
    *,
    min_overlap: float,
) -> tuple[dict[str, Any] | None, float]:
    best_job: dict[str, Any] | None = None
    best_score = 0.0
    for job in jobs:
        for field_val in (job.get("site") or "", job.get("title") or "",
                          job.get("company") or ""):
            score = _token_overlap(hint, field_val)
            if score > best_score:
                best_score = score
                best_job = job
    if best_job and best_score >= min_overlap:
        return best_job, best_score
    return None, 0.0


def match_email_to_job(
    sender: str,
    subject: str,
    body: str,
    applied_jobs: list[dict[str, Any]],
    *,
    min_overlap: float = 0.25,
) -> tuple[dict[str, Any] | None, str | None, float | None]:
    """Match an email to an applied job.  Returns (job, method, score) or (None, None, None)."""
    sender_domain = _extract_domain(sender) or ""
    is_ats = sender_domain in _ATS_DOMAINS or any(
        sender_domain.endswith(f".{d}") for d in _ATS_DOMAINS
    )

    # 1. ATS sender: trust company name extracted from subject/body
    if is_ats:
        hint = _extract_company_from_subject(subject) or _extract_company_from_body(body)
        if hint:
            job, score = _best_name_match(hint, applied_jobs, min_overlap=min_overlap)
            if job:
                return job, "ats_domain", score

    # 2. Company domain: sender domain matches a job URL domain
    if sender_domain and sender_domain not in _GENERIC_DOMAINS and not is_ats:
        for job in applied_jobs:
            for url_key in ("url", "application_url"):
                job_domain = _url_domain(job.get(url_key) or "")
                if job_domain and (
                    job_domain == sender_domain
                    or job_domain.endswith(f".{sender_domain}")
                    or sender_domain.endswith(f".{job_domain}")
                ):
                    return job, "company_domain", 1.0

    # 3. Company name in subject/body → fuzzy name match
    hint = _extract_company_from_subject(subject) or _extract_company_from_body(body)
    if hint:
        job, score = _best_name_match(hint, applied_jobs, min_overlap=min_overlap)
        if job:
            return job, "company_name", score

    # 4. Job title in subject → title overlap
    title_hint = _extract_title_from_subject(subject)
    if title_hint:
        best_job: dict[str, Any] | None = None
        best_score = 0.0
        for job in applied_jobs:
            s = _token_overlap(title_hint, job.get("title") or "")
            if s > best_score:
                best_score = s
                best_job = job
        if best_job and best_score >= min_overlap:
            return best_job, "title", best_score

    return None, None, None


# ---------------------------------------------------------------------------
# DB helpers (lazy import to avoid loading DB at module import)
# ---------------------------------------------------------------------------

def get_applied_jobs() -> list[dict[str, Any]]:
    """Return jobs from the DB that have been applied to (or tracked)."""
    from applypilot.database import get_connection
    conn = get_connection()
    rows = conn.execute("""
        SELECT j.url, j.application_url, j.title, j.site, j.apply_status,
               a.status AS tracker_status,
               COALESCE(a.company, j.site) AS company
          FROM jobs j
          LEFT JOIN applications a ON a.job_url = j.url
         WHERE j.apply_status = 'applied'
            OR a.status IS NOT NULL
         ORDER BY COALESCE(j.applied_at, j.discovered_at) DESC
    """).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Gmail service helper and scanning (Google API imported lazily — only called from scan-gmail CLI)
# ---------------------------------------------------------------------------

def build_gmail_service(
    credentials_path: Path | None = None,
    token_path: Path | None = None,
):
    """Build a gmail service instance using read-only OAuth credentials."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ImportError(
            "Gmail scanning requires optional dependencies:\n"
            "  pip install google-auth-oauthlib google-api-python-client\n"
            "Then set up credentials — run `applypilot scan-gmail --help`."
        ) from exc

    from applypilot.config import APP_DIR

    creds_path = credentials_path or (APP_DIR / "gmail_credentials.json")
    tok_path = token_path or (APP_DIR / "gmail_token.json")

    if not creds_path.exists():
        raise FileNotFoundError(
            f"Gmail credentials not found: {creds_path}\n\n"
            "One-time setup:\n"
            "  1. console.cloud.google.com → APIs & Services → Enable APIs → Gmail API\n"
            "  2. Credentials → Create OAuth 2.0 Client ID (Desktop app)\n"
            "  3. Download JSON → save as:\n"
            f"     {creds_path}"
        )

    creds = None
    if tok_path.exists():
        creds = Credentials.from_authorized_user_file(str(tok_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        tok_path.write_text(creds.to_json(), encoding="utf-8")
        log.info("Gmail token saved to %s", tok_path)

    return build("gmail", "v1", credentials=creds)


def _get_text_body(payload: dict[str, Any]) -> str:
    """Recursively extract plain text from a Gmail message payload."""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and data:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

    if mime == "text/html" and data:
        raw = base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", raw)

    plain, html = "", ""
    for part in payload.get("parts", []):
        result = _get_text_body(part)
        pt = part.get("mimeType", "")
        if "plain" in pt:
            plain += result
        elif "html" in pt:
            html += result
        else:
            plain += result
    return plain or html


def _search_query(days: int) -> str:
    return (
        f"(subject:\"your application\" OR subject:interview OR subject:offer "
        f"OR subject:unfortunately OR subject:\"next steps\" "
        f"OR subject:\"thank you for\" OR subject:\"update on\" "
        f"OR from:greenhouse.io OR from:lever.co OR from:workday.com "
        f"OR from:smartrecruiters.com OR from:workable.com OR from:icims.com "
        f"OR from:ashbyhq.com OR from:jobvite.com OR from:teamtailor.com) "
        f"newer_than:{days}d"
    )


def scan_inbox(
    days: int = 30,
    credentials_path: Path | None = None,
    token_path: Path | None = None,
    max_messages: int = 200,
    min_confidence: str = "low",
) -> list[EmailOutcome]:
    """Scan Gmail for application-related emails and classify outcomes.

    Requires optional deps: google-auth-oauthlib google-api-python-client
    On first run, opens a browser for OAuth consent (read-only scope).

    Args:
        days:             How many days back to search.
        credentials_path: Path to OAuth client secrets JSON.
                          Defaults to ~/.applypilot/gmail_credentials.json.
        token_path:       Path to cached token JSON.
                          Defaults to ~/.applypilot/gmail_token.json.
        max_messages:     Safety cap on messages fetched.
        min_confidence:   Drop results below this level ("low"|"medium"|"high").

    Returns:
        List of EmailOutcome objects (one per thread).
    """
    service = build_gmail_service(
        credentials_path=credentials_path,
        token_path=token_path,
    )

    query = _search_query(days)
    log.info("Gmail query: %s", query)
    resp = service.users().messages().list(
        userId="me", q=query, maxResults=max_messages
    ).execute()
    messages = resp.get("messages", [])
    log.info("Fetching %d candidate message(s)", len(messages))

    applied_jobs = get_applied_jobs()
    min_rank = _CONFIDENCE_RANK.get(min_confidence, 0)

    results: list[EmailOutcome] = []
    seen_threads: set[str] = set()

    for ref in messages:
        thread_id = ref.get("threadId", ref["id"])
        if thread_id in seen_threads:
            continue  # one result per thread
        seen_threads.add(thread_id)

        try:
            msg = service.users().messages().get(
                userId="me", id=ref["id"], format="full"
            ).execute()
        except Exception as exc:
            log.warning("Could not fetch message %s: %s", ref["id"], exc)
            continue

        headers = {
            h["name"].lower(): h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        date = headers.get("date", "")
        body = _get_text_body(msg.get("payload", {}))

        outcome, confidence, signals = classify_email_outcome(subject, body, sender)

        if outcome == "ambiguous":
            continue
        if _CONFIDENCE_RANK.get(confidence, 0) < min_rank:
            continue

        matched, method, score = match_email_to_job(sender, subject, body, applied_jobs)

        results.append(EmailOutcome(
            message_id=ref["id"],
            date=date,
            sender=sender,
            subject=subject,
            outcome=outcome,
            confidence=confidence,
            signals_found=signals[:6],
            matched_job_url=matched.get("url") if matched else None,
            matched_job_title=matched.get("title") if matched else None,
            matched_job_company=matched.get("company") if matched else None,
            match_method=method,
            match_score=score,
            sender_domain=_extract_domain(sender),
            body_snippet=body[:200].replace("\n", " ").strip(),
        ))

    return results


# ---------------------------------------------------------------------------
# Apply detected outcomes to the tracker (dry-run safe)
# ---------------------------------------------------------------------------

# Maps the email outcome labels to tracker status aliases accepted by
# record_application / _normalize_status.
_OUTCOME_TO_TRACKER = {
    "offer": "offer",
    "interview": "interview",   # aliased → recruiter_screen
    "rejected": "rejected",
}


def apply_outcomes(
    outcomes: list[EmailOutcome],
    *,
    dry_run: bool = True,
    channel: str = "gmail_auto",
) -> dict[str, Any]:
    """Write detected outcomes to the applications tracker.

    Only acts on outcomes that have a matched job and a known outcome type.

    Args:
        outcomes: From scan_inbox.
        dry_run:  If True (default), logs what would happen without writing.
        channel:  Source channel recorded in the applications table.

    Returns:
        Summary counts dict.
    """
    counts: dict[str, int] = {
        "written": 0,
        "skipped_no_match": 0,
        "skipped_ambiguous": 0,
        "errors": 0,
    }

    for o in outcomes:
        if o.outcome == "ambiguous":
            counts["skipped_ambiguous"] += 1
            continue
        if not o.matched_job_url:
            counts["skipped_no_match"] += 1
            continue
        if o.outcome not in _OUTCOME_TO_TRACKER:
            counts["skipped_ambiguous"] += 1
            continue

        tracker_status = _OUTCOME_TO_TRACKER[o.outcome]
        note = f"Auto-detected from Gmail ({o.date}): {o.subject[:80]}"

        if dry_run:
            log.info(
                "[dry-run] %s → %s  |  %s",
                o.matched_job_title or o.matched_job_url,
                tracker_status,
                o.subject[:60],
            )
            counts["written"] += 1
        else:
            try:
                from applypilot.applications import record_application
                record_application(
                    job_ref=o.matched_job_url,
                    status=tracker_status,
                    channel=channel,
                    notes=note,
                )
                counts["written"] += 1
            except Exception as exc:
                log.warning("Failed to record outcome for %s: %s", o.matched_job_url, exc)
                counts["errors"] += 1

    return counts
