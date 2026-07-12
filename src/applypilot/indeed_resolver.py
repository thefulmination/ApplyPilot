"""Indeed company-site apply URL resolver.

This module classifies Indeed job pages deterministically. It does not submit
applications and does not use LLM/OCR.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import sqlite3
from typing import Iterable

from applypilot.aggregator_resolver import (
    is_external_apply_url,
    next_action_for_unresolved_kind,
    source_platform_from_url,
)
from applypilot.database import get_connection


CHECKPOINT_TEXTS = (
    "captcha",
    "verify you are human",
    "verify that you are human",
    "security check",
    "unusual activity",
)

UNAVAILABLE_TEXTS = (
    "job is no longer available",
    "this job is no longer available",
    "job has expired",
    "this job has expired",
    "position has been filled",
)

INDEED_RESOLUTION_STRATEGY = "indeed_deterministic"


@dataclass(frozen=True)
class ApplyControl:
    text: str
    href: str | None
    selector: str
    aria_label: str | None = None


@dataclass(frozen=True)
class PageSnapshot:
    url: str
    body_text: str
    controls: tuple[ApplyControl, ...]


@dataclass(frozen=True)
class PageDecision:
    status: str
    final_url: str | None = None
    control: ApplyControl | None = None
    error: str | None = None
    unresolved_kind: str | None = None
    next_action: str | None = None


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
class IndeedResolverOptions:
    limit: int = 200
    tiers: tuple[str, ...] = ("priority", "recommended")
    include_low: bool = False
    refresh: bool = False
    dry_run: bool = False


@dataclass
class IndeedResolverSummary:
    considered: int = 0
    dry_run: bool = False
    counts: dict[str, int] | None = None
    unresolved_kinds: dict[str, int] | None = None
    sample_urls: list[str] | None = None

    def __post_init__(self) -> None:
        if self.counts is None:
            self.counts = {}
        if self.unresolved_kinds is None:
            self.unresolved_kinds = {}
        if self.sample_urls is None:
            self.sample_urls = []


def is_indeed_url(url: str | None) -> bool:
    return source_platform_from_url(url) == "indeed"


def is_hosted_indeed_apply_url(url: str | None) -> bool:
    text = str(url or "").lower()
    return is_indeed_url(url) and (
        "smartapply.indeed.com" in text
        or "/apply" in text
        or "/applystart" in text
        or "indeedapply" in text
    )


def unresolved_decision(
    kind: str,
    *,
    error: str | None = None,
    control: ApplyControl | None = None,
) -> PageDecision:
    return PageDecision(
        status="unresolved",
        error=error,
        control=control,
        unresolved_kind=kind,
        next_action=next_action_for_unresolved_kind(kind),
    )


def _control_text(control: ApplyControl) -> str:
    return f"{control.text or ''} {control.aria_label or ''}".lower()


def _snapshot_text(snapshot: PageSnapshot) -> str:
    return (snapshot.body_text or "").lower()


def classify_snapshot(snapshot: PageSnapshot) -> PageDecision:
    text = _snapshot_text(snapshot)
    controls = tuple(snapshot.controls or ())

    if any(token in text for token in CHECKPOINT_TEXTS):
        return unresolved_decision("checkpoint_or_captcha", error="indeed_checkpoint")

    if any(token in text for token in UNAVAILABLE_TEXTS):
        return PageDecision(status="unavailable", error="indeed_unavailable")

    for control in controls:
        label = _control_text(control)
        if is_external_apply_url(control.href):
            return PageDecision(
                status="resolved_offsite",
                final_url=control.href,
                control=control,
            )
        if "apply on company site" in label:
            return PageDecision(status="needs_click", control=control)
        if "apply now" in label or "easily apply" in label:
            return PageDecision(status="hosted_apply", control=control)

    if "apply on company site" in text:
        return PageDecision(status="needs_click")

    return unresolved_decision(
        "apply_button_missing",
        error="no_primary_apply_button",
    )


def classify_candidate(candidate: Candidate) -> PageDecision:
    application_url = candidate.application_url
    if is_external_apply_url(application_url):
        return PageDecision(status="resolved_offsite", final_url=application_url)
    if is_hosted_indeed_apply_url(application_url):
        return PageDecision(status="hosted_apply", final_url=application_url)
    if is_indeed_url(application_url) or is_indeed_url(candidate.url):
        return unresolved_decision(
            "ats_reconstruction_needed",
            error="browser_interaction_not_implemented",
        )
    return unresolved_decision(
        "apply_button_missing",
        error="not_enough_indeed_apply_metadata",
    )


def _normalize_tiers(tiers: Iterable[str] | None, include_low: bool) -> tuple[str, ...]:
    base = tuple(t.strip() for t in (tiers or ("priority", "recommended")) if t and t.strip())
    if include_low:
        return tuple(dict.fromkeys((*base, "review", "low")))
    return base or ("priority", "recommended")


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
    if limit <= 0:
        return []

    wanted_tiers = _normalize_tiers(tiers, include_low)
    tier_marks = ",".join("?" for _ in wanted_tiers)
    resolution_filter = (
        ""
        if refresh
        else "AND COALESCE(apply_url_resolution_strategy, '') = ''"
    )

    sql = f"""
        SELECT url, title, company, application_url, audit_label, audit_score, fit_score, site
          FROM jobs
         WHERE (
                lower(COALESCE(site, '')) = 'indeed'
                OR url LIKE '%indeed.com%'
                OR COALESCE(application_url, '') LIKE '%indeed.com%'
           )
           AND duplicate_of_url IS NULL
           AND COALESCE(liveness_status, '') != 'dead'
           AND applied_at IS NULL
           AND COALESCE(audit_label, '') IN ({tier_marks})
           {resolution_filter}
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

    candidates = []
    page_size = max(limit * 5, 50)
    offset = 0
    while len(candidates) < limit:
        rows = conn.execute(sql, (*wanted_tiers, page_size, offset)).fetchall()
        if not rows:
            break
        for row in rows:
            if (
                str(row["site"] or "").lower() != "indeed"
                and not is_indeed_url(row["url"])
                and not is_indeed_url(row["application_url"])
            ):
                continue
            candidates.append(Candidate(
                url=row["url"],
                title=row["title"],
                company=row["company"],
                application_url=row["application_url"],
                audit_label=row["audit_label"],
                audit_score=row["audit_score"],
                fit_score=row["fit_score"],
            ))
            if len(candidates) >= limit:
                break
        offset += len(rows)
    return candidates


