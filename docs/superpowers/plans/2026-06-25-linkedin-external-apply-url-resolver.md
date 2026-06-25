# LinkedIn External Apply URL Resolver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a safe read-only LinkedIn resolver that backfills external ATS URLs into `jobs.application_url` so ApplyPilot can move those jobs from the LinkedIn-paced lane into the faster offsite lane.

**Architecture:** Add one focused resolver module that owns DB candidate selection, URL/page classification, browser resolution, persistence, and summary reporting. Keep live LinkedIn/browser automation thin; unit-test the pure DB and classification pieces heavily, then expose the workflow through one Typer CLI command.

**Tech Stack:** Python 3.11, SQLite via `applypilot.database`, Typer/Rich CLI, Playwright sync API over Chrome DevTools Protocol, existing ApplyPilot Chrome profile cloning in `src/applypilot/apply/chrome.py`, pytest.

---

## Scope Check

This plan covers one subsystem: resolving external apply URLs for existing LinkedIn-sourced jobs. It does not automate LinkedIn Easy Apply, alter scoring/tailoring, import raw cookies, or bypass LinkedIn verification systems.

## File Structure

- Create `src/applypilot/linkedin_resolver.py`
  - Responsibility: resolver options/results dataclasses, URL classification, page snapshot classification, DB selection/persistence, live browser orchestration, and summary formatting data.
- Modify `src/applypilot/database.py`
  - Responsibility: add resolver metadata columns to `_ALL_COLUMNS` so both new and existing DBs migrate through `init_db`.
- Modify `src/applypilot/cli.py`
  - Responsibility: expose `applypilot linkedin-resolve-apply-urls` and print a concise resolver summary.
- Modify `tests/test_linkedin_resolver.py`
  - Responsibility: pure unit tests for schema columns, URL classification, DB selection, persistence, page-state classification, dry-run behavior, and stop-status behavior.
- Modify `tests/test_cli_linkedin_resolver.py`
  - Responsibility: CLI option parsing and dry-run wiring without launching a browser.

---

### Task 1: Schema Columns

**Files:**
- Modify: `src/applypilot/database.py`
- Create: `tests/test_linkedin_resolver.py`

- [ ] **Step 1: Write the failing schema test**

Create `tests/test_linkedin_resolver.py` with:

```python
from __future__ import annotations

from datetime import datetime, timezone

from applypilot import database


def test_schema_adds_linkedin_resolver_columns(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}

    assert {
        "linkedin_resolved_at",
        "linkedin_resolve_status",
        "linkedin_resolve_error",
        "linkedin_resolve_attempts",
        "linkedin_resolve_final_url",
    }.issubset(columns)
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py::test_schema_adds_linkedin_resolver_columns -q
```

Expected: FAIL with an assertion showing missing resolver columns.

- [ ] **Step 3: Add resolver columns to the schema registry**

In `src/applypilot/database.py`, add these entries to `_ALL_COLUMNS` immediately after the enrichment columns:

```python
    # LinkedIn external apply URL resolver
    "linkedin_resolved_at": "TEXT",
    "linkedin_resolve_status": "TEXT",
    "linkedin_resolve_error": "TEXT",
    "linkedin_resolve_attempts": "INTEGER DEFAULT 0",
    "linkedin_resolve_final_url": "TEXT",
```

Do not add a separate migration function; `ensure_columns()` already handles additive forward migrations.

- [ ] **Step 4: Run test to verify it passes**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py::test_schema_adds_linkedin_resolver_columns -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```powershell
git add src/applypilot/database.py tests/test_linkedin_resolver.py
git commit -m "Add LinkedIn resolver schema columns"
```

---

### Task 2: URL Classification, Candidate Selection, And Persistence

**Files:**
- Create: `src/applypilot/linkedin_resolver.py`
- Modify: `tests/test_linkedin_resolver.py`

- [ ] **Step 1: Add failing tests for URL classification**

