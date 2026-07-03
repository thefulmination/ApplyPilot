from __future__ import annotations

import base64
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from typing import Any, Literal
from urllib.parse import urlparse

from applypilot.database import get_connection

Confidence = Literal["low", "medium", "high"]
CandidateKind = Literal["code", "magic_link"]

KNOWN_ATS_DOMAINS = {
    "greenhouse.io",
    "boards.greenhouse.io",
    "myworkday.com",
    "myworkdayjobs.com",
    "lever.co",
    "ashbyhq.com",
    "icims.com",
    "smartrecruiters.com",
    "workable.com",
    "taleo.net",
    "oraclecloud.com",
}

VERIFY_WORDS = {
    "verification",
    "verification code",
    "verify",
    "verify your email",
    "confirm your email",
    "security code",
    "authentication code",
    "one-time",
    "one time",
    "one-time code",
    "one time code",
    "one-time passcode",
    "one time passcode",
    "passcode",
    "otp",
    "magic link",
    "sign in",
    "continue your application",
}

_CODE_RE = re.compile(r"(?<![A-Za-z0-9-])\d{4,8}(?![A-Za-z0-9-])")
_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_STRONG_AUTH_URL_PATH_RE = re.compile(
    r"(^|[/?&_.=-])(?:verify|verification|magic|magic-link|continue)([/?&_.=-]|$)",
    re.IGNORECASE,
)
_GENERIC_AUTH_URL_PATH_RE = re.compile(
    r"(^|[/?&_.=-])(?:signin|sign-in|login)([/?&_.=-]|$)",
    re.IGNORECASE,
)
_AUTH_URL_QUERY_RE = re.compile(
    r"(^|[&;])(?:token|verification_token|magic_token|code|otp)=",
    re.IGNORECASE,
)
_REJECTED_MAGIC_PATH_RE = re.compile(
    r"(^|[/?&_.=-])"
    r"(?:interview|interviews|schedule|scheduling|job|jobs|posting|postings|role|roles|detail|details)"
    r"([/?&_.=-]|$)",
    re.IGNORECASE,
)
_AUTH_CODE_BEFORE_RE = re.compile(
    r"\b(?:verification|security|authentication|one[- ]time)\s+(?:code|passcode)\s*(?:is\s*:?|:)?\s*$"
    r"|\b(?:otp|passcode)\s*(?:is\s*:?|:)?\s*$"
    r"|\b(?:to\s+)?(?:verify|confirm)\s+your\s+email,?\s*(?:please\s+)?(?:enter|use)\s*$",
    re.IGNORECASE,
)
_CODE_COMMAND_BEFORE_RE = re.compile(r"\b(?:enter|use)\s*$", re.IGNORECASE)
_PLAIN_CODE_BEFORE_RE = re.compile(r"\b(?:your\s+)?code\s*(?:is\s*:?|:)?\s*$", re.IGNORECASE)
_AUTH_CODE_AFTER_RE = re.compile(
    r"^\s*(?:to\s+)?(?:verify|confirm)\s+your\s+email\b"
    r"|^\s*(?:to\s+)?(?:sign\s+in|continue\s+your\s+application)\b",
    re.IGNORECASE,
)
_AUTH_CODE_FIRST_AFTER_RE = re.compile(
    r"^\s*is\s+your\s+(?:verification|security|authentication|one[- ]time)\s+(?:code|passcode)\b",
    re.IGNORECASE,
)
_NEGATIVE_CODE_PREFIX_RE = re.compile(
    r"\b(?:zip|postal|job|reference|support|contact|contacting)\s*(?:id|code|number|#)?\s*(?:is|:|#)?\s*$",
    re.IGNORECASE,
)
_NEGATIVE_CODE_SUFFIX_RE = re.compile(
    r"^\s*(?:when\s+contacting\s+support\b|for\s+(?:support|contact)\b|as\s+your\s+reference\b)",
    re.IGNORECASE,
)
_MAGIC_LINK_CONTEXT_RE = re.compile(
    r"\b(?:verify your email|confirm your email|magic link|sign in|sign-in|continue your application)\b",
    re.IGNORECASE,
)
_TRACKING_DOMAIN_LABELS = {"click", "track", "tracking", "trk", "link", "links"}
_TRACKING_PATH_RE = re.compile(
    r"(^|[/?&_.=-])(unsubscribe|unsub|pixel|tracking|track|click|redirect|open)([/?&_.=-]|$)",
    re.IGNORECASE,
)
_CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}
_GOOGLE_ACCOUNT_DOMAINS = {"accounts.google.com"}
_GOOGLE_SECURITY_WORDS = (
    "security alert",
    "passkey",
    "suspicious",
    "suspicious login",
    "new sign-in",
    "new sign in",
)


