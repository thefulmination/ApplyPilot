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

# Outcome priority among JOB emails: offer > rejected > interview > acknowledged.
# Subject matches count double (stronger signal than body mentions). A non-job email
# (promotional/financial/newsletter) is dropped BEFORE classification by the non-job gate.

# --- Recruiting/job context anchors -------------------------------------------
# An email is kept for classification only if it has recruiting context: a known ATS
# sender, a job-context anchor below, OR a genuine job-OUTCOME signal (a real offer/
# rejection/interview phrase -- see _has_job_outcome_signal). Guards against promotional
# emails that reuse outcome words ("loan offer", "rewards application"). NOTE: generic
# offer phrasing ("we'd like to offer you") is deliberately NOT an anchor -- loans use it.
_JOB_CONTEXT = [
    "your application", "thank you for applying", "thanks for applying",
    "we received your application", "we've received your application",
    "received your application", "application has been received",
    "application was received", "application received", "application confirmation",
    "your candidacy", "candidate", "applied to", "applied for",
    "application for the", "application to the", "job application",
    "the position", "this position", "the role of", "this role", "for the role",
    "recruiter", "recruiting", "talent acquisition", "talent team", "talent partner",
    "hiring team", "hiring manager", "people team",
    "phone screen", "careers page", "careers team", "career opportunities",
    "job opening", "requisition", "thank you for your interest in",
    "assessment", "hackerrank", "codility", "codesignal", "take-home",
    "coding challenge", "skills assessment",
]

# --- Promotional / non-job markers --------------------------------------------
# STRONG = unambiguous promo/financial/marketing -> drop a non-ATS email OUTRIGHT (these
# never appear in a real recruiting email, even one that reuses "application"/"offer").
_PROMO_STRONG = [
    "balance transfer", "annual percentage rate", "% apr", "percent apr",
    "credit limit", "cardmember", "personal loan", "loan is approved", "loan offer",
    "refinance", "refinancing", "student loan", "credit card", "rewards card",
    "companion fare", "bonus points", "save on interest", "% off", "shop now",
    "coaching call", "free coaching", "webinar", "register now", "save your seat",
    "limited-time offer", "pre-approved", "frequent flyer", "your statement",
]
# WEAK = promotional-leaning; decisive only when there is NO real job context/signal.
_PROMO_MARKERS = [
    "view in browser", "view this email in your browser", "unsubscribe",
    "activate this offer", "limited-time", "pre-selected", "sponsored",
    "newsletter", "daily digest",
]
# Domains that are never recruiting (finance / airline / retail / newsletter).
_NON_JOB_SENDERS = [
    "citi.com", "chase.com", "capitalone.com", "americanexpress.com", "discover.com",
    "firsttechfed.com", "sofi.com", "marcus.com", "creditkarma.com", "bankofamerica.com",
    "earnest.com", "alaskaair.com", "united.com", "delta.com", "southwest.com",
    "slickdeals.net", "myprotein.com", "groupon.com", "tdf.org", "ticketmaster.com",
    "medium.com", "substack.com",
]

# --- Offer signals ------------------------------------------------------------
# STRONG = a single occurrence is a real job offer (worth 2). Phrased to be JOB-specific
# (".../you the position/role/job", "...of employment") so a financial/promotional
# "we'd like to offer you 0% APR" does NOT match.
_OFFER_STRONG = [
    "offer of employment", "offer you the position", "offer you the role",
    "offer you the job", "pleased to offer you the", "we would like to offer you the",
    "we'd like to offer you the", "happy to offer you the", "thrilled to offer you the",
    "excited to offer you the", "prepared to offer you the", "extend an offer of",
    "extend you an offer", "would like to extend an offer", "formal offer of employment",
    "welcome aboard", "welcome you aboard", "welcome to the team",
]
_OFFER_SUBJECT = ["offer of employment", "job offer", "offer letter", "your offer of"]
# WEAK = corroborating (worth 1); two of these (or one + a strong/subject hit) = offer.
_OFFER_WEAK = [
    "your starting salary", "annual base salary", "your compensation will be",
    "compensation package", "your start date", "start date of", "join the team",
    "excited to have you join", "offer letter is attached", "attached is your offer",
]

