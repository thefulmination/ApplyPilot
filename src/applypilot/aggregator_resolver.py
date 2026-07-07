"""Shared deterministic helpers for aggregator apply URL resolvers."""

from __future__ import annotations

from urllib.parse import urlparse
from urllib.parse import ParseResult


SOURCE_PLATFORM_SUFFIXES = {
    "linkedin": ("linkedin.com",),
    "indeed": ("indeed.com",),
}

UNRESOLVED_NEXT_ACTIONS = {
    "auth_required": "refresh_session",
    "checkpoint_or_captcha": "pause_resolver",
    "rate_limited": "retry_later",
    "page_unreachable": "retry_later",
    "dom_unreadable": "retry_fresh_context",
    "apply_button_missing": "run_ats_reconstruction",
    "conflicting_signals": "run_conservative_dom_parser",
    "outbound_not_observed": "retry_with_network_capture",
    "outbound_still_source_platform": "run_url_unwrapper",
    "malformed_outbound_url": "extract_from_serialized_page_data",
    "ats_reconstruction_needed": "run_ats_reconstruction",
    "low_confidence_match": "manual_review",
}


def _parse_url(url: str | None) -> ParseResult | None:
    if not url:
        return None
    try:
        return urlparse(url)
    except Exception:
        return None


def host_of(url: str | None) -> str:
    parsed = _parse_url(url)
    if parsed is None:
        return ""
    return (parsed.hostname or "").lower()


def source_platform_from_url(url: str | None) -> str | None:
    host = host_of(url)
    for platform, suffixes in SOURCE_PLATFORM_SUFFIXES.items():
        if any(host == suffix or host.endswith(f".{suffix}") for suffix in suffixes):
            return platform
    return None


def is_source_platform_url(url: str | None) -> bool:
    return source_platform_from_url(url) is not None


def is_external_apply_url(url: str | None) -> bool:
    parsed = _parse_url(url)
    if parsed is None:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return not is_source_platform_url(url)


def next_action_for_unresolved_kind(kind: str | None) -> str | None:
    if not kind:
        return None
    return UNRESOLVED_NEXT_ACTIONS.get(kind)
