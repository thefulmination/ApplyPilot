"""Deterministic pre-agent job eligibility checks."""
from __future__ import annotations

import re

_REMOTE_RE = re.compile(r"\b(remote|work from home|work from anywhere|distributed|wfh)\b", re.I)
_US_RE = re.compile(
    r"\b(united states|u\.?s\.?a?|usa|california|new york|texas|florida|"
    r"massachusetts|washington|illinois|colorado|georgia|north carolina|virginia)\b",
    re.I,
)
_FOREIGN_RE = re.compile(
    r"\b(canada|united kingdom|uk|europe|germany|france|india|philippines|"
    r"australia|singapore|mexico|brazil|toronto|vancouver|london|berlin)\b",
    re.I,
)
_FOREIGN_ONLY_RE = re.compile(
    r"\b(?:remote\s+)?(?:only|must be (?:based|located)|residents? of|work authorization in)\b",
    re.I,
)
_NO_SPONSOR_RE = re.compile(
    r"\b(?:unable|cannot|can not|do not|does not|won't|will not)\s+(?:to\s+)?(?:offer|provide)?\s*"
    r"(?:visa\s+)?sponsor(?:ship)?\b|\bno\s+(?:visa\s+)?sponsorship\b",
    re.I,
)


def evaluate_job_eligibility(
    *,
    location: str | None,
    description: str | None,
    location_policy: dict | None = None,
    work_authorization: dict | None = None,
) -> tuple[str, str]:
    """Return ``(eligible|ineligible, reason)`` from explicit factual rules.

    Unknown or absent location data is not treated as an exclusion. This gate only
    rejects explicit contradictions and never asks a model to infer eligibility.
    """
    policy = location_policy or {}
    work_auth = work_authorization or {}
    location_text = (location or "").strip()
    description_text = (description or "").strip()
    combined = f"{location_text}\n{description_text}"
    low_location = location_text.lower()
    remote = bool(_REMOTE_RE.search(combined))

    needs_sponsorship = str(work_auth.get("require_sponsorship", "")).strip().lower()
    if needs_sponsorship in {"yes", "true", "1", "y"} and _NO_SPONSOR_RE.search(description_text):
        return "ineligible", "not_eligible_work_auth:no_sponsorship"

    reject_patterns = [str(value).strip().lower() for value in policy.get("reject_patterns", []) if value]
    for pattern in reject_patterns:
        if pattern in low_location and not remote:
            return "ineligible", f"not_eligible_location:{pattern}"[:200]

    foreign = _FOREIGN_RE.search(location_text)
    if foreign and not _US_RE.search(location_text) and not remote:
        return "ineligible", f"not_eligible_location:{foreign.group(0).lower()}"

    if remote and foreign and not _US_RE.search(combined) and _FOREIGN_ONLY_RE.search(combined):
        return "ineligible", f"not_eligible_work_auth:{foreign.group(0).lower()}_only"

    accept_patterns = [str(value).strip().lower() for value in policy.get("accept_patterns", []) if value]
    if remote:
        return "eligible", "remote"
    if policy.get("accept_any_us") and _US_RE.search(combined):
        return "eligible", "us_relocation_allowed"
    if any(pattern in low_location for pattern in accept_patterns):
        return "eligible", "accepted_location"
    return "eligible", "no_deterministic_exclusion"