# --- Interview signals --------------------------------------------------------
# STRONG SUBJECT = an explicit NEW interview/assessment invite -> short-circuits to
# interview. Thread-title phrases ("your interview", "interview with") are NOT here: they
# persist on a post-interview REJECTION reply, so they must not force "interview".
_INTERVIEW_SUBJECT_STRONG = [
    "interview invitation", "interview invite", "invitation to interview",
    "schedule your interview", "schedule an interview", "book your interview",
    "phone screen", "your assessment", "complete your assessment",
]
# BODY = actionable scheduling/assessment requests. Bare "next steps"/"schedule" excluded
# (they appear in receipts/rejections); these are concrete CTAs.
# STRONG body CTA = a concrete, present-tense interview/assessment action -> overrides an
# acknowledgment receipt. Assessment platforms, direct availability questions, booking
# links, explicit "invite you to interview" -- these do NOT appear in receipt boilerplate.
_INTERVIEW_STRONG = [
    "hackerrank", "codility", "codesignal", "coding assessment", "technical assessment",
    "online assessment", "take-home", "complete the assessment",
    "complete the online assessment", "complete an assessment", "skills assessment",
    "are you available", "when are you free", "your availability", "availability for a",
    "what is your availability", "calendly", "book a time", "pick a time",
    "choose a time", "select a time", "booking link", "use the link below to schedule",
    "invite you to interview", "invite you to an interview", "invitation to interview",
    "like to interview you", "would like to interview you",
]
# WEAK body CTA = generic scheduling that ALSO appears in receipt boilerplate ("we will
# reach out to schedule a call IF your background matches"). Classifies as interview ONLY
# when the email is not an acknowledgment receipt.
_INTERVIEW_WEAK = [
    "would like to schedule", "schedule a call", "schedule an interview",
    "schedule a time", "set up a call", "set up a time", "set up an interview",
    "find a time", "find time to chat", "grab time", "quick call", "hop on a call",
    "phone screen", "phone interview", "video interview", "zoom interview",
    "next round", "move to the next round", "first round interview",
    "move you forward to an interview",
]

# --- Rejection signals --------------------------------------------------------
# STRONG = DEFINITE rejections; these override an acknowledgment receipt, since
# rejections are often phrased "thank you for applying ... unfortunately ...". Curated to
# EXCLUDE the conditional/courtesy boilerplate ATS acknowledgments include -- the Lever
# template "If you are not selected for this position, keep an eye on our jobs page" and
# closings like "we wish you the best in your search" are NOT rejections.
_REJECTION_STRONG = [
    "regret to inform", "you have not been selected", "you were not selected",
    "not been selected for this", "decided to pursue other", "pursue other candidates",
    "pursue other applicant", "move forward with other candidate",
    "moving forward with other candidate", "forward with other applicant",
    "we have decided to pursue", "decided to move forward with other",
    "not be moving forward with your", "will not be moving forward with your",
    "not moving forward with your application", "not be moving your application forward",
    "decided not to move forward with your", "decided not to proceed",
    "will not be proceeding with your", "not be proceeding with your application",
    "unable to move forward with your application", "not be advancing your application",
    "not be progressing your application", "position has been filled",
    "no longer being considered", "after careful consideration, we have decided",
    "after careful review, we have decided", "have decided to move forward with another",
    "not selected to move forward", "will not be extending",
]
# Genuine-rejection SUBJECT phrases ONLY. NEUTRAL status subjects ("update on your
# application", "regarding your application", "application status") were REMOVED -- they
# sit on offers, progressions, and acknowledgments too, and were injecting reject weight
# that flagged real offers/acks as rejected.
_REJECTION_SUBJECT = [
    "unfortunately", "we regret to inform", "application unsuccessful",
    "not selected", "your application was unsuccessful",
]
# DECISION body signals = a real rejection DECISION -> overrides an acknowledgment receipt
# (a soft rejection under a "thanks for applying" subject is still a rejection).
_REJECTION_DECISION = [
    "not moving forward", "won't be moving forward", "will not be moving forward",
    "no longer considering", "not the right fit", "another candidate",
    "go with another", "chosen another", "decided to go with", "cannot move you forward",
    "not be a match", "not a match for", "regret that we", "not moving you forward",
]
# WEAK = ambiguous courtesy/disclaimer ("due to volume we unfortunately cannot respond to
# everyone") -> a rejection signal ONLY when the email is not a receipt.
_REJECTION_WEAK = [
    "unfortunately", "we regret that", "your application was unsuccessful",
]

# --- Acknowledgment signals (NEW) ---------------------------------------------
# Application received / under review, NO decision yet. The big missing bucket -- these
# are receipts, not interviews, and previously leaked into "interview"/"ambiguous".
# NOTE: bare "your application" is deliberately NOT here -- it appears in interview and
# rejection subjects too ("Next steps for your application", "Your application -- update"),
# so it must not force an acknowledgment over real interview/offer signals.
_ACK_SUBJECT = [
    "thank you for applying", "thanks for applying", "thank you for your application",
    "application received", "application confirmation", "we received your application",
    "we've received your application", "received your application", "application submitted",
]
_ACK_BODY = [
    "thank you for applying", "thanks for applying", "we have received your application",
    "we've received your application", "we received your application",
    "your application has been received", "application has been submitted",
    "we will review your application", "we'll review your application",
    "currently reviewing", "under review", "reviewing your application",
    "we appreciate your interest", "thank you for your interest in",
    "received and we will", "application is being reviewed",
    "review every application", "if your background", "we'll be in touch",
    "will be in touch",
]

