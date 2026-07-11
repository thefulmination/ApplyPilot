from __future__ import annotations

import base64
import html
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr, parsedate_to_datetime
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from applypilot.ats_domains import ATS_SENDER_DOMAINS, PROVIDER_DOMAIN_GROUPS
from applypilot.auth_matching import unique_assignments
from applypilot.database import get_connection

Confidence = Literal["low", "medium", "high"]
CandidateKind = Literal["code", "magic_link"]

KNOWN_ATS_DOMAINS = ATS_SENDER_DOMAINS

VERIFY_WORDS = {
    "verification",
    "verification code",
    "verify",
    "verify your email",
    "confirm your email",
    "confirm your identity",
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

AUTH_GMAIL_RAW_QUERY = (
    'verification OR verify OR code OR "one-time" OR "one time" '
    'OR "confirm your email" OR "magic link"'
)

_CODE_RE = re.compile(r"(?<![A-Za-z0-9-])\d{4,8}(?![A-Za-z0-9-])")
_ALNUM_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9-])(?=[A-Za-z0-9]{6,12}(?![A-Za-z0-9-]))"
    r"(?=[A-Za-z0-9]*[A-Za-z])(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{6,12}(?![A-Za-z0-9-])"
)
_URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
_STRONG_AUTH_URL_PATH_RE = re.compile(
    r"(^|[/?&_.=-])(?:verify|verification|magic|magic-link|continue|activate)([/?&_.=-]|$)",
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
    r"\b(?:verification|security|authentication|one[- ]time)\s+(?:code|pass\s*code)\s*(?:is\s*:?|:)?\s*$"
    r"|\b(?:otp|pass\s*code)\s*(?:is\s*:?|:)?\s*$"
    r"|\b(?:to\s+)?(?:verify|confirm)\s+your\s+email,?\s*(?:please\s+)?(?:enter|use)\s*$",
    re.IGNORECASE,
)
_CODE_COMMAND_BEFORE_RE = re.compile(r"\b(?:enter|use)\s*$", re.IGNORECASE)
_PLAIN_CODE_BEFORE_RE = re.compile(r"\b(?:your\s+)?code\s*(?:is\s*:?|:)?\s*$", re.IGNORECASE)
_CODE_FIELD_BEFORE_RE = re.compile(
    r"\b(?:copy\s+and\s+paste|enter|use)\s+(?:this\s+)?code\b"
    r"|\bsecurity\s+code\s+field\b",
    re.IGNORECASE,
)
_AUTH_CODE_AFTER_RE = re.compile(
    r"^\s*(?:to\s+)?(?:verify|confirm)\s+your\s+email\b"
    r"|^\s*(?:to\s+)?confirm\s+your\s+identity\b"
    r"|^\s*(?:to\s+)?(?:sign\s+in|continue\s+your\s+application)\b",
    re.IGNORECASE,
)
_AUTH_CODE_FIRST_AFTER_RE = re.compile(
    r"^\s*is\s+your\s+(?:verification|security|authentication|one[- ]time)\s+(?:code|pass\s*code)\b",
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


def domains_related(left: str | None, right: str | None) -> bool:
    left = _normalize_domain(left or "")
    right = _normalize_domain(right or "")
    if not left or not right:
        return False
    if left == right or left.endswith(f".{right}") or right.endswith(f".{left}"):
        return True
    return any(
        any(left == domain or left.endswith(f".{domain}") for domain in group)
        and any(right == domain or right.endswith(f".{domain}") for domain in group)
        for group in PROVIDER_DOMAIN_GROUPS
    )


def match_belongs_to_provider(
    match: AuthEmailMatch, provider_domain: str | None
) -> bool:
    if not provider_domain:
        return True
    sender = sender_domain(getattr(match, "sender", "") or "")
    candidate = getattr(match, "candidate", None)
    if getattr(candidate, "kind", None) == "magic_link":
        destination = url_domain(getattr(candidate, "value", "") or "")
        if not destination or not domains_related(provider_domain, destination):
            return False
        sender_is_provider = is_known_ats_domain(sender) or any(
            sender == domain or sender.endswith(f".{domain}")
            for group in PROVIDER_DOMAIN_GROUPS
            for domain in group
        )
        if sender_is_provider and not domains_related(provider_domain, sender):
            return False
        return True
    return bool(sender) and domains_related(provider_domain, sender)


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
    for match in list(_CODE_RE.finditer(text)) + list(_ALNUM_CODE_RE.finditer(text)):
        value = match.group(0)
        if _span_inside(match.start(), match.end(), url_spans):
            continue
        if _looks_like_year(value):
            continue

        short_prefix = text[max(0, match.start() - 40) : match.start()]
        context_prefix = text[max(0, match.start() - 120) : match.start()]
        if _NEGATIVE_CODE_PREFIX_RE.search(short_prefix):
            continue
        if not value.isdigit() and not _CODE_FIELD_BEFORE_RE.search(context_prefix):
            continue
        if not _has_auth_code_context(
            text,
            match.start(),
            match.end(),
            sender_is_known_ats,
            has_verification_language,
        ):
            continue

        reasons = [
            "numeric_code" if value.isdigit() else "alphanumeric_code",
            "nearby_verification_language",
            "auth_code_context",
        ]
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
        window = text[max(0, match.start() - 80) : min(len(text), match.end() + 80)]
        strong_auth_path = _has_strong_auth_url_path(value)
        strong_auth_link = _has_strong_auth_url_signal(value)
        generic_auth_link = _has_generic_auth_url_path(value)
        if _is_tracking_or_click_wrapper(value, domain) and not strong_auth_path:
            continue
        if _is_rejected_magic_link_path(value):
            continue

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
    return re.sub(r"\s+", " ", html.unescape(f"{subject or ''}\n{body or ''}"))


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
    if _CODE_FIELD_BEFORE_RE.search(prefix):
        return sender_is_known_ats or has_verification_language
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


def _received_at_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _received_at_in_window(raw: str | None, *, cutoff: datetime) -> bool:
    received = _received_at_dt(raw)
    if received is None:
        return False
    return received >= cutoff


def eligible_auth_matches(
    matches: list[AuthEmailMatch],
    *,
    not_before: datetime | None = None,
    provider_domain: str | None = None,
    skew_seconds: int = 60,
    excluded_message_ids: set[str] | None = None,
    reference_time: datetime | None = None,
) -> list[AuthEmailMatch]:
    excluded = excluded_message_ids or set()
    skew = max(0, skew_seconds)
    if reference_time is None:
        reference_time = datetime.now(timezone.utc)
    elif reference_time.tzinfo is None:
        reference_time = reference_time.replace(tzinfo=timezone.utc)
    reference_time = reference_time.astimezone(timezone.utc)
    ceiling = reference_time + timedelta(seconds=skew)
    floor = None
    if not_before is not None:
        if not_before.tzinfo is None:
            not_before = not_before.replace(tzinfo=timezone.utc)
        floor = not_before.astimezone(timezone.utc) - timedelta(
            seconds=skew
        )

    eligible = []
    for match in matches:
        received = _received_at_dt(match.received_at)
        if match.message_id in excluded or received is None:
            continue
        if floor is not None and received < floor:
            continue
        if received > ceiling:
            continue
        if not match_belongs_to_provider(match, provider_domain):
            continue
        eligible.append(match)
    return eligible


def scan_gmail_for_auth_codes(
    *,
    service=None,
    messages=None,
    minutes: int = 10,
    max_messages: int | str = 25,
) -> list[AuthEmailMatch]:
    """Scan for high-confidence auth codes/magic links.

    Two mutually exclusive input paths:
    - `messages`: a list[MailMessage] (from get_mail_source().fetch(...)) --
      iterated directly through the frozen parser.
    - `service`: legacy Gmail-API service object -- back-compat path.
    """
    from applypilot.mail_source import validate_max_messages

    budget = validate_max_messages(max_messages)
    if budget <= 0:
        return []

    window_minutes = max(1, int(minutes))
    if messages is None:
        from applypilot.mail_source import GmailApiMailSource

        max_older_days = max(1, (window_minutes + 1439) // 1440)
        messages = GmailApiMailSource(build_service=lambda: service).fetch(
            since_days=max_older_days,
            max_messages=budget,
            gmail_raw_query=AUTH_GMAIL_RAW_QUERY,
        )

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
    matches: list[AuthEmailMatch] = []
    for message in messages[:budget]:
        if not _received_at_in_window(message.date, cutoff=cutoff):
            continue
        matches.extend(
            _high_confidence_matches(
                message_id=message.id,
                thread_id=message.thread_id,
                subject=message.subject,
                sender=message.sender,
                received_at=message.date,
                body=message.body,
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
    max_messages: int | str = 25,
    not_before: datetime | None = None,
    provider_domain: str | None = None,
    skew_seconds: int = 60,
    excluded_message_ids: set[str] | None = None,
    claim_match: Callable[[AuthEmailMatch], bool] | None = None,
    claim_matches: Callable[[list[AuthEmailMatch]], AuthEmailMatch | None] | None = None,
) -> AuthEmailMatch | None:
    from applypilot.mail_source import validate_max_messages

    budget = validate_max_messages(max_messages)
    if budget <= 0:
        return None

    excluded = set(excluded_message_ids or ())
    deadline = time.monotonic() + timeout_seconds
    errors = 0
    while time.monotonic() < deadline:
        try:
            if service is None:
                from applypilot.mail_source import get_mail_source

                since_days = max(1, (minutes + 1439) // 1440)
                msgs = get_mail_source().fetch(
                    since_days=since_days,
                    max_messages=budget,
                    gmail_raw_query=AUTH_GMAIL_RAW_QUERY,
                )
                matches = scan_gmail_for_auth_codes(
                    messages=msgs,
                    minutes=minutes,
                    max_messages=budget,
                )
            else:
                matches = scan_gmail_for_auth_codes(
                    service=service,
                    minutes=minutes,
                    max_messages=budget,
                )
            matches = eligible_auth_matches(
                matches,
                not_before=None if claim_matches else not_before,
                provider_domain=None if claim_matches else provider_domain,
                skew_seconds=skew_seconds,
                excluded_message_ids=excluded,
            )
            matches.sort(
                key=lambda match: _received_at_dt(match.received_at)
                or datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            if claim_matches is not None:
                claimed = claim_matches(matches)
                if claimed is not None:
                    return claimed
                errors = 0
                time.sleep(max(0.0, poll_seconds))
                continue
            for match in matches:
                if claim_match is None or claim_match(match):
                    return match
                excluded.add(match.message_id)
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
                None,
                normalized_sender_domain,
                None,
                received_at or now_utc(),
                event_type,
                confidence,
                matched_job_url,
                matched_company,
                matched_method,
                None,
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


def _as_utc(value: datetime | None = None) -> datetime:
    result = value or datetime.now(timezone.utc)
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _parse_utc_timestamp(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def claimed_auth_message_ids(
    candidate_message_ids: set[str],
    *,
    connection: sqlite3.Connection | None = None,
) -> set[str]:
    """Return claimed IDs from a bounded current candidate set."""
    if len(candidate_message_ids) > 1000:
        raise ValueError("candidate_message_ids must contain <= 1000 values")
    conn = connection or get_connection()
    claimed: set[str] = set()
    candidates = sorted(candidate_message_ids)
    for offset in range(0, len(candidates), 900):
        chunk = candidates[offset:offset + 900]
        if not chunk:
            continue
        placeholders = ",".join("?" for _ in chunk)
        rows = conn.execute(
            f"""
            SELECT event.message_id
              FROM auth_challenges AS challenge
              JOIN inbox_events AS event ON event.id = challenge.inbox_event_id
             WHERE event.message_id IN ({placeholders})
            """,
            chunk,
        ).fetchall()
        claimed.update(str(row[0]) for row in rows)
    return claimed


def _claim_auth_match_in_transaction(
    conn: sqlite3.Connection,
    challenge_id: int,
    *,
    message_id: str,
    now_text: str,
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
) -> str:
    existing = conn.execute(
        """
        SELECT challenge.status, challenge.requested_at, challenge.expires_at,
               event.message_id
          FROM auth_challenges AS challenge
          LEFT JOIN inbox_events AS event ON event.id = challenge.inbox_event_id
         WHERE challenge.id = ?
        """,
        (challenge_id,),
    ).fetchone()
    if existing is None:
        return "rejected"
    if existing[0] == "resolved":
        return "idempotent" if existing[3] == message_id else "rejected"
    if existing[0] not in ACTIVE_CHALLENGE_STATUSES:
        return "rejected"
    requested_at = _parse_utc_timestamp(existing[1])
    expires_at = _parse_utc_timestamp(existing[2])
    reference = _parse_utc_timestamp(now_text)
    if expires_at <= reference or requested_at > reference:
        if expires_at <= reference:
            conn.execute(
                """
                UPDATE auth_challenges
                   SET status='expired', resolved_at=NULL,
                       last_error=COALESCE(last_error, 'expired'), updated_at=?
                 WHERE id=? AND status IN ('pending', 'watching')
                """,
                (now_text, challenge_id),
            )
            return "expired"
        return "rejected"

    conn.execute(
        """
        INSERT OR IGNORE INTO inbox_events (
            message_id, thread_id, sender, sender_domain, subject,
            received_at, event_type, confidence, matched_job_url,
            matched_company, matched_method, snippet, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id, thread_id, None, sender_domain(sender or ""), None,
            received_at or now_text, event_type, confidence, matched_job_url,
            matched_company, matched_method, None, now_text,
        ),
    )
    event_row = conn.execute(
        "SELECT id FROM inbox_events WHERE message_id = ?", (message_id,)
    ).fetchone()
    if event_row is None:
        raise sqlite3.IntegrityError("inbox event was not persisted")
    event_id = int(event_row[0])
    cursor = conn.execute(
        """
        UPDATE auth_challenges
           SET status='resolved', resolved_at=?, inbox_event_id=?, updated_at=?,
               last_error=NULL
         WHERE id=? AND status IN ('pending', 'watching')
           AND julianday(requested_at) <= julianday(?)
           AND julianday(expires_at) > julianday(?)
           AND NOT EXISTS (
               SELECT 1 FROM auth_challenges WHERE inbox_event_id=?
           )
        """,
        (now_text, event_id, now_text, challenge_id, now_text, now_text, event_id),
    )
    return "claimed" if int(cursor.rowcount or 0) == 1 else "rejected"


def claim_auth_match(
    challenge_id: int,
    *,
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
    connection: sqlite3.Connection | None = None,
    now: datetime | None = None,
) -> bool:
    """Commit one mailbox-message claim and challenge resolution atomically."""
    conn = connection or get_connection()
    now_text = _as_utc(now).isoformat()

    try:
        conn.execute("BEGIN IMMEDIATE")
        outcome = _claim_auth_match_in_transaction(
            conn,
            challenge_id,
            message_id=message_id,
            now_text=now_text,
            thread_id=thread_id,
            sender=sender,
            subject=subject,
            event_type=event_type,
            confidence=confidence,
            matched_job_url=matched_job_url,
            matched_company=matched_company,
            matched_method=matched_method,
            snippet=snippet,
            received_at=received_at,
        )
        if outcome in {"claimed", "expired"}:
            conn.commit()
        else:
            conn.rollback()
        return outcome in {"claimed", "idempotent"}
    except Exception:
        conn.rollback()
        raise


def claim_unique_auth_match(
    challenge_id: int,
    matches: list[AuthEmailMatch],
    *,
    now: datetime | None = None,
    skew_seconds: int = 60,
    connection: sqlite3.Connection | None = None,
) -> AuthEmailMatch | None:
    """Atomically claim this challenge's globally unique local assignment."""
    conn = connection or get_connection()
    reference = _as_utc(now)
    now_text = reference.isoformat()
    candidates: dict[str, tuple[AuthEmailMatch, datetime]] = {}
    overflow = False
    for match in matches:
        received = _received_at_dt(match.received_at)
        if match.message_id and received is not None:
            candidates.setdefault(match.message_id, (match, received))
        if len(candidates) > 1000:
            overflow = True
            break
    if not candidates or overflow:
        return None

    try:
        conn.execute("BEGIN IMMEDIATE")
        target = conn.execute(
            """
            SELECT challenge.status, event.message_id
              FROM auth_challenges AS challenge
              LEFT JOIN inbox_events AS event ON event.id=challenge.inbox_event_id
             WHERE challenge.id=?
            """,
            (challenge_id,),
        ).fetchone()
        if target is not None and target[0] == "resolved":
            selected = candidates.get(str(target[1]))
            conn.rollback()
            return selected[0] if selected is not None else None
        conn.execute(
            """
            UPDATE auth_challenges
               SET status='expired', resolved_at=NULL,
                   last_error=COALESCE(last_error, 'expired'), updated_at=?
             WHERE status IN ('pending', 'watching')
               AND julianday(expires_at) <= julianday(?)
            """,
            (now_text, now_text),
        )
        active = conn.execute(
            """
            SELECT id, job_url, provider, challenge_type, requested_at, expires_at
              FROM auth_challenges
             WHERE status IN ('pending', 'watching')
               AND julianday(requested_at) <= julianday(?)
               AND julianday(expires_at) > julianday(?)
             ORDER BY requested_at, id
             LIMIT 1001
            """,
            (now_text, now_text),
        ).fetchall()
        if len(active) > 1000:
            conn.commit()
            return None
        requests = [dict(row) for row in active]
        used = claimed_auth_message_ids(set(candidates), connection=conn)
        available = [item for identifier, item in candidates.items() if identifier not in used]

        def eligible(request, item):
            match, received = item
            requested = _parse_utc_timestamp(request["requested_at"])
            expected_kind = (
                "magic_link" if request["challenge_type"] == "magic_link" else "code"
            )
            return (
                received >= requested - timedelta(seconds=max(0, skew_seconds))
                and received <= reference + timedelta(seconds=max(0, skew_seconds))
                and match.candidate.kind == expected_kind
                and match_belongs_to_provider(match, request.get("provider"))
            )

        assignments = unique_assignments(
            requests,
            available,
            request_id=lambda request: request["id"],
            message_id=lambda item: item[0].message_id,
            eligible=eligible,
            max_items=1000,
        )
        selected = next(
            (item[0] for request, item in assignments if request["id"] == challenge_id),
            None,
        )
        if selected is None:
            conn.commit()
            return None
        request = next(request for request in requests if request["id"] == challenge_id)
        outcome = _claim_auth_match_in_transaction(
            conn,
            challenge_id,
            message_id=selected.message_id,
            now_text=now_text,
            thread_id=selected.thread_id,
            sender=selected.sender,
            subject=selected.subject,
            event_type="auth_code",
            confidence=selected.candidate.confidence,
            matched_job_url=request["job_url"],
            matched_method=selected.candidate.kind,
            snippet=selected.snippet,
            received_at=selected.received_at,
        )
        if outcome != "claimed":
            conn.rollback()
            return None
        conn.commit()
        return selected
    except Exception:
        conn.rollback()
        raise


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