@dataclass(frozen=True)
class VerificationCandidate:
    kind: CandidateKind
    value: str
    confidence: Confidence
    reasons: tuple[str, ...]
    position: int = 0


@dataclass(frozen=True)
class _CandidateDraft:
    kind: CandidateKind
    value: str
    reasons: tuple[str, ...]
    position: int


@dataclass(frozen=True)
class AuthEmailMatch:
    message_id: str
    thread_id: str | None
    sender: str
    subject: str
    received_at: str | None
    snippet: str
    candidate: VerificationCandidate
    reasons: tuple[str, ...]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sender_domain(sender: str) -> str:
    _, address = parseaddr(sender or "")
    value = address or sender or ""
    if value.lower().startswith("mailto:"):
        value = value[7:]
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    return _normalize_domain(value)


def url_domain(url: str) -> str:
    parsed = urlparse(url.strip())
    return _normalize_domain(parsed.hostname or "")


def is_known_ats_domain(domain: str) -> bool:
    normalized = _normalize_domain(domain)
    if not normalized:
        return False
    return any(normalized == known or normalized.endswith(f".{known}") for known in KNOWN_ATS_DOMAINS)


def is_google_security_prompt(subject: str, body: str, sender: str) -> bool:
    domain = sender_domain(sender)
    if any(
        domain == google_domain or domain.endswith(f".{google_domain}")
        for google_domain in _GOOGLE_ACCOUNT_DOMAINS
    ):
        return True

    text = _combined_text(subject, body).lower()
    return any(word in text for word in _GOOGLE_SECURITY_WORDS)


def extract_verification_candidates(subject: str, body: str, sender: str) -> list[VerificationCandidate]:
    if is_google_security_prompt(subject, body, sender):
        return []

    text = _combined_text(subject, body)
    sender_is_known_ats = is_known_ats_domain(sender_domain(sender))
    has_verification_language = _has_verification_language(text)
    url_spans = [(match.start(), match.end()) for match in _URL_RE.finditer(text)]

    drafts = _extract_code_drafts(text, sender_is_known_ats, has_verification_language, url_spans)
    drafts.extend(_extract_magic_link_drafts(text, sender_is_known_ats, has_verification_language))
    drafts = _dedupe_drafts(drafts)

    single_candidate = len(drafts) == 1
    candidates = [
        VerificationCandidate(
            kind=draft.kind,
            value=draft.value,
            confidence=_confidence_for(draft.kind, draft.reasons, single_candidate),
            reasons=draft.reasons,
            position=draft.position,
        )
        for draft in drafts
    ]
    return sorted(candidates, key=_candidate_sort_key)


def _extract_code_drafts(
    text: str,
    sender_is_known_ats: bool,
    has_verification_language: bool,
    url_spans: list[tuple[int, int]],
) -> list[_CandidateDraft]:
    drafts: list[_CandidateDraft] = []
    for match in _CODE_RE.finditer(text):
        value = match.group(0)
        if _span_inside(match.start(), match.end(), url_spans):
            continue
        if _looks_like_year(value):
            continue

        prefix = text[max(0, match.start() - 40) : match.start()]
        if _NEGATIVE_CODE_PREFIX_RE.search(prefix):
            continue
        if not _has_auth_code_context(
            text,
            match.start(),
            match.end(),
            sender_is_known_ats,
            has_verification_language,
        ):
            continue

        reasons = ["numeric_code", "nearby_verification_language", "auth_code_context"]
        if sender_is_known_ats:
            reasons.append("known_ats_sender")
        if has_verification_language:
            reasons.append("verification_language")
        drafts.append(_CandidateDraft(kind="code", value=value, reasons=tuple(reasons), position=match.start()))
    return drafts