Append to `tests/test_linkedin_resolver.py`:

```python
from applypilot import linkedin_resolver


def test_url_classification_distinguishes_linkedin_and_offsite():
    assert linkedin_resolver.is_linkedin_url("https://www.linkedin.com/jobs/view/123") is True
    assert linkedin_resolver.is_linkedin_url("https://linkedin.com/jobs/view/123") is True
    assert linkedin_resolver.is_linkedin_url("https://jobs.lever.co/acme/123") is False
    assert linkedin_resolver.is_external_apply_url("https://jobs.lever.co/acme/123") is True
    assert linkedin_resolver.is_external_apply_url("https://www.linkedin.com/jobs/view/123") is False
    assert linkedin_resolver.is_external_apply_url("") is False
    assert linkedin_resolver.is_external_apply_url(None) is False
```

- [ ] **Step 2: Add failing tests for selection and persistence**

Append to `tests/test_linkedin_resolver.py`:

```python
def _insert_job(
    conn,
    *,
    url: str,
    title: str = "Chief of Staff",
    site: str = "linkedin",
    application_url: str | None = None,
    audit_label: str | None = "recommended",
    audit_score: float | None = 8.5,
    fit_score: int | None = 8,
    duplicate_of_url: str | None = None,
    liveness_status: str | None = None,
    applied_at: str | None = None,
    linkedin_resolve_status: str | None = None,
    discovered_at: str = "2026-06-20T00:00:00+00:00",
):
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, site, company, application_url, audit_label, audit_score,
            fit_score, duplicate_of_url, liveness_status, applied_at,
            linkedin_resolve_status, discovered_at
        )
        VALUES (?, ?, ?, 'Acme', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            url,
            title,
            site,
            application_url,
            audit_label,
            audit_score,
            fit_score,
            duplicate_of_url,
            liveness_status,
            applied_at,
            linkedin_resolve_status,
            discovered_at,
        ),
    )
    conn.commit()


def test_fetch_candidates_prioritizes_recommended_unresolved_linkedin_rows(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)

    _insert_job(conn, url="https://www.linkedin.com/jobs/view/low", audit_label="low", audit_score=9.9)
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/dupe", duplicate_of_url="https://x")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/dead", liveness_status="dead")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/applied", applied_at="2026-06-20T01:00:00+00:00")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/offsite", application_url="https://jobs.lever.co/acme/1")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/easy", linkedin_resolve_status="easy_apply")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/priority", audit_label="priority", audit_score=7.0)
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/recommended", audit_label="recommended", audit_score=9.0)

    rows = linkedin_resolver.fetch_candidates(limit=10, tiers=("priority", "recommended"))

    assert [row.url for row in rows] == [
        "https://www.linkedin.com/jobs/view/priority",
        "https://www.linkedin.com/jobs/view/recommended",
    ]


def test_fetch_candidates_can_include_low_and_refresh_completed_statuses(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/low", audit_label="low")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/easy", linkedin_resolve_status="easy_apply")

    rows = linkedin_resolver.fetch_candidates(
        limit=10,
        tiers=("priority", "recommended"),
        include_low=True,
        refresh=True,
    )

    assert {row.url for row in rows} == {
        "https://www.linkedin.com/jobs/view/low",
        "https://www.linkedin.com/jobs/view/easy",
    }


def test_record_resolution_sets_offsite_application_url_and_attempt_metadata(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/resolve")

    linkedin_resolver.record_resolution(
        "https://www.linkedin.com/jobs/view/resolve",
        status="resolved_offsite",
        final_url="https://jobs.ashbyhq.com/acme/resolve",
    )

    row = conn.execute(
        """
        SELECT application_url, linkedin_resolve_status, linkedin_resolve_attempts,
               linkedin_resolve_final_url, linkedin_resolve_error, linkedin_resolved_at
        FROM jobs WHERE url = ?
        """,
        ("https://www.linkedin.com/jobs/view/resolve",),
    ).fetchone()
    assert row["application_url"] == "https://jobs.ashbyhq.com/acme/resolve"
    assert row["linkedin_resolve_status"] == "resolved_offsite"
    assert row["linkedin_resolve_attempts"] == 1
    assert row["linkedin_resolve_final_url"] == "https://jobs.ashbyhq.com/acme/resolve"
    assert row["linkedin_resolve_error"] is None
    assert row["linkedin_resolved_at"]


def test_record_resolution_does_not_overwrite_existing_offsite_without_refresh(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)
    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/already",
        application_url="https://boards.greenhouse.io/acme/jobs/old",
    )

    linkedin_resolver.record_resolution(
        "https://www.linkedin.com/jobs/view/already",
        status="resolved_offsite",
        final_url="https://jobs.lever.co/acme/new",
        refresh=False,
    )

    app_url = conn.execute(
        "SELECT application_url FROM jobs WHERE url = ?",
        ("https://www.linkedin.com/jobs/view/already",),
    ).fetchone()[0]
    assert app_url == "https://boards.greenhouse.io/acme/jobs/old"
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py -q
```

