"""LinkedIn external apply URL resolver.

This module resolves LinkedIn job links that point to external ATS application
destinations before normal apply processing. It is intentionally read-only with
respect to job submission and should never submit applications or drive Easy
Apply workflows.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import os
import random
import sqlite3
import time
from typing import Callable, Iterable, Sequence
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from applypilot.apply.chrome import BASE_CDP_PORT, cleanup_worker, launch_chrome
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


@dataclass(frozen=True)
class PageDecision:
    status: str
    stop_run: bool = False
    final_url: str | None = None
    error: str | None = None
    control: ApplyControl | None = None


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
    url = str(snapshot.url or "").lower()
    text = _snapshot_text_lower(snapshot)
    controls = tuple(snapshot.controls or ())

    if "/checkpoint/" in url or "/uas/" in url:
        return PageDecision(status="challenge_required", stop_run=True, error="linkedin_checkpoint")

    if any(token in text for token in CHALLENGE_TEXTS):
        return PageDecision(status="challenge_required", stop_run=True, error="linkedin_challenge")

    if "linkedin.com/login" in url or any(token in text for token in LOGIN_TEXTS):
        return PageDecision(status="login_required", stop_run=True, error="linkedin_login")

    if any(token in text for token in UNAVAILABLE_TEXTS):
        return PageDecision(status="unavailable")

    easy_apply = next(
        (control for control in controls if "easy apply" in str(control.text or "").lower()),
        None,
    )
    if easy_apply is not None:
        return PageDecision(status="easy_apply", control=easy_apply)

    apply_control = next(
        (control for control in controls if "apply" in str(control.text or "").lower()),
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
            error=decision.error,
            refresh=options.refresh,
        )
        counts[decision.status] += 1
        if len(sample_urls) < 10:
            sample_urls.append(_summary_url(candidate, decision))
        if decision.stop_run or should_stop_run(decision.status):
            stopped_reason = decision.status
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
    return PageSnapshot(url=page.url, text=text, controls=controls)


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
            return _click_and_capture_external(page, decision.control, options)
        return decision
    except PlaywrightTimeoutError:
        return PageDecision(status="timeout", error="page_timeout")
    except Exception as exc:
        message = str(exc)
        if "context or browser has been closed" in message or "target page" in message.lower():
            return PageDecision(status="browser_error", error="browser_context_closed")
        return PageDecision(status="error", error=str(exc)[:200])
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


def _same_tab_decision_after_click(page, control: ApplyControl) -> PageDecision:
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

    decision = classify_snapshot(_snapshot_page(page))
    if decision.status in {"login_required", "challenge_required", "easy_apply", "unavailable"}:
        return decision
    return PageDecision(status="no_apply_button", error=f"same_tab_stayed_on_linkedin:{page.url}", control=control)


def _click_and_capture_external(page, control: ApplyControl, options: ResolverOptions) -> PageDecision:
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
            popup_decision = classify_snapshot(_snapshot_page(popup))
            if popup_decision.status in {"login_required", "challenge_required", "easy_apply", "unavailable"}:
                return popup_decision
            return PageDecision(status="no_apply_button", error=f"popup_stayed_on_linkedin:{popup.url}", control=control)
        finally:
            try:
                popup.close()
            except Exception:
                pass
    except PlaywrightTimeoutError:
        return _same_tab_decision_after_click(page, control)
    except Exception as exc:
        return PageDecision(status="error", error=str(exc)[:200], control=control)


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
        cleanup_worker(options.worker_id, proc)