def _extract_magic_link_drafts(
    text: str,
    sender_is_known_ats: bool,
    has_verification_language: bool,
) -> list[_CandidateDraft]:
    drafts: list[_CandidateDraft] = []
    for match in _URL_RE.finditer(text):
        value = _clean_url(match.group(0))
        domain = url_domain(value)
        known_ats_link = is_known_ats_domain(domain)
        if _is_tracking_or_click_wrapper(value, domain):
            continue
        if _is_rejected_magic_link_path(value):
            continue

        window = text[max(0, match.start() - 80) : min(len(text), match.end() + 80)]
        strong_auth_link = _has_strong_auth_url_signal(value)
        generic_auth_link = _has_generic_auth_url_path(value)
        has_context = _has_magic_link_context(window)
        if not (strong_auth_link or has_context):
            continue

        reasons = ["magic_link"]
        if strong_auth_link:
            reasons.append("strong_auth_link")
        if generic_auth_link:
            reasons.append("generic_auth_link")
        if sender_is_known_ats:
            reasons.append("known_ats_sender")
        if known_ats_link:
            reasons.append("known_ats_link")
        if has_verification_language:
            reasons.append("verification_language")
        drafts.append(_CandidateDraft(kind="magic_link", value=value, reasons=tuple(reasons), position=match.start()))
    return drafts


def _combined_text(subject: str, body: str) -> str:
    return f"{subject or ''}\n{body or ''}"


def _normalize_domain(domain: str) -> str:
    return (domain or "").strip().strip("<>[]()").lower().rstrip(".")


def _has_verification_language(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in VERIFY_WORDS)


def _has_auth_code_context(
    text: str,
    start: int,
    end: int,
    sender_is_known_ats: bool,
    has_verification_language: bool,
) -> bool:
    prefix = text[max(0, start - 120) : start]
    suffix = text[end : min(len(text), end + 120)]
    if _NEGATIVE_CODE_SUFFIX_RE.search(suffix):
        return False
    if _AUTH_CODE_BEFORE_RE.search(prefix):
        return True
    if _PLAIN_CODE_BEFORE_RE.search(prefix):
        return sender_is_known_ats and has_verification_language
    if _AUTH_CODE_FIRST_AFTER_RE.search(suffix):
        return True
    return bool(_CODE_COMMAND_BEFORE_RE.search(prefix) and _AUTH_CODE_AFTER_RE.search(suffix))


def _has_magic_link_context(text: str) -> bool:
    return bool(_MAGIC_LINK_CONTEXT_RE.search(text))


def _has_strong_auth_url_signal(url: str) -> bool:
    return _has_strong_auth_url_path(url) or _has_auth_url_query(url)


def _has_strong_auth_url_path(url: str) -> bool:
    parsed = urlparse(url)
    return bool(_STRONG_AUTH_URL_PATH_RE.search(parsed.path.lower()))


def _has_auth_url_query(url: str) -> bool:
    parsed = urlparse(url)
    return bool(_AUTH_URL_QUERY_RE.search(parsed.query.lower()))


def _has_generic_auth_url_path(url: str) -> bool:
    parsed = urlparse(url)
    return bool(_GENERIC_AUTH_URL_PATH_RE.search(parsed.path.lower()))


def _is_rejected_magic_link_path(url: str) -> bool:
    if _has_strong_auth_url_path(url):
        return False
    parsed = urlparse(url)
    return bool(_REJECTED_MAGIC_PATH_RE.search(parsed.path.lower()))


def _is_tracking_or_click_wrapper(url: str, domain: str) -> bool:
    normalized_domain = _normalize_domain(domain)
    labels = set(normalized_domain.split("."))
    if labels & _TRACKING_DOMAIN_LABELS:
        return True

    parsed = urlparse(url)
    path_query = f"{parsed.path}?{parsed.query}".lower()
    return bool(_TRACKING_PATH_RE.search(path_query))


