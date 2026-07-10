"""Pure, fail-closed verification of application submission evidence."""
from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


_CONFIRMATION_MARKERS = (
    "thank you for applying",
    "thanks for applying",
    "application received",
    "application submitted",
    "your application has been submitted",
    "we've received your application",
    "we have received your application",
    "successfully submitted",
)


@dataclass(frozen=True)
class SubmissionEvidence:
    response_ok: bool = False
    response_id: str | None = None
    response_url: str | None = None
    allowed_response_hosts: tuple[str, ...] = ()
    page_url: str | None = None
    allowed_success_hosts: tuple[str, ...] = ()
    success_url_markers: tuple[str, ...] = ()
    dom_text: str | None = None
    confirmation_email_ref: str | None = None
    validation_errors: tuple[str, ...] = ()
    screenshot_present: bool = False
    submit_button_disabled: bool = False


@dataclass(frozen=True)
class VerificationResult:
    status: str
    method: str | None = None
    reference: str | None = None


def _normalized(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _host_allowed(url: str | None, allowed: tuple[str, ...]) -> bool:
    try:
        host = (urlsplit(url or "").hostname or "").strip().lower()
    except ValueError:
        return False
    return bool(
        host
        and any(host == item.lower() or host.endswith(f".{item.lower()}") for item in allowed)
    )


def _confirmation_marker(text: str | None) -> str | None:
    normalized = _normalized(text)
    return next((marker for marker in _CONFIRMATION_MARKERS if marker in normalized), None)


def verify_submission(evidence: SubmissionEvidence) -> VerificationResult:
    """Return verified only from explicit allowlisted positive evidence."""
    response_id = (evidence.response_id or "").strip()
    if (
        evidence.response_ok
        and response_id
        and _host_allowed(evidence.response_url, evidence.allowed_response_hosts)
    ):
        return VerificationResult("verified", "response_id", response_id)

    errors = tuple(item.strip() for item in evidence.validation_errors if item.strip())
    if errors:
        return VerificationResult("contradicted", "validation_error", errors[0][:300])

    marker = _confirmation_marker(evidence.dom_text)
    success_url = evidence.page_url or ""
    url_marker = next(
        (item for item in evidence.success_url_markers if item and item in success_url),
        None,
    )
    if (
        marker
        and url_marker
        and _host_allowed(success_url, evidence.allowed_success_hosts)
    ):
        return VerificationResult("verified", "success_url_dom", success_url[:500])
    if marker:
        return VerificationResult("verified", "confirmation_dom", marker)

    email_ref = (evidence.confirmation_email_ref or "").strip()
    if email_ref:
        return VerificationResult("verified", "confirmation_email", email_ref[:500])

    return VerificationResult("unverified")