Expected: FAIL with `ImportError` or missing `linkedin_resolver` functions.

- [ ] **Step 4: Add the pure resolver implementation**

Create `src/applypilot/linkedin_resolver.py` with this starting implementation:

```python
"""LinkedIn external apply URL resolver.

This module resolves LinkedIn-sourced jobs that redirect to external ATS pages.
It does not submit applications and it does not automate LinkedIn Easy Apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
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
        return tuple(dict.fromkeys((*base, "review", "low")))
    return base or ("priority", "recommended")


def fetch_candidates(
    *,
    limit: int,
    tiers: Iterable[str] | None = ("priority", "recommended"),
    include_low: bool = False,
    refresh: bool = False,
    conn=None,
) -> list[Candidate]:
    if conn is None:
        conn = get_connection()
    wanted_tiers = _normalize_tiers(tiers, include_low)
    tier_marks = ",".join("?" for _ in wanted_tiers)
    refresh_clause = "" if refresh else (
        "AND COALESCE(linkedin_resolve_status, '') NOT IN "
        "('resolved_offsite', 'easy_apply', 'login_required', 'challenge_required', 'unavailable')"
    )
    rows = conn.execute(
        f"""
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
           {refresh_clause}
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
        """,
        (*wanted_tiers, limit),
    ).fetchall()
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
    conn=None,
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```powershell
git add src/applypilot/linkedin_resolver.py tests/test_linkedin_resolver.py
git commit -m "Add LinkedIn resolver DB selection"
```

---

### Task 3: Page Snapshot Classification

**Files:**
- Modify: `src/applypilot/linkedin_resolver.py`
- Modify: `tests/test_linkedin_resolver.py`

- [ ] **Step 1: Add failing tests for page-state classification**

Append to `tests/test_linkedin_resolver.py`:

```python
def test_classify_snapshot_stops_on_linkedin_challenge():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/checkpoint/challenge",
        text="Quick security check. Verify it's you.",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "challenge_required"
    assert decision.stop_run is True
    assert decision.final_url is None


def test_classify_snapshot_stops_on_login_wall():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/login",
        text="Sign in to view this job",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "login_required"
    assert decision.stop_run is True


def test_classify_snapshot_detects_unavailable_job():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/404",
        text="This job is no longer accepting applications",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "unavailable"
    assert decision.stop_run is False


def test_classify_snapshot_detects_easy_apply():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Chief of Staff",
        controls=(linkedin_resolver.ApplyControl(text="Easy Apply", href=None, selector="button"),),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "easy_apply"
    assert decision.stop_run is False