def record_resolution(
    url: str,
    decision: PageDecision,
    *,
    dry_run: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    if dry_run:
        return
    if conn is None:
        conn = get_connection()

    now = datetime.now(timezone.utc).isoformat()
    error = decision.error
    if decision.status == "unresolved" and decision.unresolved_kind:
        detail = f"{decision.unresolved_kind}:{decision.next_action or ''}".rstrip(":")
        error = f"{detail}; {error}" if error else detail

    if decision.status == "resolved_offsite" and is_external_apply_url(decision.final_url):
        conn.execute(
            """
            UPDATE jobs
               SET application_url = ?,
                   apply_url_resolved_at = ?,
                   apply_url_resolution_strategy = ?,
                   apply_url_resolution_confidence = 1.0,
                   apply_url_resolution_source = ?,
                   apply_url_resolution_error = NULL,
                   apply_url_resolution_attempts = COALESCE(apply_url_resolution_attempts, 0) + 1,
                   apply_url_resolution_matched_url = NULL
             WHERE url = ?
            """,
            (
                decision.final_url,
                now,
                INDEED_RESOLUTION_STRATEGY,
                decision.status,
                url,
            ),
        )
    else:
        conn.execute(
            """
            UPDATE jobs
               SET apply_url_resolved_at = ?,
                   apply_url_resolution_strategy = ?,
                   apply_url_resolution_confidence = ?,
                   apply_url_resolution_source = ?,
                   apply_url_resolution_error = ?,
                   apply_url_resolution_attempts = COALESCE(apply_url_resolution_attempts, 0) + 1,
                   apply_url_resolution_matched_url = NULL
             WHERE url = ?
            """,
            (
                now,
                INDEED_RESOLUTION_STRATEGY,
                1.0 if decision.status == "hosted_apply" else None,
                decision.status,
                error,
                url,
            ),
        )

    if decision.status == "unavailable":
        conn.execute(
            """
            UPDATE jobs
               SET liveness_status = 'dead',
                   liveness_reason = 'indeed_resolver_unavailable',
                   last_verified_live = ?
             WHERE url = ?
            """,
            (now, url),
        )
    elif decision.status in {"hosted_apply", "resolved_offsite"}:
        conn.execute(
            """
            UPDATE jobs
               SET liveness_status = 'live',
                   liveness_reason = ?,
                   last_verified_live = ?
             WHERE url = ?
               AND COALESCE(liveness_status, '') != 'dead'
            """,
            (f"indeed_resolver_{decision.status}", now, url),
        )
    conn.commit()


def _summary_url(candidate: Candidate, decision: PageDecision) -> str:
    return decision.final_url or candidate.application_url or candidate.url


def run_resolver(
    options: IndeedResolverOptions,
    *,
    conn: sqlite3.Connection | None = None,
) -> IndeedResolverSummary:
    if conn is None:
        conn = get_connection()

    candidates = fetch_candidates(
        limit=options.limit,
        tiers=options.tiers,
        include_low=options.include_low,
        refresh=options.refresh,
        conn=conn,
    )

    counts: Counter[str] = Counter()
    unresolved_kinds: Counter[str] = Counter()
    sample_urls: list[str] = []
    for candidate in candidates:
        decision = classify_candidate(candidate)
        counts[decision.status] += 1
        if decision.status == "unresolved" and decision.unresolved_kind:
            unresolved_kinds[decision.unresolved_kind] += 1
        record_resolution(candidate.url, decision, dry_run=options.dry_run, conn=conn)
        if len(sample_urls) < 10:
            sample_urls.append(_summary_url(candidate, decision))

    return IndeedResolverSummary(
        considered=len(candidates),
        dry_run=options.dry_run,
        counts=dict(counts),
        unresolved_kinds=dict(unresolved_kinds),
        sample_urls=sample_urls,
    )


def _locator_for_control(page, control: ApplyControl):
    if control.selector:
        try:
            locator = page.locator(control.selector)
            if locator.count() == 1:
                return locator.first
        except Exception:
            pass
    control_text = (control.text or "Apply").strip() or "Apply"
    return page.get_by_text(control_text, exact=False).first


def _click_and_capture_external(
    page,
    control: ApplyControl,
    timeout_ms: int = 5000,
) -> PageDecision:
    try:
        locator = _locator_for_control(page, control)
        with page.expect_popup(timeout=timeout_ms) as popup_info:
            locator.click(timeout=timeout_ms)
        popup = popup_info.value
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
            if is_external_apply_url(popup.url):
                return PageDecision(
                    status="resolved_offsite",
                    final_url=popup.url,
                    control=control,
                )
            if source_platform_from_url(popup.url) is not None:
                return unresolved_decision(
                    "outbound_still_source_platform",
                    error=popup.url,
                    control=control,
                )
            return unresolved_decision(
                "malformed_outbound_url",
                error=popup.url,
                control=control,
            )
        finally:
            try:
                popup.close()
            except Exception:
                pass
    except Exception as exc:
        return unresolved_decision(
            "outbound_not_observed",
            error=str(exc)[:200],
            control=control,
        )
