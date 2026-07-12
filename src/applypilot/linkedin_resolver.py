"""LinkedIn external apply URL resolver.

This module resolves LinkedIn job links that point to external ATS application
destinations before normal apply processing. It is intentionally read-only with
respect to job submission and should never submit applications or drive Easy
Apply workflows.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import os
import random
import sqlite3
import time
from typing import Callable, Iterable, Sequence

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from applypilot.apply.chrome import BASE_CDP_PORT, cleanup_worker, launch_chrome
from applypilot.apply.lifecycle_fault import require_browser_cleanup
from applypilot.aggregator_resolver import (
    is_external_apply_url,
    next_action_for_unresolved_kind,
    source_platform_from_url,
)
from applypilot.database import get_connection

COMPLETED_STATUSES = {
    "resolved_offsite",
    "easy_apply",
    "login_required",
    "challenge_required",
    "unavailable",
    "unresolved",
}

STOP_UNRESOLVED_KINDS = {"auth_required", "checkpoint_or_captcha", "rate_limited"}

CHALLENGE_TEXTS = (
    "security check",
    "verify it's you",
    "verify it is you",
    "quick security check",
    "restricted your account",
    "verify your identity",
    "unusual activity",
    "captcha",
)

LOGIN_TEXTS = (
    "sign in to view",
    "sign in to continue",
    "join linkedin",
    "email or phone",
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
    company_hint: str | None = None


@dataclass(frozen=True)
class PageDecision:
    status: str
    stop_run: bool = False
    final_url: str | None = None
    error: str | None = None
    control: ApplyControl | None = None
    unresolved_kind: str | None = None
    next_action: str | None = None
    company_hint: str | None = None


@dataclass(frozen=True)
class ResolverOptions:
    limit: int = int(os.environ.get("APPLYPILOT_LINKEDIN_RESOLVE_LIMIT") or 200)
    tiers: tuple[str, ...] = ("priority", "recommended")
    include_low: bool = False
    refresh: bool = False
    dry_run: bool = False
    delay_min: float = float(os.environ.get("APPLYPILOT_LINKEDIN_RESOLVE_DELAY_MIN") or 8)
    delay_max: float = float(os.environ.get("APPLYPILOT_LINKEDIN_RESOLVE_DELAY_MAX") or 20)
    page_timeout_ms: int = int(os.environ.get("APPLYPILOT_LINKEDIN_RESOLVE_PAGE_TIMEOUT") or 45000)
    click_timeout_ms: int = int(os.environ.get("APPLYPILOT_LINKEDIN_RESOLVE_CLICK_TIMEOUT") or 20000)
    browser: str = os.environ.get("APPLYPILOT_LINKEDIN_RESOLVE_BROWSER") or "chrome"
    worker_id: int = int(os.environ.get("APPLYPILOT_LINKEDIN_RESOLVE_WORKER_ID") or 80)
    chunk_size: int = int(os.environ.get("APPLYPILOT_LINKEDIN_RESOLVE_CHUNK_SIZE") or 10)


@dataclass
class ResolverSummary:
    considered: int = 0
    dry_run: bool = False
    counts: dict[str, int] | None = None
    stopped_reason: str | None = None
    sample_urls: list[str] | None = None

    def __post_init__(self) -> None:
        if self.counts is None:
            self.counts = {}
        if self.sample_urls is None:
            self.sample_urls = []


def is_linkedin_url(url: str | None) -> bool:
    return source_platform_from_url(url) == "linkedin"


def unresolved_decision(
    kind: str,
    *,
    error: str | None = None,
    stop_run: bool = False,
    control: ApplyControl | None = None,
) -> PageDecision:
    return PageDecision(
        status="unresolved",
        stop_run=stop_run,
        error=error,
        control=control,
        unresolved_kind=kind,
        next_action=next_action_for_unresolved_kind(kind),
    )


def _snapshot_text_lower(snapshot: PageSnapshot) -> str:
    return (snapshot.text or "").lower()


_COMPANY_HINT_BAD_EXACT = {
    "about the job",
    "apply",
    "easy apply",
    "employment type",
    "full-time",
    "hybrid",
    "internship",
    "job function",
    "on-site",
    "part-time",
    "remote",
    "save",
    "seniority level",
    "share",
    "united states",
}

_COMPANY_HINT_BAD_TOKENS = (
    " applicants",
    " connections",
    " employees",
    " followers",
    " promoted",
    " reposted",
    " views",
    " weeks ago",
    " week ago",
    " days ago",
    " day ago",
    " hours ago",
    " hour ago",
    " minutes ago",
    " minute ago",
)

_ROLE_LINE_TOKENS = (
    "analyst",
    "associate",
    "chief of staff",
    "director",
    "engineer",
    "gtm",
    "head of",
    "lead",
    "manager",
    "operations",
    "principal",
    "product",
    "sales",
    "senior",
    "strategy",
)


def _clean_company_hint(value: str | None) -> str | None:
    text = " ".join(str(value or "").replace("\xa0", " ").split()).strip(" -|•")
    for separator in (" · ", " | ", " • "):
        if separator in text:
            text = text.split(separator, 1)[0].strip()
    if not text or len(text) > 120:
        return None
    low = f" {text.lower()} "
    if text.lower() in _COMPANY_HINT_BAD_EXACT:
        return None
    if any(token in low for token in _COMPANY_HINT_BAD_TOKENS):
        return None
    if "linkedin" == text.lower():
        return None
    return text


def _looks_like_role_line(value: str) -> bool:
    low = value.lower()
    return any(token in low for token in _ROLE_LINE_TOKENS)


def extract_company_hint(snapshot: PageSnapshot) -> str | None:
    """Best-effort LinkedIn company name extraction from the page top card."""
    direct = _clean_company_hint(snapshot.company_hint)
    if direct:
        return direct

    lines = [
        cleaned
        for raw in str(snapshot.text or "").splitlines()
        if (cleaned := _clean_company_hint(raw))
    ]
    if not lines:
        return None

    if len(lines) < 2 or not _looks_like_role_line(lines[0]):
        return None
    for line in lines[1:8]:
        if not _looks_like_role_line(line):
            return line
    return None


def classify_snapshot(snapshot: PageSnapshot) -> PageDecision:
    url = str(snapshot.url or "").lower()
    text = _snapshot_text_lower(snapshot)
    controls = tuple(snapshot.controls or ())
    company_hint = extract_company_hint(snapshot)

    if "/checkpoint/" in url or "/uas/" in url:
        return unresolved_decision(
            "checkpoint_or_captcha",
            stop_run=True,
            error="linkedin_checkpoint",
        )

    if "too many requests" in text or "rate limit" in text or "temporarily restricted" in text:
        return unresolved_decision(
            "rate_limited",
            stop_run=True,
            error="linkedin_rate_limited",
        )

    if any(token in text for token in CHALLENGE_TEXTS):
        return unresolved_decision(
            "checkpoint_or_captcha",
            stop_run=True,
            error="linkedin_challenge",
        )

    if "linkedin.com/login" in url or any(token in text for token in LOGIN_TEXTS):
        return unresolved_decision(
            "auth_required",
            stop_run=True,
            error="linkedin_login",
        )

    if any(token in text for token in UNAVAILABLE_TEXTS):
        return PageDecision(status="unavailable", company_hint=company_hint)

    easy_apply = next(
        (control for control in controls if "easy apply" in str(control.text or "").lower()),
        None,
    )
    if easy_apply is not None:
        return PageDecision(status="easy_apply", control=easy_apply, company_hint=company_hint)

    apply_control = next(
        (control for control in controls if "apply" in str(control.text or "").lower()),
        None,
    )
    if apply_control is None:
        return unresolved_decision("apply_button_missing", error="no_primary_apply_button")

    if is_external_apply_url(apply_control.href):
        return PageDecision(
            status="resolved_offsite",
            final_url=apply_control.href,
            control=apply_control,
            company_hint=company_hint,
        )

    return PageDecision(status="needs_click", control=apply_control, company_hint=company_hint)


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
    company_hint: str | None = None,
    error: str | None = None,
    unresolved_kind: str | None = None,
    next_action: str | None = None,
    refresh: bool = False,
    conn: sqlite3.Connection | None = None,
) -> None:
    if conn is None:
        conn = get_connection()

    if status == "unresolved":
        unresolved_kind = unresolved_kind or "dom_unreadable"
        next_action = next_action or next_action_for_unresolved_kind(unresolved_kind)
    else:
        unresolved_kind = None
        next_action = None

    now = datetime.now(timezone.utc).isoformat()
    clean_company_hint = _clean_company_hint(company_hint)
    existing = conn.execute(
        "SELECT application_url, company FROM jobs WHERE url = ?",
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
                   linkedin_unresolved_kind = ?,
                   linkedin_next_action = ?,
                   linkedin_resolved_at = ?,
                   linkedin_resolve_attempts = COALESCE(linkedin_resolve_attempts, 0) + 1
             WHERE url = ?
            """,
            (final_url, final_url, status, error, unresolved_kind, next_action, now, url),
        )
    else:
        conn.execute(
            """
            UPDATE jobs
               SET linkedin_resolve_final_url = ?,
                   linkedin_resolve_status = ?,
                   linkedin_resolve_error = ?,
                   linkedin_unresolved_kind = ?,
                   linkedin_next_action = ?,
                   linkedin_resolved_at = ?,
                   linkedin_resolve_attempts = COALESCE(linkedin_resolve_attempts, 0) + 1
             WHERE url = ?
            """,
            (final_url, status, error, unresolved_kind, next_action, now, url),
        )
    if clean_company_hint:
        conn.execute(
            """
            UPDATE jobs
               SET company = ?
             WHERE url = ?
               AND TRIM(COALESCE(company, '')) = ''
            """,
            (clean_company_hint, url),
        )
    if status == "unavailable":
        conn.execute(
            """
            UPDATE jobs
               SET liveness_status = 'dead',
                   liveness_reason = 'linkedin_resolver_unavailable',
                   last_verified_live = ?
             WHERE url = ?
            """,
            (now, url),
        )
    elif status in {"easy_apply", "resolved_offsite"}:
        conn.execute(
            """
            UPDATE jobs
               SET liveness_status = 'live',
                   liveness_reason = ?,
                   last_verified_live = ?
             WHERE url = ?
               AND COALESCE(liveness_status, '') != 'dead'
            """,
            (f"linkedin_resolver_{status}", now, url),
        )
    conn.commit()


