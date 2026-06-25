"""LinkedIn external apply URL resolver.

This module resolves LinkedIn job links that point to external ATS application
destinations before normal apply processing. It is intentionally read-only with
respect to job submission and should never submit applications or drive Easy
Apply workflows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from typing import Iterable
from urllib.parse import urlparse

from applypilot.database import get_connection

COMPLETED_STATUSES = {
    "resolved_offsite",
    "easy_apply",
    "login_required",
    "challenge_required",
    "unavailable",
}

STOP_STATUSES = {"login_required", "challenge_required"}


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str | None
    company: str | None
    application_url: str | None
    audit_label: str | None
    audit_score: float | None
    fit_score: int | None


def _host(url: str | None) -> str:
    if not url:
        return ""
    host = (urlparse(url).hostname or "").lower()
    return host[4:] if host.startswith("www.") else host


def is_linkedin_url(url: str | None) -> bool:
    host = _host(url)
    return host == "linkedin.com" or host.endswith(".linkedin.com")


def is_external_apply_url(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not is_linkedin_url(url)


def _normalize_tiers(tiers: Iterable[str] | None, include_low: bool) -> tuple[str, ...]:
    base = tuple(t.strip() for t in (tiers or ("priority", "recommended")) if t and t.strip())
    if include_low:
        # Keep "review" available for future callers while keeping order stable.
        return tuple(dict.fromkeys((*base, "review", "low")))
    return base or ("priority", "recommended")


def _build_completed_status_filter() -> str:
    placeholders = ",".join("?" for _ in sorted(COMPLETED_STATUSES))
    return (
        f"COALESCE(linkedin_resolve_status, '') NOT IN ({placeholders})"
        if placeholders
        else "1=1"
    )


def fetch_candidates(
    *,
    limit: int,
    tiers: Iterable[str] | None = ("priority", "recommended"),
    include_low: bool = False,
    refresh: bool = False,
    conn: sqlite3.Connection | None = None,
) -> list[Candidate]:
    if conn is None:
        conn = get_connection()

    wanted_tiers = _normalize_tiers(tiers, include_low)
    tier_marks = ",".join("?" for _ in wanted_tiers)
    completed_filter = _build_completed_status_filter()
    completed_params: list[str] = sorted(COMPLETED_STATUSES)

    query = f"""
        SELECT url, title, company, application_url, audit_label, audit_score, fit_score
          FROM jobs
         WHERE (lower(COALESCE(site, '')) = 'linkedin' OR url LIKE '%linkedin.com/jobs%')
           AND duplicate_of_url IS NULL
           AND COALESCE(liveness_status, '') != 'dead'
           AND applied_at IS NULL
           AND (
                application_url IS NULL
             OR application_url = ''
             OR application_url LIKE '%linkedin.com%'
           )
           AND COALESCE(audit_label, '') IN ({tier_marks})
    """

    if not refresh:
        query += f"    AND {completed_filter}\n"

    query += """
         ORDER BY
           CASE COALESCE(audit_label, '')
                WHEN 'priority' THEN 0
                WHEN 'recommended' THEN 1
                WHEN 'review' THEN 2
                WHEN 'low' THEN 3
                ELSE 4
           END,
           COALESCE(audit_score, -1) DESC,
           COALESCE(fit_score, -1) DESC,
           COALESCE(discovered_at, '') DESC
         LIMIT ?
    """

    params: list[str | int] = [*wanted_tiers]
    if not refresh:
        params.extend(completed_params)
    params.append(limit)

    rows = conn.execute(query, params).fetchall()

    return [
        Candidate(
            url=row["url"],
            title=row["title"],
            company=row["company"],
            application_url=row["application_url"],
            audit_label=row["audit_label"],
            audit_score=row["audit_score"],
            fit_score=row["fit_score"],
        )
        for row in rows
    ]


def record_resolution(
    url: str,
    *,
    status: str,
    final_url: str | None = None,
    error: str | None = None,
    refresh: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    if conn is None:
        conn = get_connection()

    now = datetime.now(timezone.utc).isoformat()
    existing = conn.execute(
        "SELECT application_url FROM jobs WHERE url = ?",
        (url,),
    ).fetchone()
    current_app_url = existing["application_url"] if existing else None

    should_set_application = (
        status == "resolved_offsite"
        and is_external_apply_url(final_url)
        and (refresh or not is_external_apply_url(current_app_url))
    )

    if should_set_application:
        conn.execute(
            """
            UPDATE jobs
               SET application_url = ?,
                   linkedin_resolve_final_url = ?,
                   linkedin_resolve_status = ?,
                   linkedin_resolve_error = ?,
                   linkedin_resolved_at = ?,
                   linkedin_resolve_attempts = COALESCE(linkedin_resolve_attempts, 0) + 1
             WHERE url = ?
            """,
            (final_url, final_url, status, error, now, url),
        )
    else:
        conn.execute(
            """
            UPDATE jobs
               SET linkedin_resolve_final_url = ?,
                   linkedin_resolve_status = ?,
                   linkedin_resolve_error = ?,
                   linkedin_resolved_at = ?,
                   linkedin_resolve_attempts = COALESCE(linkedin_resolve_attempts, 0) + 1
             WHERE url = ?
            """,
            (final_url, status, error, now, url),
        )
    conn.commit()


def should_stop_run(status: str) -> bool:
    return status in STOP_STATUSES