def test_classify_snapshot_detects_external_apply_href():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Chief of Staff",
        controls=(
            linkedin_resolver.ApplyControl(
                text="Apply",
                href="https://jobs.lever.co/acme/123",
                selector="a[href='https://jobs.lever.co/acme/123']",
            ),
        ),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "resolved_offsite"
    assert decision.final_url == "https://jobs.lever.co/acme/123"
    assert decision.control is not None


def test_classify_snapshot_keeps_generic_apply_control_for_click():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Chief of Staff",
        controls=(
            linkedin_resolver.ApplyControl(
                text="Apply",
                href="https://www.linkedin.com/jobs/view/123?trk=public_jobs_apply-link-offsite",
                selector="button",
            ),
        ),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "needs_click"
    assert decision.control is not None


def test_classify_snapshot_reports_missing_apply_control():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Chief of Staff",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "no_apply_button"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py -q
```

Expected: FAIL with missing `PageSnapshot`, `ApplyControl`, or `classify_snapshot`.

- [ ] **Step 3: Implement page-state classification**

Add these dataclasses and functions to `src/applypilot/linkedin_resolver.py` after `Candidate`:

```python
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
```

Add these constants after `STOP_STATUSES`:

```python
CHALLENGE_TEXTS = (
    "unusual activity",
    "verify it's you",
    "verify it is you",
    "quick security check",
    "restricted your account",
    "captcha",
)

LOGIN_TEXTS = (
    "sign in to view",
    "sign in to continue",
    "join linkedin",
    "email or phone",
)

UNAVAILABLE_TEXTS = (
    "no longer accepting applications",
    "this job is no longer available",
    "this job has expired",
    "we couldn't find a match",
)
```

Add this function near the pure URL helpers:

```python
def classify_snapshot(snapshot: PageSnapshot) -> PageDecision:
    url_lower = (snapshot.url or "").lower()
    text_lower = (snapshot.text or "").lower()
    if "/checkpoint/" in url_lower or "/uas/" in url_lower:
        return PageDecision(status="challenge_required", stop_run=True, error="linkedin_checkpoint")
    if any(token in text_lower for token in CHALLENGE_TEXTS):
        return PageDecision(status="challenge_required", stop_run=True, error="linkedin_challenge")
    if "linkedin.com/login" in url_lower or any(token in text_lower for token in LOGIN_TEXTS):
        return PageDecision(status="login_required", stop_run=True, error="linkedin_login")
    if any(token in text_lower for token in UNAVAILABLE_TEXTS):
        return PageDecision(status="unavailable")

    for control in snapshot.controls:
        label = (control.text or "").strip().lower()
        if "easy apply" in label:
            return PageDecision(status="easy_apply", control=control)

    for control in snapshot.controls:
        label = (control.text or "").strip().lower()
        if "apply" in label and is_external_apply_url(control.href):
            return PageDecision(status="resolved_offsite", final_url=control.href, control=control)

    for control in snapshot.controls:
        label = (control.text or "").strip().lower()
        if "apply" in label:
            return PageDecision(status="needs_click", control=control)

    return PageDecision(status="no_apply_button")
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```powershell
git add src/applypilot/linkedin_resolver.py tests/test_linkedin_resolver.py
git commit -m "Classify LinkedIn resolver page states"
```

---

### Task 4: Dry-Run And Live Browser Orchestration

**Files:**
- Modify: `src/applypilot/linkedin_resolver.py`
- Modify: `tests/test_linkedin_resolver.py`

- [ ] **Step 1: Add failing tests for dry-run and stop behavior**

Append to `tests/test_linkedin_resolver.py`:

```python
def test_run_resolver_dry_run_does_not_record_status_or_launch_browser(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/dry", audit_label="recommended")

    def fail_launch(*args, **kwargs):
        raise AssertionError("dry run must not launch browser")

    monkeypatch.setattr(linkedin_resolver, "launch_chrome", fail_launch)

    summary = linkedin_resolver.run_resolver(
        linkedin_resolver.ResolverOptions(limit=10, dry_run=True)
    )

    assert summary.considered == 1
    assert summary.dry_run is True
    assert summary.counts == {}
    assert conn.execute(
        "SELECT linkedin_resolve_status FROM jobs WHERE url = ?",
        ("https://www.linkedin.com/jobs/view/dry",),
    ).fetchone()[0] is None


def test_run_resolver_stops_after_login_required_result(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/first", audit_label="recommended")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/second", audit_label="recommended")

    calls = []

    def fake_resolve(candidate, options):
        calls.append(candidate.url)
        return linkedin_resolver.PageDecision(
            status="login_required",
            stop_run=True,
            error="linkedin_login",
        )

    monkeypatch.setattr(linkedin_resolver, "_run_live_browser", lambda candidates, options: linkedin_resolver._run_candidates_for_test(candidates, options, fake_resolve))

    summary = linkedin_resolver.run_resolver(linkedin_resolver.ResolverOptions(limit=10))

    assert calls == ["https://www.linkedin.com/jobs/view/first"]
    assert summary.stopped_reason == "login_required"
    row = conn.execute(
        "SELECT linkedin_resolve_status, linkedin_resolve_error FROM jobs WHERE url = ?",
        ("https://www.linkedin.com/jobs/view/first",),
    ).fetchone()
    assert row["linkedin_resolve_status"] == "login_required"
    assert row["linkedin_resolve_error"] == "linkedin_login"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py -q
```

Expected: FAIL with missing `ResolverOptions`, `run_resolver`, and orchestration helpers.

- [ ] **Step 3: Implement resolver options, summary, and orchestration**

Add imports at the top of `src/applypilot/linkedin_resolver.py`:

```python
import os
import random
import time
from collections import Counter

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

from applypilot.apply.chrome import BASE_CDP_PORT, cleanup_worker, launch_chrome
```

Add dataclasses after `PageDecision`:

```python
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
```

Add this test helper and main runner:

```python
def _run_candidates_for_test(candidates, options: ResolverOptions, resolver) -> ResolverSummary:
    counts: Counter[str] = Counter()
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
        if decision.stop_run or should_stop_run(decision.status):
            stopped_reason = decision.status
            break
        _sleep_between(options)
    return ResolverSummary(
        considered=len(candidates),
        dry_run=False,
        counts=dict(counts),
        stopped_reason=stopped_reason,
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
        return ResolverSummary(considered=0, dry_run=False, counts={})
    return _run_live_browser(candidates, options)


def _sleep_between(options: ResolverOptions) -> None:
    delay_hi = max(options.delay_min, options.delay_max)
    if delay_hi <= 0:
        return
    delay = random.uniform(max(0.0, options.delay_min), delay_hi)
    time.sleep(delay)
```

Add page snapshot and live browser functions:

```python
def _snapshot_page(page) -> PageSnapshot:
    text = ""
    try:
        text = page.locator("body").inner_text(timeout=3000)
    except Exception:
        text = ""
    controls_raw = page.evaluate(
        """
        () => Array.from(document.querySelectorAll('a,button')).map((el, index) => {
            const text = (el.innerText || el.getAttribute('aria-label') || el.textContent || '').trim();
            const href = el.href || el.getAttribute('href') || '';
            const tag = el.tagName.toLowerCase();
            return {text, href, selector: `${tag}:nth-of-type(${index + 1})`};
        }).filter(item => item.text.toLowerCase().includes('apply') || (item.href || '').toLowerCase().includes('apply'));
        """
    )
    controls = tuple(
        ApplyControl(
            text=str(item.get("text") or ""),
            href=str(item.get("href") or "") or None,
            selector=str(item.get("selector") or ""),
        )
        for item in controls_raw
    )
    return PageSnapshot(url=page.url, text=text, controls=controls)


def _resolve_one_with_context(context, candidate: Candidate, options: ResolverOptions) -> PageDecision:
    page = context.new_page()
    try:
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
        return PageDecision(status="error", error=str(exc)[:200])
    finally:
        try:
            page.close()
        except Exception:
            pass


def _click_and_capture_external(page, control: ApplyControl, options: ResolverOptions) -> PageDecision:
    control_text = (control.text or "Apply").strip()
    locator = page.get_by_text(control_text, exact=False).first
    try:
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
            return PageDecision(status="no_apply_button", error=f"popup_stayed_on_linkedin:{popup.url}", control=control)
        finally:
            popup.close()
    except PlaywrightTimeoutError:
        try:
            locator.click(timeout=options.click_timeout_ms)
            page.wait_for_load_state("domcontentloaded", timeout=options.click_timeout_ms)
            if is_external_apply_url(page.url):
                return PageDecision(status="resolved_offsite", final_url=page.url, control=control)
            return PageDecision(status="no_apply_button", error=f"same_tab_stayed_on_linkedin:{page.url}", control=control)
        except PlaywrightTimeoutError:
            return PageDecision(status="timeout", error="click_timeout", control=control)
        except Exception as exc:
            return PageDecision(status="error", error=str(exc)[:200], control=control)


def _run_live_browser(candidates: list[Candidate], options: ResolverOptions) -> ResolverSummary:
    counts: Counter[str] = Counter()
    stopped_reason: str | None = None
    proc = launch_chrome(options.worker_id, port=BASE_CDP_PORT + options.worker_id, headless=False, browser=options.browser)
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{BASE_CDP_PORT + options.worker_id}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            for candidate in candidates:
                decision = _resolve_one_with_context(context, candidate, options)
                record_resolution(
                    candidate.url,
                    status=decision.status,
                    final_url=decision.final_url,
                    error=decision.error,
                    refresh=options.refresh,
                )
                counts[decision.status] += 1
                if decision.stop_run or should_stop_run(decision.status):
                    stopped_reason = decision.status
                    break
                _sleep_between(options)
            browser.close()
    finally:
        cleanup_worker(options.worker_id, proc)
    return ResolverSummary(
        considered=len(candidates),
        dry_run=False,
        counts=dict(counts),
        stopped_reason=stopped_reason,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

Run:

```powershell
git add src/applypilot/linkedin_resolver.py tests/test_linkedin_resolver.py
git commit -m "Add LinkedIn resolver orchestration"
```

---

### Task 5: CLI Command And Output

**Files:**
- Modify: `src/applypilot/cli.py`
- Create: `tests/test_cli_linkedin_resolver.py`

- [ ] **Step 1: Add failing CLI test**

Create `tests/test_cli_linkedin_resolver.py` with:

```python
from __future__ import annotations

from typer.testing import CliRunner

from applypilot import cli
from applypilot import linkedin_resolver