def should_stop_run(status: str, unresolved_kind: str | None = None) -> bool:
    return status == "unresolved" and unresolved_kind in STOP_UNRESOLVED_KINDS


def _summary_url(candidate: Candidate, decision: PageDecision) -> str:
    if decision.status == "resolved_offsite" and decision.final_url:
        return decision.final_url
    return candidate.url


def _run_candidates_for_test(
    candidates: Sequence[Candidate],
    options: ResolverOptions,
    resolver: Callable[[Candidate, ResolverOptions], PageDecision],
) -> ResolverSummary:
    return _run_candidates(candidates, options, resolver)


def _run_candidates(
    candidates: Sequence[Candidate],
    options: ResolverOptions,
    resolver: Callable[[Candidate, ResolverOptions], PageDecision],
) -> ResolverSummary:
    counts: Counter[str] = Counter()
    sample_urls: list[str] = []
    stopped_reason: str | None = None

    for candidate in candidates:
        decision = resolver(candidate, options)
        record_resolution(
            candidate.url,
            status=decision.status,
            final_url=decision.final_url,
            company_hint=decision.company_hint,
            error=decision.error,
            unresolved_kind=decision.unresolved_kind,
            next_action=decision.next_action,
            refresh=options.refresh,
        )
        counts[decision.status] += 1
        if len(sample_urls) < 10:
            sample_urls.append(_summary_url(candidate, decision))
        if decision.stop_run or should_stop_run(decision.status, decision.unresolved_kind):
            stopped_reason = decision.unresolved_kind or decision.status
            break
        _sleep_between(options)

    return ResolverSummary(
        considered=sum(counts.values()),
        dry_run=False,
        counts=dict(counts),
        stopped_reason=stopped_reason,
        sample_urls=sample_urls,
    )


