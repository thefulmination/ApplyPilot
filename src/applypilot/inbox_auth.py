from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Literal
from urllib.parse import urlparse

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
_AUTH_URL_PATH_RE = re.compile(
    r"(^|[/?&_.=-])(?:verify|verification|magic|magic-link|signin|sign-in|login|continue)([/?&_.=-]|$)",
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
    r"^\s*(?:when|for|as)\b.{0,60}\b(?:support|contact|contacting|reference)\b",
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
        if not (_has_auth_url_path(value) or _has_magic_link_context(window)):
            continue

        reasons = ["magic_link"]
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


def _has_auth_url_path(url: str) -> bool:
    parsed = urlparse(url)
    return bool(_AUTH_URL_PATH_RE.search(parsed.path.lower()))


def _is_rejected_magic_link_path(url: str) -> bool:
    if _has_auth_url_path(url):
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
        if "known_ats_link" in reason_set and "verification_language" in reason_set and single_candidate:
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
