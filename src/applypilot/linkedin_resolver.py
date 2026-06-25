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

CHALLENGE_TEXTS = (
    "security check",
    "verify it's you",
    "verify it is you",
    "quick security check",
    "restricted your account",
    "verify your identity",
    "unusual activity",
    "captcha",
    "checkpoint",
)

LOGIN_TEXTS = (
    "sign in to",
    "sign in to view",
    "sign in to continue",
    "join linkedin",
    "email or phone",
    "sign in",
    "log in",
    "linkedin login",
)

UNAVAILABLE_TEXTS = (
    "no longer accepting applications",
    "no longer accepting applications on linkedin",
    "this job is no longer accepting applications",
    "this job is no longer available",
    "this job has expired",
    "we couldn't find a match",
)


@dataclass(frozen=True)
class Candidate:
    url: str
    title: str | None
    company: str | None
    application_url: str | None
    audit_label: str | None
    audit_score: float | None
    fit_score: int | None


@dataclass(frozen=True)
class ApplyControl:
    text: str
    href: str | None
    selector: str


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    text: str
    controls: tuple[ApplyControl, ...]


@dataclass(frozen=True)
class PageDecision:
    status: str
    stop_run: bool = False
    final_url: str | None = None
    error: str | None = None
    control: ApplyControl | None = None


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


def _snapshot_text_lower(snapshot: PageSnapshot) -> str:
    return (snapshot.text or "").lower()


def classify_snapshot(snapshot: PageSnapshot) -> PageDecision:
    url = snapshot.url.lower()
    text = _snapshot_text_lower(snapshot)

    if "/checkpoint/" in url or "/uas/" in url:
        return PageDecision(status="challenge_required", stop_run=True, error="linkedin_checkpoint")

    if any(token in text for token in CHALLENGE_TEXTS):
        return PageDecision(status="challenge_required", stop_run=True, error="linkedin_challenge")

    if "linkedin.com/login" in url or any(token in text for token in LOGIN_TEXTS):
        return PageDecision(status="login_required", stop_run=True, error="linkedin_login")

    if any(token in text for token in UNAVAILABLE_TEXTS):
        return PageDecision(status="unavailable")

    easy_apply = next(
        (control for control in snapshot.controls if "easy apply" in control.text.lower()),
        None,
    )
    if easy_apply is not None:
        return PageDecision(status="easy_apply", control=easy_apply)

    apply_control = next(
        (control for control in snapshot.controls if "apply" in control.text.lower()),
        None,
    )
    if apply_control is None:
        return PageDecision(status="no_apply_button")

    if is_external_apply_url(apply_control.href):
        return PageDecision(
            status="resolved_offsite",
            final_url=apply_control.href,
            control=apply_control,
        )

    return PageDecision(status="needs_click", control=apply_control)


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


def _fetch_candidate_page(
    conn: sqlite3.Connection,
    *,
    query: str,
    params: list[str | int],
    page_size: int,
    offset: int,
) -> list[sqlite3.Row]:
    return conn.execute(query, (*params, page_size, offset)).fetchall()


def fetch_candidates(
    *,
    limit: int,
    tiers: Iterable[str] | None = ("priority", "recommended"),
    include_low: bool = False,
    refresh: bool = False,
    max_scan_rows: int | None = None,
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
           COALESCE(discovered_at, '') DESC,
           url ASC
         LIMIT ? OFFSET ?
    """

    params: list[str | int] = [*wanted_tiers]
    if not refresh:
        params.extend(completed_params)

    if limit <= 0:
        return []

    max_scan_rows = max(500, limit * 50) if max_scan_rows is None else max_scan_rows
    if max_scan_rows <= 0:
        return []

    max_page = 100
    chunk_size = max(limit * 5, 10)
    if max_page > 0:
        chunk_size = min(chunk_size, max_page)

    offset = 0
    scanned_rows = 0
    candidates: list[Candidate] = []
    while len(candidates) < limit:
        page_cap = max_scan_rows - scanned_rows
        if page_cap <= 0:
            break
        current_page = min(chunk_size, page_cap)

        rows = _fetch_candidate_page(
            conn,
            query=query,
            params=params,
            page_size=current_page,
            offset=offset,
        )
        if not rows:
            break
        scanned_rows += len(rows)

        for row in rows:
            if not row["application_url"] or not is_external_apply_url(row["application_url"]):
                candidates.append(
                    Candidate(
                        url=row["url"],
                        title=row["title"],
                        company=row["company"],
                        application_url=row["application_url"],
                        audit_label=row["audit_label"],
                        audit_score=row["audit_score"],
                        fit_score=row["fit_score"],
                    )
                )
                if len(candidates) >= limit:
                    break

        if len(rows) < current_page or scanned_rows >= max_scan_rows:
            break
        offset += current_page

    return candidates[:limit]


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
    if existing is None:
        raise ValueError(f"Job not found: {url}")
    current_app_url = existing[0] if existing else None

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