def run_resolver(options: ResolverOptions) -> ResolverSummary:
    candidates = fetch_candidates(
        limit=options.limit,
        tiers=options.tiers,
        include_low=options.include_low,
        refresh=options.refresh,
    )
    if options.dry_run:
        return ResolverSummary(
            considered=len(candidates),
            dry_run=True,
            counts={},
            sample_urls=[candidate.url for candidate in candidates[:10]],
        )
    if not candidates:
        return ResolverSummary(considered=0, dry_run=False, counts={}, sample_urls=[])
    return _run_live_browser(candidates, options)


def _sleep_between(options: ResolverOptions) -> None:
    delay_hi = max(options.delay_min, options.delay_max)
    if delay_hi <= 0:
        return
    delay = random.uniform(max(0.0, options.delay_min), delay_hi)
    time.sleep(delay)


def _snapshot_page(page) -> PageSnapshot:
    text = ""
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        text = ""

    company_hint = None
    try:
        company_hint = _clean_company_hint(
            page.evaluate(
                """
                () => {
                  const clean = (value) => (value || '').toString().replace(/\\s+/g, ' ').trim();
                  const fromOrg = (org) => {
                    if (!org) return '';
                    if (typeof org === 'string') return clean(org);
                    return clean(org.name);
                  };
                  const visit = (node) => {
                    if (!node || typeof node !== 'object') return '';
                    if (Array.isArray(node)) {
                      for (const item of node) {
                        const found = visit(item);
                        if (found) return found;
                      }
                      return '';
                    }
                    const org = fromOrg(node.hiringOrganization);
                    if (org) return org;
                    for (const value of Object.values(node)) {
                      const found = visit(value);
                      if (found) return found;
                    }
                    return '';
                  };
                  for (const script of Array.from(document.querySelectorAll('script[type="application/ld+json"]'))) {
                    try {
                      const found = visit(JSON.parse(script.textContent || ''));
                      if (found) return found;
                    } catch (_) {}
                  }
                  const selectors = [
                    '.jobs-unified-top-card__company-name a',
                    '.jobs-unified-top-card__company-name',
                    '.job-details-jobs-unified-top-card__company-name a',
                    '.job-details-jobs-unified-top-card__company-name',
                    '.topcard__org-name-link',
                    '.topcard__flavor--black-link',
                    'a[data-control-name="company_link"]'
                  ];
                  for (const selector of selectors) {
                    const value = clean(document.querySelector(selector)?.innerText || document.querySelector(selector)?.textContent);
                    if (value) return value;
                  }
                  return '';
                }
                """
            )
        )
    except Exception:
        company_hint = None

    controls_raw = page.evaluate(
        """
        () => {
          const cssPath = (el) => {
            if (el.id) {
              return `#${CSS.escape(el.id)}`;
            }
            const parts = [];
            let node = el;
            while (node && node.nodeType === Node.ELEMENT_NODE) {
              const tag = node.tagName.toLowerCase();
              const parent = node.parentElement;
              if (!parent) {
                parts.unshift(tag);
                break;
              }
              const sameTag = Array.from(parent.children).filter((child) => child.tagName === node.tagName);
              const index = sameTag.indexOf(node) + 1;
              parts.unshift(`${tag}:nth-of-type(${index})`);
              node = parent;
            }
            return parts.join(" > ");
          };
          return Array.from(document.querySelectorAll('a,button')).map((el) => {
            const text = (el.innerText || el.getAttribute('aria-label') || el.textContent || '').trim();
            const href = el.href || el.getAttribute('href') || '';
            return {text, href, selector: cssPath(el)};
          }).filter((item) => {
            const haystack = `${item.text} ${item.href}`.toLowerCase();
            return haystack.includes('apply');
          });
        }
        """
    )
    controls = tuple(
        ApplyControl(
            text=str(item.get("text") or ""),
            href=str(item.get("href") or "") or None,
            selector=str(item.get("selector") or ""),
        )
        for item in controls_raw
        if isinstance(item, dict)
    )
    return PageSnapshot(url=page.url, text=text, controls=controls, company_hint=company_hint)