def _span_inside(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(span_start <= start and end <= span_end for span_start, span_end in spans)


def _looks_like_year(value: str) -> bool:
    return len(value) == 4 and (value.startswith("19") or value.startswith("20"))


def _clean_url(url: str) -> str:
    return url.rstrip(".,;:!?)]}'\"")


def _dedupe_drafts(drafts: list[_CandidateDraft]) -> list[_CandidateDraft]:
    by_key: dict[tuple[CandidateKind, str], _CandidateDraft] = {}
    for draft in drafts:
        key = (draft.kind, draft.value)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = draft
            continue

        reasons = tuple(dict.fromkeys((*existing.reasons, *draft.reasons)))
        by_key[key] = _CandidateDraft(
            kind=existing.kind,
            value=existing.value,
            reasons=reasons,
            position=min(existing.position, draft.position),
        )
    return list(by_key.values())


def _confidence_for(kind: CandidateKind, reasons: tuple[str, ...], single_candidate: bool) -> Confidence:
    reason_set = set(reasons)
    if kind == "magic_link":
        if (
            "known_ats_link" in reason_set
            and "strong_auth_link" in reason_set
            and "verification_language" in reason_set
            and single_candidate
        ):
            return "high"
        if "verification_language" in reason_set and ("known_ats_sender" in reason_set or "known_ats_link" in reason_set):
            return "medium"
        if "verification_language" in reason_set:
            return "medium"
        return "low"

    if "known_ats_sender" in reason_set and "verification_language" in reason_set and single_candidate:
        return "high"
    if "known_ats_link" in reason_set and "verification_language" in reason_set and single_candidate:
        return "high"
    if "verification_language" in reason_set and (
        "known_ats_sender" in reason_set
        or "known_ats_link" in reason_set
        or "nearby_verification_language" in reason_set
    ):
        return "medium"
    if "verification_language" in reason_set:
        return "medium"
    return "low"


def _candidate_sort_key(candidate: VerificationCandidate) -> tuple[int, int, int, str]:
    kind_order = 0 if candidate.kind == "code" else 1
    return (-_CONFIDENCE_ORDER[candidate.confidence], kind_order, candidate.position, candidate.value)


ACTIVE_CHALLENGE_STATUSES = ("pending", "watching")
FINAL_CHALLENGE_STATUSES = ("resolved", "expired", "failed")


def _headers(payload: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h.get("value", "") for h in payload.get("headers", []) if h.get("name")}


def _payload_text(payload: dict[str, Any]) -> str:
    mime = payload.get("mimeType", "")
    body = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and body:
        padded = body + "=" * ((4 - len(body) % 4) % 4)
        return base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")

    if mime == "text/html" and body:
        padded = body + "=" * ((4 - len(body) % 4) % 4)
        raw = base64.urlsafe_b64decode(padded).decode("utf-8", errors="replace")
        return re.sub(r"<[^>]+>", " ", raw)

    pieces: list[str] = []
    for part in payload.get("parts", []):
        extracted = _payload_text(part)
        if extracted:
            pieces.append(extracted)
    return "\n".join(pieces)


def _high_confidence_matches(
    *,
    message_id: str,
    thread_id: str | None,
    subject: str,
    sender: str,
    received_at: str | None,
    body: str,
) -> list[AuthEmailMatch]:
    """Run the FROZEN parser (extract_verification_candidates) over one message's
    subject/body/sender and build AuthEmailMatch rows for the high-confidence
    candidates. Shared by both the Gmail-API `service` path and the MailMessage
    (`messages=`) path so the parsing/confidence logic never forks."""
    matches: list[AuthEmailMatch] = []
    candidates = extract_verification_candidates(subject, body, sender)
    for candidate in candidates:
        if candidate.confidence != "high":
            continue
        matches.append(
            AuthEmailMatch(
                message_id=message_id,
                thread_id=thread_id,
                sender=sender,
                subject=subject,
                received_at=received_at,
                snippet=body[:240].replace("\n", " ").strip(),
                candidate=candidate,
                reasons=candidate.reasons,
            )
        )
    return matches


def scan_gmail_for_auth_codes(
    *,
    service=None,
    messages=None,
    minutes: int = 10,
    max_messages: int = 25,
) -> list[AuthEmailMatch]:
    """Scan for high-confidence auth codes/magic links.

    Two mutually exclusive input paths:
    - `messages`: a list[MailMessage] (from get_mail_source().fetch(...)) --
      iterated directly through the frozen parser.
    - `service`: legacy Gmail-API service object -- back-compat path.
    """
    if messages is not None:
        matches: list[AuthEmailMatch] = []
        seen_threads: set[str] = set()
        for m in messages:
            thread_id = m.thread_id or m.id
            if thread_id in seen_threads:
                continue
            seen_threads.add(thread_id)
            matches.extend(
                _high_confidence_matches(
                    message_id=m.id,
                    thread_id=m.thread_id,
                    subject=m.subject,
                    sender=m.sender,
                    received_at=m.date,
                    body=m.body,
                )
            )
        return matches

    window_minutes = max(1, int(minutes))
    max_older_days = max(1, (window_minutes + 1439) // 1440)
    query = (
        f'newer_than:{max_older_days}d (verification OR verify OR code OR "one-time" '
        f'OR "one time" OR "confirm your email" OR "magic link")'
    )
    gmail_messages = (
        service.users()
        .messages()
        .list(userId="me", q=query, maxResults=max_messages)
        .execute()
        .get("messages", [])
    )

    matches = []
    seen_threads = set()
    for ref in gmail_messages:
        thread_id = ref.get("threadId", ref["id"])
        if thread_id in seen_threads:
            continue
        seen_threads.add(thread_id)

        msg = (
            service.users()
            .messages()
            .get(userId="me", id=ref["id"], format="full")
            .execute()
        )
        payload = msg.get("payload", {})
        hdrs = _headers(payload)
        subject = hdrs.get("subject", "")
        sender = hdrs.get("from", "")
        received_at = hdrs.get("date")
        body = _payload_text(payload)

        matches.extend(
            _high_confidence_matches(
                message_id=ref["id"],
                thread_id=ref.get("threadId"),
                subject=subject,
                sender=sender,
                received_at=received_at,
                body=body,
            )
        )

    return matches


def watch_gmail_for_auth_code(
    *,
    service=None,
    timeout_seconds: int = 300,
    poll_seconds: int = 5,
    max_errors: int = 3,
    minutes: int = 10,
    max_messages: int = 25,
) -> AuthEmailMatch | None:
    deadline = time.monotonic() + timeout_seconds
    errors = 0
    while time.monotonic() < deadline:
        try:
            if service is None:
                from applypilot.mail_source import get_mail_source

                since_days = max(1, (minutes + 1439) // 1440)
                msgs = get_mail_source().fetch(since_days=since_days, max_messages=max_messages)
                matches = scan_gmail_for_auth_codes(
                    messages=msgs,
                    minutes=minutes,
                    max_messages=max_messages,
                )
            else:
                matches = scan_gmail_for_auth_codes(
                    service=service,
                    minutes=minutes,
                    max_messages=max_messages,
                )
            if matches:
                return matches[0]
            errors = 0
        except Exception:
            errors += 1
            if errors >= max_errors:
                return None
        time.sleep(max(0.0, poll_seconds))
    return None


def create_auth_challenge(
    job_url: str,
    application_url: str,
    provider: str,
    challenge_type: str = "email_code",
    ttl_seconds: int = 300,
) -> int:
    """Create or reuse an active auth challenge and return its row id.

    If an active challenge already exists for the same job/application/provider/type,
    return the existing row id so repeated retries do not create duplicates.
    """
    conn = get_connection()
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=ttl_seconds)

    existing = conn.execute(
        """
        SELECT id FROM auth_challenges
        WHERE job_url = ?
          AND application_url = ?
          AND provider = ?
          AND challenge_type = ?
          AND status IN (?, ?)
        ORDER BY requested_at DESC
        LIMIT 1
        """,
        (job_url, application_url, provider, challenge_type, *ACTIVE_CHALLENGE_STATUSES),
    ).fetchone()
    if existing is not None:
        return int(existing[0])

    cursor = conn.execute(
        """
        INSERT INTO auth_challenges (
            job_url, application_url, provider, challenge_type, status,
            requested_at, expires_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        """,
        (
            job_url,
            application_url,
            provider,
            challenge_type,
            now.isoformat(),
            expires.isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def list_auth_challenges(
    status: str | None = None,
    job_url: str | None = None,
    provider: str | None = None,
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Return challenge rows, optionally filtered, ordered by requested time."""
    conn = get_connection()
    clauses = ["1 = 1"]
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if job_url:
        clauses.append("job_url = ?")
        params.append(job_url)
    if provider:
        clauses.append("provider = ?")
        params.append(provider)

    sql = (
        """
        SELECT id, job_url, application_url, provider, challenge_type, status,
               requested_at, expires_at, resolved_at, attempt_count,
               inbox_event_id, last_error, created_at, updated_at
        FROM auth_challenges
        WHERE """
        + " AND ".join(clauses)
        + " ORDER BY requested_at DESC"
    )
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)

    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def set_auth_challenge_status(
    challenge_id: int,
    status: str,
    last_error: str | None = None,
) -> bool:
    """Update a challenge status and timestamps; return True when a row changes."""
    now = now_utc()
    conn = get_connection()
    if status == "resolved":
        cursor = conn.execute(
            """
            UPDATE auth_challenges
               SET status = ?,
                   resolved_at = ?,
                   last_error = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (status, now, last_error, now, challenge_id),
        )
    else:
        cursor = conn.execute(
            """
            UPDATE auth_challenges
               SET status = ?,
                   last_error = ?,
                   updated_at = ?
             WHERE id = ?
            """,
            (status, last_error, now, challenge_id),
        )
    conn.commit()
    return int(cursor.rowcount or 0) == 1


def mark_auth_challenge_attempt(
    challenge_id: int,
    last_error: str | None = None,
) -> bool:
    """Increment attempt_count and persist the latest observation error."""
    conn = get_connection()
    now = now_utc()
    cursor = conn.execute(
        """
        UPDATE auth_challenges
           SET attempt_count = attempt_count + 1,
               last_error = COALESCE(?, last_error),
               updated_at = ?
         WHERE id = ?
        """,
        (last_error, now, challenge_id),
    )
    conn.commit()
    return int(cursor.rowcount or 0) == 1


def record_inbox_event(
    message_id: str,
    thread_id: str | None = None,
    sender: str | None = None,
    subject: str | None = None,
    event_type: str = "auth_code",
    confidence: Confidence = "low",
    matched_job_url: str | None = None,
    matched_company: str | None = None,
    matched_method: str | None = None,
    snippet: str | None = None,
    received_at: str | None = None,
) -> int:
    """Record an inbox verification event by message id.

    Repeated calls with the same message_id are idempotent and return the same row id.
    """
    conn = get_connection()
    created_at = now_utc()
    normalized_sender_domain = sender_domain(sender or "")

    try:
        cursor = conn.execute(
            """
            INSERT INTO inbox_events (
                message_id, thread_id, sender, sender_domain, subject,
                received_at, event_type, confidence, matched_job_url,
                matched_company, matched_method, snippet, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                thread_id,
                sender,
                normalized_sender_domain,
                subject,
                received_at or now_utc(),
                event_type,
                confidence,
                matched_job_url,
                matched_company,
                matched_method,
                snippet,
                created_at,
            ),
        )
        conn.commit()
        return int(cursor.lastrowid)
    except sqlite3.IntegrityError:
        row = conn.execute(
            "SELECT id FROM inbox_events WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            raise
        return int(row[0])


def resolve_auth_challenge(challenge_id: int, inbox_event_id: int) -> bool:
    """Resolve a challenge only if it's still awaiting completion."""
    conn = get_connection()
    now = now_utc()
    cursor = conn.execute(
        """
        UPDATE auth_challenges
           SET status = 'resolved',
               resolved_at = ?,
               inbox_event_id = ?,
               updated_at = ?,
               last_error = NULL
         WHERE id = ? AND status IN ('pending', 'watching')
        """,
        (now, inbox_event_id, now, challenge_id),
    )
    conn.commit()
    return int(cursor.rowcount or 0) == 1


def expire_stale_challenges() -> int:
    """Mark expired pending/watching challenges as expired."""
    conn = get_connection()
    now = now_utc()
    cursor = conn.execute(
        """
        UPDATE auth_challenges
           SET status = 'expired',
               updated_at = ?,
               last_error = COALESCE(last_error, 'expired')
         WHERE status IN ('pending', 'watching')
           AND expires_at <= ?
        """,
        (now, now),
    )
    conn.commit()
    return int(cursor.rowcount or 0)