def test_linkedin_resolve_apply_urls_dry_run_wires_options(monkeypatch):
    captured = {}

    def fake_run_resolver(options):
        captured["options"] = options
        return linkedin_resolver.ResolverSummary(
            considered=3,
            dry_run=True,
            counts={},
            sample_urls=["https://www.linkedin.com/jobs/view/1"],
        )

    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    monkeypatch.setattr(linkedin_resolver, "run_resolver", fake_run_resolver)

    result = CliRunner().invoke(
        cli.app,
        [
            "linkedin-resolve-apply-urls",
            "--limit",
            "3",
            "--delay-min",
            "12",
            "--delay-max",
            "30",
            "--tiers",
            "priority,recommended",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0
    assert captured["options"].limit == 3
    assert captured["options"].delay_min == 12
    assert captured["options"].delay_max == 30
    assert captured["options"].tiers == ("priority", "recommended")
    assert captured["options"].dry_run is True
    assert "LinkedIn external apply URL resolver" in result.output
    assert "dry run" in result.output.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli_linkedin_resolver.py -q
```

Expected: FAIL because the CLI command does not exist.

- [ ] **Step 3: Add the Typer command**

In `src/applypilot/cli.py`, add this command immediately after `linkedin_split_command()`:

```python
@app.command("linkedin-resolve-apply-urls")
def linkedin_resolve_apply_urls_command(
    limit: int = typer.Option(200, "--limit", help="Maximum unresolved LinkedIn jobs to inspect."),
    delay_min: float = typer.Option(8.0, "--delay-min", help="Minimum delay between LinkedIn job pages."),
    delay_max: float = typer.Option(20.0, "--delay-max", help="Maximum delay between LinkedIn job pages."),
    tiers: str = typer.Option("priority,recommended", "--tiers", help="Comma-separated audit labels to include."),
    include_low: bool = typer.Option(False, "--include-low", help="Also include review and low audit labels."),
    refresh: bool = typer.Option(False, "--refresh", help="Revisit rows with previous resolver statuses."),
    dry_run: bool = typer.Option(False, "--dry-run", help="List candidates without opening LinkedIn."),
    browser: str = typer.Option("chrome", "--browser", help="Browser profile source: chrome, edge, cft, chromium, or default."),
    worker_id: int = typer.Option(80, "--worker-id", help="Resolver browser worker id; keep separate from apply workers."),
) -> None:
    """Resolve external ATS apply URLs from LinkedIn job pages without applying."""
    _bootstrap()
    from applypilot import linkedin_resolver

    parsed_tiers = tuple(t.strip() for t in tiers.split(",") if t.strip())
    if not parsed_tiers:
        console.print("[red]--tiers must include at least one audit label.[/red]")
        raise typer.Exit(code=1)
    if delay_max < delay_min:
        console.print("[red]--delay-max must be greater than or equal to --delay-min.[/red]")
        raise typer.Exit(code=1)
    if browser.lower() not in {"chrome", "edge", "cft", "chromium", "default"}:
        console.print("[red]--browser must be one of chrome, edge, cft, chromium, or default.[/red]")
        raise typer.Exit(code=1)

    summary = linkedin_resolver.run_resolver(
        linkedin_resolver.ResolverOptions(
            limit=limit,
            tiers=parsed_tiers,
            include_low=include_low,
            refresh=refresh,
            dry_run=dry_run,
            delay_min=delay_min,
            delay_max=delay_max,
            browser=browser.lower(),
            worker_id=worker_id,
        )
    )

    console.print("\n[bold]LinkedIn external apply URL resolver[/bold]")
    console.print(f"  considered: {summary.considered}")
    if summary.dry_run:
        console.print("  mode:       dry run")
        for url in summary.sample_urls or []:
            console.print(f"  - {url}")
        return
    for status, count in sorted((summary.counts or {}).items()):
        console.print(f"  {status}: {count}")
    if summary.stopped_reason:
        console.print(f"  [yellow]stopped:[/yellow] {summary.stopped_reason}")
    console.print("[dim]Next: run `applypilot linkedin-split` to inspect the offsite/Easy Apply split.[/dim]")
```

- [ ] **Step 4: Update `linkedin-split` hint**

In `src/applypilot/cli.py`, update the `offsite == 0` message inside `linkedin_split_command()` to:

```python
    if offsite == 0:
        console.print(
            "[dim]No offsite URLs resolved yet. Run "
            "`applypilot linkedin-resolve-apply-urls --dry-run --limit 20` first, "
            "then a small live resolver pass if the candidate list looks right.[/dim]"
        )
```

- [ ] **Step 5: Run CLI and resolver tests**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_cli_linkedin_resolver.py tests/test_linkedin_resolver.py tests/test_apply_linkedin_cap.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

Run:

```powershell
git add src/applypilot/cli.py tests/test_cli_linkedin_resolver.py
git commit -m "Expose LinkedIn apply URL resolver command"
```

---

### Task 6: End-To-End Verification And Safe First Run

**Files:**
- No source files changed unless verification finds a concrete bug.

- [ ] **Step 1: Run the focused unit suite**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py tests/test_cli_linkedin_resolver.py tests/test_apply_linkedin_cap.py tests/test_apply_lane_filter.py -q
```

Expected: PASS.

- [ ] **Step 2: Run CLI dry-run against the real local DB**

Run:

```powershell
.\.venv\Scripts\python.exe -m applypilot linkedin-resolve-apply-urls --dry-run --limit 20
```

Expected: command exits `0`, prints `LinkedIn external apply URL resolver`, prints `mode: dry run`, and does not launch a browser.

- [ ] **Step 3: Check current LinkedIn split before live resolution**

Run:

```powershell
.\.venv\Scripts\python.exe -m applypilot linkedin-split
```

Expected: command exits `0` and reports the current offsite vs Easy Apply / unresolved counts.

- [ ] **Step 4: Run a small live resolver pass only if Chrome is logged into LinkedIn**

Run:

```powershell
.\.venv\Scripts\python.exe -m applypilot linkedin-resolve-apply-urls --limit 25 --delay-min 12 --delay-max 30 --tiers priority,recommended
```

Expected:
- Browser window opens visibly.
- If LinkedIn is logged in, resolver records statuses and exits normally.
- If LinkedIn shows login, checkpoint, captcha, unusual activity, or account restriction, resolver records `login_required` or `challenge_required`, stops the run, and does not continue hitting LinkedIn pages.

- [ ] **Step 5: Verify offsite split after the live pass**

Run:

```powershell
.\.venv\Scripts\python.exe -m applypilot linkedin-split
```

Expected: offsite count increases if any external LinkedIn apply buttons were resolved; Easy Apply count remains for true Easy Apply or unresolved jobs.

- [ ] **Step 6: Inspect DB metadata for the first few resolver rows**

Run:

```powershell
@'
from applypilot.database import get_connection
conn = get_connection()
rows = conn.execute("""
    SELECT url, application_url, linkedin_resolve_status, linkedin_resolve_error,
           linkedin_resolve_attempts, linkedin_resolve_final_url
      FROM jobs
     WHERE linkedin_resolve_status IS NOT NULL
     ORDER BY linkedin_resolved_at DESC
     LIMIT 10
""").fetchall()
for row in rows:
    print(dict(row))
'@ | .\.venv\Scripts\python.exe -
```

Expected: rows show explicit statuses, attempt counts, and external `application_url` only for `resolved_offsite`.

- [ ] **Step 7: Commit any verification fix, or leave code clean**

If a verification bug required code changes, run the focused tests again and commit:

```powershell
git add src/applypilot/linkedin_resolver.py src/applypilot/cli.py src/applypilot/database.py tests/test_linkedin_resolver.py tests/test_cli_linkedin_resolver.py
git commit -m "Harden LinkedIn apply URL resolver"
```

If no code changes were needed, do not create an empty commit.

---

## Final Verification Commands

Run before calling the implementation complete:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_linkedin_resolver.py tests/test_cli_linkedin_resolver.py tests/test_apply_linkedin_cap.py tests/test_apply_lane_filter.py -q
.\.venv\Scripts\python.exe -m applypilot linkedin-resolve-apply-urls --dry-run --limit 20
.\.venv\Scripts\python.exe -m applypilot linkedin-split
```

Expected:
- All pytest tests pass.
- Dry-run exits without launching Chrome.
- `linkedin-split` still runs and shows the live lane split.

## Operational Command After Implementation

Use this first:

```powershell
.\.venv\Scripts\python.exe -m applypilot linkedin-resolve-apply-urls --dry-run --limit 50 --tiers priority,recommended
```

Then, if the dry-run candidate list is correct and Chrome is logged into LinkedIn:

```powershell
.\.venv\Scripts\python.exe -m applypilot linkedin-resolve-apply-urls --limit 25 --delay-min 12 --delay-max 30 --tiers priority,recommended
.\.venv\Scripts\python.exe -m applypilot linkedin-split
```