def _is_browser_closed_error(message: str) -> bool:
    text = message.lower()
    return any(
        token in text
        for token in (
            "target page, context or browser has been closed",
            "context or browser has been closed",
            "browser has been closed",
            "context closed",
            "browser closed",
        )
    )


def _with_company_hint(decision: PageDecision, company_hint: str | None) -> PageDecision:
    clean_hint = _clean_company_hint(company_hint)
    if not clean_hint or decision.company_hint:
        return decision
    return replace(decision, company_hint=clean_hint)


def _resolve_one_with_context(context, candidate: Candidate, options: ResolverOptions) -> PageDecision:
    page = None
    try:
        page = context.new_page()
        page.goto(candidate.url, wait_until="domcontentloaded", timeout=options.page_timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception:
            pass

        snapshot = _snapshot_page(page)
        decision = classify_snapshot(snapshot)
        if decision.status == "resolved_offsite" and decision.final_url:
            return decision
        if decision.status == "needs_click" and decision.control:
            clicked = _click_and_capture_external(page, decision.control, options)
            return _with_company_hint(clicked, decision.company_hint)
        return decision
    except PlaywrightTimeoutError:
        return unresolved_decision("page_unreachable", error="page_timeout")
    except Exception as exc:
        message = str(exc)
        if _is_browser_closed_error(message):
            return unresolved_decision("page_unreachable", error="browser_context_closed")
        return unresolved_decision("dom_unreadable", error=str(exc)[:200])
    finally:
        if page is not None:
            try:
                page.close()
            except Exception:
                pass


def _locator_for_control(page, control: ApplyControl):
    def _matches(locator) -> bool:
        expected_text = str(control.text or "").strip().lower()
        expected_href = str(control.href or "").strip()
        if not expected_text and not expected_href:
            return True
        actual_text = ""
        actual_href = ""
        try:
            actual_text = str(locator.inner_text(timeout=1000) or "").strip().lower()
        except Exception:
            pass
        try:
            actual_href = str(locator.get_attribute("href", timeout=1000) or "").strip()
        except Exception:
            pass
        text_matches = bool(expected_text and expected_text in actual_text)
        href_matches = bool(expected_href and actual_href and actual_href == expected_href)
        return text_matches or href_matches

    if control.selector:
        try:
            locator = page.locator(control.selector)
            if locator.count() == 1:
                candidate = locator.first
                if _matches(candidate):
                    return candidate
        except Exception:
            pass
    control_text = (control.text or "Apply").strip() or "Apply"
    text_locator = page.get_by_text(control_text, exact=False)
    if text_locator.count() == 1:
        return text_locator.first
    exact_locator = page.get_by_text(control_text, exact=True)
    if exact_locator.count() == 1:
        return exact_locator.first
    raise ValueError(f"ambiguous_apply_control:{control_text}")


def _same_tab_decision_after_click(
    page,
    control: ApplyControl,
    *,
    before_click_url: str | None = None,
) -> PageDecision:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=3000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=5000)
    except Exception:
        pass
    if is_external_apply_url(page.url):
        return PageDecision(status="resolved_offsite", final_url=page.url, control=control)
    if is_linkedin_url(page.url) and page.url != before_click_url:
        return unresolved_decision(
            "outbound_still_source_platform",
            error=f"same_tab_stayed_on_linkedin:{page.url}",
            control=control,
        )
    if page.url == before_click_url:
        return unresolved_decision(
            "outbound_not_observed",
            error=f"same_tab_no_navigation:{page.url}",
            control=control,
        )

    decision = classify_snapshot(_snapshot_page(page))
    if decision.status in {"unresolved", "easy_apply", "unavailable"}:
        return decision
    return unresolved_decision(
        "outbound_still_source_platform",
        error=f"same_tab_stayed_on_linkedin:{page.url}",
        control=control,
    )


