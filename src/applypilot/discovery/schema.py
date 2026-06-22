"""Pydantic data contracts for scraped job listings.

Validates job dicts at the point of scrape ingestion so that selector drift or
anti-bot placeholder substitutions (HTTP 200 with empty body) surface
immediately as a WARNING rather than silently accumulating null rows for days.

Key insight from production scraping research: CSS selectors can silently return
None without raising an exception (BeautifulSoup find/select behaviour), meaning
boards can degrade for weeks before anyone notices. A null-rate metric that
fires on the SAME run as the drift is the earliest possible detection.

Usage in a discoverer's _store_jobs():
    from applypilot.discovery.schema import validate_jobs

    valid_jobs, report = validate_jobs(raw_jobs, board="remoteok")
    # report["null_rate"] is fraction of valid jobs missing ≥1 signal field
    # valid_jobs have been normalised (url stripped, etc.)
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, field_validator

log = logging.getLogger(__name__)

# Fields we track null-rate for.  These are most likely to silently degrade
# when a board's API changes or returns anti-bot placeholder content.
SIGNAL_FIELDS: tuple[str, ...] = ("title", "full_description", "location", "company")

# Log a WARNING when this fraction of a batch is missing at least one SIGNAL_FIELD.
_NULL_RATE_WARN_THRESHOLD = 0.20


class JobListing(BaseModel):
    """Minimal validated shape for a discovered job listing.

    Extra fields (strategy, source_board, department, …) pass through unchanged
    via extra="allow" so no caller needs to be updated to add new fields.
    """

    model_config = {"extra": "allow"}

    url: str
    title: str | None = None
    description: str | None = None
    full_description: str | None = None
    location: str | None = None
    site: str | None = None
    company: str | None = None
    salary: str | None = None
    application_url: str | None = None
    source_board: str | None = None
    strategy: str | None = None

    @field_validator("url")
    @classmethod
    def url_not_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("url must not be empty")
        return v.strip()


def validate_jobs(
    jobs: list[dict[str, Any]],
    board: str = "unknown",
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Validate a batch of scraped jobs and return (valid_jobs, null_rate_report).

    Jobs with an invalid/empty url are dropped entirely.
    Jobs with a valid url but missing signal fields are kept but counted in the
    null-rate report so callers can log/alert on selector drift.

    Returns:
        (valid_jobs, report)

        report keys:
            board         — source board name
            total         — input count
            valid         — jobs kept
            dropped_url   — jobs dropped for missing/empty url
            null_counts   — {field: count_of_jobs_missing_that_field}
            null_rate     — fraction of valid jobs missing ≥1 SIGNAL_FIELD
    """
    valid: list[dict[str, Any]] = []
    dropped_url = 0
    null_counts: dict[str, int] = {f: 0 for f in SIGNAL_FIELDS}
    with_nulls = 0

    for raw in jobs:
        try:
            listing = JobListing.model_validate(raw)
        except Exception:
            dropped_url += 1
            continue

        job = listing.model_dump()
        has_null = False
        for field in SIGNAL_FIELDS:
            val = job.get(field)
            if val is None or (isinstance(val, str) and not val.strip()):
                null_counts[field] += 1
                has_null = True
        if has_null:
            with_nulls += 1
        valid.append(job)

    null_rate = with_nulls / len(valid) if valid else 0.0

    if dropped_url:
        log.warning(
            "[schema:%s] %d/%d jobs dropped — missing or empty url",
            board, dropped_url, len(jobs),
        )
    if null_rate >= _NULL_RATE_WARN_THRESHOLD:
        missing_summary = ", ".join(
            f"{f}:{null_counts[f]}" for f in SIGNAL_FIELDS if null_counts.get(f)
        )
        log.warning(
            "[schema:%s] High null rate: %.0f%% of %d jobs are missing key fields (%s). "
            "Possible selector drift or anti-bot substitution.",
            board,
            null_rate * 100,
            len(valid),
            missing_summary,
        )

    return valid, {
        "board": board,
        "total": len(jobs),
        "valid": len(valid),
        "dropped_url": dropped_url,
        "null_counts": null_counts,
        "null_rate": round(null_rate, 4),
    }