# Known ATS sender domains — high-confidence job emails. Includes the *-mail.* sending
# subdomains ATS platforms actually send from (e.g. greenhouse-mail.io, teamtailor-mail
# .com) -- the bare board domains (greenhouse.io) never appear as the From, so matching
# and ATS detection used to miss every greenhouse/teamtailor confirmation.
_ATS_DOMAINS: frozenset[str] = frozenset({
    "greenhouse.io", "greenhouse-mail.io",
    "lever.co", "hire.lever.co",
    "workday.com", "myworkdayjobs.com", "myworkday.com",
    "icims.com", "brassring.com",
    "smartrecruiters.com", "smartrecruiters-mail.com",
    "workable.com", "workablemail.com", "taleo.net",
    "jobvite.com", "jobvite-mail.com", "recruitee.com",
    "ashbyhq.com", "ashbyhq-mail.com", "ashby.email",
    "successfactors.com", "applytojob.com",
    "bamboohr.com", "rippling.com",
    "eightfold.ai", "beamery.com", "paradox.ai", "fountain.com",
    "dover.com", "dover.io", "teamtailor.com", "teamtailor-mail.com",
    "pinpointhq.com", "comeet.co", "gem.com", "breezy.hr", "hiringthing.com",
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
    outcome: str           # offer|interview|rejected|acknowledged|ambiguous
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

def _is_ats_domain(domain: str | None) -> bool:
    """True if a sender domain is (or is a subdomain of) a known ATS/recruiting domain."""
    if not domain:
        return False
    return domain in _ATS_DOMAINS or any(domain.endswith(f".{d}") for d in _ATS_DOMAINS)


def _confidence(weight: int) -> str:
    return "high" if weight >= 4 else "medium" if weight >= 2 else "low"


def _has_job_outcome_signal(subj: str, text: str) -> bool:
    """A genuine offer/rejection/interview phrase -- counts as job context so a real
    outcome email from a COMPANY (non-ATS) sender (careers@/talent@/people@) is never
    dropped by the non-job gate just for lacking an incidental anchor word."""
    return (
        any(p in text for p in _OFFER_STRONG)
        or any(p in subj for p in _OFFER_SUBJECT)
        or any(p in text for p in _REJECTION_STRONG)
        or any(p in text for p in _REJECTION_DECISION)
        or any(p in subj for p in _INTERVIEW_SUBJECT_STRONG)
        or any(p in text for p in _INTERVIEW_STRONG)
        or any(p in text for p in _INTERVIEW_WEAK)
    )


def is_non_job_email(subject: str, body: str, sender: str = "") -> tuple[bool, list[str]]:
    """Decide whether an email is NOT a job/recruiting email (so the caller drops it).

    Keep an email only if it has recruiting context: a known ATS sender, a job-context
    anchor, OR a genuine job-outcome signal. Drop it for a known non-job sender, an
    unambiguous promotional/financial marker (a real recruiting email never carries
    these, even one that reuses "application"/"offer"), or no recruiting context at all.
    Returns (is_non_job, reasons)."""
    dom = _extract_domain(sender) or ""
    subj = subject.lower()
    text = subj + " " + body.lower()

    if _is_ats_domain(dom):
        return False, []  # ATS senders are always job emails
    if any(dom == d or dom.endswith(f".{d}") for d in _NON_JOB_SENDERS):
        return True, [f"non_job_sender:{dom}"]
    strong_promo = [p for p in _PROMO_STRONG if p in text]
    if strong_promo:
        return True, [f"promo:{p}" for p in strong_promo[:3]]
    if any(a in text for a in _JOB_CONTEXT) or _has_job_outcome_signal(subj, text):
        return False, []
    weak = [p for p in _PROMO_MARKERS if p in text]
    return True, (["no_job_context"] + [f"promo:{p}" for p in weak[:2]])


def classify_email_outcome(
    subject: str,
    body: str,
    sender: str = "",
) -> tuple[str, str, list[str]]:
    """Classify a job email as offer / interview / rejected / acknowledged / ambiguous.

    Non-job emails (promotional/financial/newsletter) return ("not_job", "high", reasons)
    and are dropped by the caller. Returns (outcome, confidence, signals_found).
    """
    non_job, why = is_non_job_email(subject, body, sender)
    if non_job:
        return "not_job", "high", why

    subj = subject.lower()
    text = subj + " " + body.lower()
    signals: list[str] = []

    def hits(phrases: list[str], in_subj: bool = False) -> list[str]:
        src = subj if in_subj else text
        found = [p for p in phrases if p in src]
        signals.extend((f"[subj] {p}" if in_subj else p) for p in found)
        return found

    # OFFER -- a strong job-specific phrase OR an offer subject = weight 2; weak = 1 each.
    offer_w = (2 if hits(_OFFER_STRONG) else 0) + 2 * len(hits(_OFFER_SUBJECT, in_subj=True))
    offer_w += len(hits(_OFFER_WEAK))
    # INTERVIEW -- explicit invite subject; concrete (strong) body CTA vs generic (weak).
    interview_subj = bool(hits(_INTERVIEW_SUBJECT_STRONG, in_subj=True))
    iv_strong = bool(hits(_INTERVIEW_STRONG))
    interview_w = (2 if iv_strong else 0) + len(hits(_INTERVIEW_WEAK))
    # REJECTION -- definite (strong) and a real DECISION override receipts; "unfortunately"
    # and a genuine-reject subject are weaker (yield to a receipt).
    reject_strong = bool(hits(_REJECTION_STRONG))
    reject_decision = bool(hits(_REJECTION_DECISION))
    reject_w = 2 * len(hits(_REJECTION_SUBJECT, in_subj=True)) + len(hits(_REJECTION_WEAK))
    # ACKNOWLEDGMENT
    ack_subj = bool(hits(_ACK_SUBJECT, in_subj=True))
    ack_w = (2 if ack_subj else 0) + len(hits(_ACK_BODY))

    # Precedence. The key boundary the adversarial pass exposed: a receipt subject must NOT
    # mask a real outcome, AND receipt boilerplate ("if your background matches, we'll
    # reach out to schedule a call") must NOT register as a real interview/rejection. So
    # only CONCRETE signals (strong-reject, decision-reject, explicit-interview-subject,
    # strong interview CTA) override a receipt; generic scheduling / "unfortunately" yield.
    if offer_w >= 2:
        return "offer", _confidence(offer_w), signals
    if reject_strong:
        return "rejected", "high", signals
    if interview_subj:
        return "interview", _confidence(interview_w + 2), signals
    if iv_strong:                  # concrete interview/assessment CTA -> overrides a receipt
        return "interview", _confidence(interview_w), signals
    if reject_decision:            # a real rejection decision -> overrides a receipt
        return "rejected", _confidence(max(reject_w, 2)), signals
    if ack_subj or ack_w >= 1:     # receipt with only boilerplate -> acknowledged
        return "acknowledged", _confidence(max(ack_w, 2)), signals
    if reject_w >= 1:              # "unfortunately"/reject subject, only when not a receipt
        return "rejected", _confidence(reject_w), signals
    if interview_w >= 1:           # generic scheduling, only when not a receipt
        return "interview", _confidence(max(interview_w, 2)), signals
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
    # Recall-oriented: ATS senders + recruiting subject anchors within the window.
    # Exclude the Promotions/Social tabs -- that's where bank-loan / airline / retail
    # "offer" emails and newsletters land (the dominant non-job noise). The classifier's
    # non-job gate is the backstop for promo that slips into the Updates tab.
    return (
        f"-category:promotions -category:social newer_than:{days}d "
        "(subject:\"your application\" OR subject:\"thank you for applying\" "
        "OR subject:\"thanks for applying\" OR subject:\"application received\" "
        "OR subject:\"application confirmation\" OR subject:interview "
        "OR subject:\"interview invitation\" OR subject:\"offer of employment\" "
        "OR subject:unfortunately OR subject:\"update on your application\" "
        "OR from:greenhouse-mail.io OR from:greenhouse.io OR from:lever.co "
        "OR from:ashbyhq.com OR from:ashbyhq-mail.com OR from:workday.com "
        "OR from:myworkdayjobs.com OR from:smartrecruiters.com OR from:workable.com "
        "OR from:icims.com OR from:jobvite.com OR from:teamtailor.com "
        "OR from:teamtailor-mail.com OR from:bamboohr.com OR from:rippling.com "
        "OR from:recruitee.com OR from:pinpointhq.com OR from:dover.com "
        "OR from:eightfold.ai OR from:paradox.ai OR from:gem.com)"
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

        # Drop non-job (promotional/financial/newsletter) and unclassifiable emails.
        # Acknowledgments ARE kept (shown in the scan) but are display-only downstream.
        if outcome in ("ambiguous", "not_job"):
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
        "skipped_acknowledged": 0,
        "errors": 0,
    }

    for o in outcomes:
        # Acknowledgments are receipts (application received) -- shown in the scan but
        # NOT written to the tracker: the tool already recorded the apply, and writing
        # 'applied' from an old receipt could downgrade a job that has since advanced.
        if o.outcome == "acknowledged":
            counts["skipped_acknowledged"] += 1
            continue
        if o.outcome not in _OUTCOME_TO_TRACKER:
            counts["skipped_ambiguous"] += 1
            continue
        if not o.matched_job_url:
            counts["skipped_no_match"] += 1
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