def _click_and_capture_external(page, control: ApplyControl, options: ResolverOptions) -> PageDecision:
    before_click_url = getattr(page, "url", None)
    try:
        locator = _locator_for_control(page, control)
        with page.expect_popup(timeout=options.click_timeout_ms) as popup_info:
            locator.click(timeout=options.click_timeout_ms)
        popup = popup_info.value
        try:
            popup.wait_for_load_state("domcontentloaded", timeout=options.click_timeout_ms)
            try:
                popup.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            if is_external_apply_url(popup.url):
                return PageDecision(status="resolved_offsite", final_url=popup.url, control=control)
            if is_linkedin_url(popup.url):
                return unresolved_decision(
                    "outbound_still_source_platform",
                    error=f"popup_stayed_on_linkedin:{popup.url}",
                    control=control,
                )
            popup_decision = classify_snapshot(_snapshot_page(popup))
            if popup_decision.status in {"unresolved", "easy_apply", "unavailable"}:
                return popup_decision
            return unresolved_decision(
                "outbound_still_source_platform",
                error=f"popup_stayed_on_linkedin:{popup.url}",
                control=control,
            )
        finally:
            try:
                popup.close()
            except Exception:
                pass
    except PlaywrightTimeoutError:
        return _same_tab_decision_after_click(page, control, before_click_url=before_click_url)
    except Exception as exc:
        message = str(exc)
        if _is_browser_closed_error(message):
            return unresolved_decision("page_unreachable", error="browser_context_closed", control=control)
        return unresolved_decision("dom_unreadable", error=message[:200], control=control)


def _merge_summaries(summaries: Sequence[ResolverSummary]) -> ResolverSummary:
    counts: Counter[str] = Counter()
    sample_urls: list[str] = []
    considered = 0
    stopped_reason = None
    for summary in summaries:
        considered += summary.considered
        counts.update(summary.counts or {})
        for url in summary.sample_urls or []:
            if len(sample_urls) < 10:
                sample_urls.append(url)
        if summary.stopped_reason:
            stopped_reason = summary.stopped_reason
            break
    return ResolverSummary(
        considered=considered,
        dry_run=False,
        counts=dict(counts),
        stopped_reason=stopped_reason,
        sample_urls=sample_urls,
    )


def _run_live_browser(candidates: Sequence[Candidate], options: ResolverOptions) -> ResolverSummary:
    chunk_size = options.chunk_size if options.chunk_size > 0 else len(candidates)
    summaries: list[ResolverSummary] = []
    for start in range(0, len(candidates), chunk_size):
        chunk = candidates[start:start + chunk_size]
        summary = _run_live_browser_batch(chunk, options)
        summaries.append(summary)
        if summary.stopped_reason:
            break
    return _merge_summaries(summaries)


def _run_live_browser_batch(candidates: Sequence[Candidate], options: ResolverOptions) -> ResolverSummary:
    port = BASE_CDP_PORT + options.worker_id
    proc = launch_chrome(
        options.worker_id,
        port=port,
        headless=False,
        browser=options.browser,
    )
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                return _run_candidates(
                    candidates,
                    options,
                    lambda candidate, opts: _resolve_one_with_context(context, candidate, opts),
                )
            finally:
                browser.close()
    finally:
        require_browser_cleanup(cleanup_worker, options.worker_id, proc)
