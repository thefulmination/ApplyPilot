# Fleet Email-Verification Reconcile — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve the fleet `apply_queue` `crash_unconfirmed` backlog by matching those jobs against application-outcome emails (`email_events`) and flipping confirmed ones to `applied`.

**Architecture:** A new pure-logic module `fleet/email_reconcile.py` + a home-side entrypoint `fleet/email_reconcile_main.py`. It reads outcome emails from the home SQLite brain and crash jobs from the fleet Postgres, reuses the existing `gmail_outcomes.match_email_to_job` fuzzy matcher to link them, and (only on `--apply`) flips confirmed jobs to `applied` with an audit row. Dry-run by default.

**Tech Stack:** Python 3.11, `psycopg` (PG, dict_row), `sqlite3` (home brain, read-only), `pytest`. Reuses `applypilot.gmail_outcomes.match_email_to_job`, `applypilot.outcome_scan.scan_outcomes`, `applypilot.apply.pgqueue.connect`.

## Global Constraints

- **Interpreter / test runner (verified):** `".conda-env/python.exe" -m pytest` run from repo root `C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot`. The `.venv` is stale — do not use it.
- **Safety invariants (from spec §3), every task must preserve:** never re-queue/re-apply a job; write only to fleet PG (`apply_queue` status of confirmed jobs + `email_reconcile_actions`); home brain opened **read-only** (`mode=ro`); strong-match-only auto-flip; dry-run unless `--apply`; status-guarded idempotent writes.
- **Match policy (spec §6):** `STRONG_METHODS = {board_slug, linkedin_job_id, company_domain}`; `MIN_STRONG = 0.6`; confirming stages `{acknowledged, screen, assessment, interview, offer, rejected}`.
- **Test style:** follow `tests/test_diagnoser.py` — pure functions + in-memory sqlite + a `_FakeConn`/`_FakeCursor` for PG; no live services in unit tests.
- **Lint:** `".conda-env/python.exe" -m ruff check <files>` must pass (line-length 120).

---

### Task 1: Core module — dataclasses, constants, `classify_match`

**Files:**
- Create: `src/applypilot/fleet/email_reconcile.py`
- Test: `tests/test_email_reconcile.py`

**Interfaces:**
- Produces:
  - `CONFIRMING_STAGES: frozenset[str]`, `STRONG_METHODS: frozenset[str]`, `MIN_STRONG: float = 0.6`
  - `@dataclass OutcomeEmail(message_id, sender, subject, body, company, title, job_url, stage, occurred_at)`
  - `@dataclass Resolution(job_url, message_id, method, score, stage, occurred_at, classification)`
  - `@dataclass ReconcileResult(confirmed: list[Resolution], probable: list[Resolution], unmatched_emails: int, jobs_total: int)`
  - `classify_match(method: str | None, score: float | None, *, min_strong: float = MIN_STRONG) -> str | None` → `"confirmed"`, `"probable"`, or `None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_email_reconcile.py
from applypilot.fleet import email_reconcile as er

def test_classify_strong_method_is_confirmed_regardless_of_score():
    assert er.classify_match("company_domain", 1.0) == "confirmed"
    assert er.classify_match("board_slug", 1.0) == "confirmed"
    assert er.classify_match("linkedin_job_id", 1.0) == "confirmed"

def test_classify_fuzzy_at_or_above_threshold_is_confirmed():
    assert er.classify_match("company_name", 0.6) == "confirmed"
    assert er.classify_match("title", 0.75) == "confirmed"

def test_classify_fuzzy_below_threshold_is_probable():
    assert er.classify_match("company_name", 0.59) == "probable"
    assert er.classify_match("ats_domain", 0.25) == "probable"

def test_classify_no_match_is_none():
    assert er.classify_match(None, None) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'applypilot.fleet.email_reconcile'`

- [ ] **Step 3: Write minimal implementation**

```python
# src/applypilot/fleet/email_reconcile.py
"""Fleet email-verification reconcile (Phase 1). Match crash_unconfirmed apply jobs to
application-outcome emails (email_events) and flip confirmed ones to 'applied'. Advisory/
dry-run by default; writes only to the fleet Postgres. Reuses gmail_outcomes.match_email_to_job."""
from __future__ import annotations
from dataclasses import dataclass

CONFIRMING_STAGES = frozenset({"acknowledged", "screen", "assessment", "interview", "offer", "rejected"})
STRONG_METHODS = frozenset({"board_slug", "linkedin_job_id", "company_domain"})
MIN_STRONG = 0.6


@dataclass
class OutcomeEmail:
    message_id: str
    sender: str
    subject: str
    body: str
    company: str
    title: str
    job_url: str | None
    stage: str
    occurred_at: str | None


@dataclass
class Resolution:
    job_url: str
    message_id: str
    method: str
    score: float
    stage: str
    occurred_at: str | None
    classification: str  # "confirmed" | "probable"


@dataclass
class ReconcileResult:
    confirmed: list
    probable: list
    unmatched_emails: int
    jobs_total: int


def classify_match(method: str | None, score: float | None, *, min_strong: float = MIN_STRONG) -> str | None:
    """confirmed if a strong (exact-ish) method or a fuzzy score >= min_strong; probable for a
    weaker fuzzy hit; None when there was no match at all."""
    if method is None:
        return None
    if method in STRONG_METHODS or (score is not None and score >= min_strong):
        return "confirmed"
    return "probable"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/email_reconcile.py tests/test_email_reconcile.py
git commit -m "feat(email-reconcile): core dataclasses + classify_match policy"
```

---

### Task 2: `load_outcome_emails` (read home brain `email_events`, read-only)

**Files:**
- Modify: `src/applypilot/fleet/email_reconcile.py`
- Test: `tests/test_email_reconcile.py`

**Interfaces:**
- Consumes: a `sqlite3.Connection` to the home brain.
- Produces: `load_outcome_emails(conn) -> list[OutcomeEmail]` — only rows whose `stage in CONFIRMING_STAGES`.

- [ ] **Step 1: Write the failing test**

```python
import sqlite3

def _mk_home_db():
    c = sqlite3.connect(":memory:")
    c.execute("""CREATE TABLE email_events (message_id TEXT PRIMARY KEY, sender TEXT, subject TEXT,
                 body_text TEXT, company TEXT, title TEXT, job_url TEXT, stage TEXT NOT NULL,
                 occurred_at TEXT)""")
    rows = [
        ("m1", "jobs@stripe.com", "Application received", "thanks", "Stripe", "Analyst",
         "https://stripe.com/jobs/1", "acknowledged", "2026-06-29T10:00:00+00:00"),
        ("m2", "no-reply@x.com", "Unsubscribe", "promo", "", "", None, "other", "2026-06-29T11:00:00+00:00"),
        ("m3", "talent@acme.com", "Update on your application", "regret", "Acme", "Engineer",
         "https://acme.com/careers/9", "rejected", "2026-06-29T12:00:00+00:00"),
    ]
    c.executemany("INSERT INTO email_events VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return c

def test_load_outcome_emails_keeps_only_confirming_stages():
    emails = er.load_outcome_emails(_mk_home_db())
    ids = {e.message_id for e in emails}
    assert ids == {"m1", "m3"}          # "other" dropped
    m1 = next(e for e in emails if e.message_id == "m1")
    assert m1.company == "Stripe" and m1.stage == "acknowledged"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py::test_load_outcome_emails_keeps_only_confirming_stages -v`
Expected: FAIL — `AttributeError: module ... has no attribute 'load_outcome_emails'`

- [ ] **Step 3: Write minimal implementation**

Append to `email_reconcile.py`:

```python
def load_outcome_emails(conn) -> list:
    """Read submission-proving outcome emails from the home brain's email_events table.
    Caller opens the sqlite connection read-only."""
    placeholders = ",".join("?" for _ in CONFIRMING_STAGES)
    cur = conn.execute(
        f"SELECT message_id, sender, subject, body_text, company, title, job_url, stage, occurred_at "
        f"FROM email_events WHERE stage IN ({placeholders})",
        tuple(sorted(CONFIRMING_STAGES)),
    )
    out = []
    for r in cur.fetchall():
        out.append(OutcomeEmail(
            message_id=r[0], sender=r[1] or "", subject=r[2] or "", body=r[3] or "",
            company=r[4] or "", title=r[5] or "", job_url=r[6], stage=r[7], occurred_at=r[8],
        ))
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/email_reconcile.py tests/test_email_reconcile.py
git commit -m "feat(email-reconcile): load_outcome_emails (confirming stages only)"
```

---

### Task 3: `load_crash_jobs` (read fleet PG `apply_queue`, shaped for the matcher)

**Files:**
- Modify: `src/applypilot/fleet/email_reconcile.py`
- Test: `tests/test_email_reconcile.py`

**Interfaces:**
- Consumes: a psycopg connection (dict_row cursors, like `test_diagnoser._FakeConn`).
- Produces: `load_crash_jobs(conn) -> list[dict]` — each dict has `url, application_url, company, title, site` (`site` = `apply_domain`, the field `match_email_to_job` reads).

- [ ] **Step 1: Write the failing test**

```python
# Reuse the fake-conn pattern from test_diagnoser.py
class _FakeCursor:
    def __init__(self, rows): self._rows = rows; self.executed = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None): self.executed.append((sql, params))
    def fetchall(self): return self._rows

class _FakeConn:
    def __init__(self, rows): self._cur = _FakeCursor(rows)
    def cursor(self): return self._cur

def test_load_crash_jobs_shapes_candidates_for_matcher():
    rows = [{"url": "https://stripe.com/jobs/1", "application_url": "https://boards.greenhouse.io/stripe/jobs/1",
             "company": "Stripe", "title": "Analyst", "apply_domain": "boards.greenhouse.io"}]
    jobs = er.load_crash_jobs(_FakeConn(rows))
    assert jobs[0]["site"] == "boards.greenhouse.io"       # apply_domain -> site
    assert jobs[0]["company"] == "Stripe" and jobs[0]["url"] == "https://stripe.com/jobs/1"

def test_load_crash_jobs_filters_no_result_line_bucket():
    fc = _FakeConn([])
    er.load_crash_jobs(fc)
    sql = fc._cur.executed[0][0].replace(" ", "")
    assert "status='crash_unconfirmed'" in sql
    assert "failed:no_result_line" in sql
```

- [ ] **Step 2: Run test to verify it fails**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -k load_crash_jobs -v`
Expected: FAIL — `AttributeError: ... 'load_crash_jobs'`

- [ ] **Step 3: Write minimal implementation**

Append to `email_reconcile.py`:

```python
def load_crash_jobs(conn) -> list[dict]:
    """Read the crash_unconfirmed / no_result_line jobs and shape them as match_email_to_job
    candidates (site = apply_domain). Read-only."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT url, application_url, company, title, apply_domain "
            "FROM apply_queue WHERE status='crash_unconfirmed' AND apply_error='failed:no_result_line'"
        )
        out = []
        for r in cur.fetchall():
            out.append({
                "url": r["url"], "application_url": r["application_url"],
                "company": r["company"], "title": r["title"], "site": r["apply_domain"],
            })
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -k load_crash_jobs -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/email_reconcile.py tests/test_email_reconcile.py
git commit -m "feat(email-reconcile): load_crash_jobs candidate shaping"
```

---

### Task 4: `reconcile` — match emails to crash jobs (reuse real matcher), classify, dedupe

**Files:**
- Modify: `src/applypilot/fleet/email_reconcile.py`
- Test: `tests/test_email_reconcile.py`

**Interfaces:**
- Consumes: `match_email_to_job` from `applypilot.gmail_outcomes`; `OutcomeEmail`, `classify_match`, `Resolution`, `ReconcileResult`.
- Produces: `reconcile(emails: list[OutcomeEmail], jobs: list[dict], *, min_strong=MIN_STRONG) -> ReconcileResult`. One job resolves at most once (highest score wins; confirmed beats probable).

- [ ] **Step 1: Write the failing test**

```python
def _email(**kw):
    base = dict(message_id="m", sender="", subject="", body="", company="", title="",
                job_url=None, stage="acknowledged", occurred_at="2026-06-29T10:00:00+00:00")
    base.update(kw); return er.OutcomeEmail(**base)

def test_reconcile_company_domain_is_confirmed():
    jobs = [{"url": "https://stripe.com/jobs/1", "application_url": "", "company": "Stripe",
             "title": "Analyst", "site": "stripe.com"}]
    emails = [_email(message_id="m1", sender="jobs@stripe.com", subject="Application received")]
    res = er.reconcile(emails, jobs)
    assert len(res.confirmed) == 1
    r = res.confirmed[0]
    assert r.job_url == "https://stripe.com/jobs/1" and r.method == "company_domain" and r.classification == "confirmed"

def test_reconcile_no_overlap_is_unmatched():
    jobs = [{"url": "https://stripe.com/jobs/1", "application_url": "", "company": "Stripe",
             "title": "Analyst", "site": "stripe.com"}]
    emails = [_email(message_id="m9", sender="news@randombrand.com", subject="Weekly digest", body="sale")]
    res = er.reconcile(emails, jobs)
    assert res.confirmed == [] and res.probable == [] and res.unmatched_emails == 1

def test_reconcile_resolves_each_job_once():
    # Two emails both match the same job; it must resolve to exactly one Resolution (dedupe).
    jobs = [{"url": "https://stripe.com/jobs/1", "application_url": "", "company": "Stripe",
             "title": "Analyst", "site": "stripe.com"}]
    emails = [
        _email(message_id="a", sender="jobs@stripe.com", subject="Application received"),
        _email(message_id="b", sender="careers@stripe.com", subject="We got your application"),
    ]
    res = er.reconcile(emails, jobs)
    all_urls = [r.job_url for r in res.confirmed + res.probable]
    assert all_urls.count("https://stripe.com/jobs/1") == 1   # deduped to one resolution
    assert len(res.confirmed) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -k reconcile -v`
Expected: FAIL — `AttributeError: ... 'reconcile'`

- [ ] **Step 3: Write minimal implementation**

Append to `email_reconcile.py` (add the import at the top of the file):

```python
from applypilot.gmail_outcomes import match_email_to_job


def reconcile(emails: list, jobs: list[dict], *, min_strong: float = MIN_STRONG) -> ReconcileResult:
    """Match each outcome email to a crash job via the existing fuzzy matcher and classify the hit.
    A job is resolved at most once: the highest-scoring hit wins (a strong method scores 1.0)."""
    best: dict[str, Resolution] = {}   # job_url -> best Resolution
    unmatched = 0
    for e in emails:
        job, method, score = match_email_to_job(e.sender, e.subject, e.body, jobs)
        cls = classify_match(method, score, min_strong=min_strong)
        if job is None or cls is None:
            unmatched += 1
            continue
        url = job["url"]
        cand = Resolution(job_url=url, message_id=e.message_id, method=method, score=float(score),
                          stage=e.stage, occurred_at=e.occurred_at, classification=cls)
        prev = best.get(url)
        if prev is None or cand.score > prev.score:
            best[url] = cand
    confirmed = [r for r in best.values() if r.classification == "confirmed"]
    probable = [r for r in best.values() if r.classification == "probable"]
    return ReconcileResult(confirmed=confirmed, probable=probable,
                           unmatched_emails=unmatched, jobs_total=len(jobs))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -k reconcile -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/email_reconcile.py tests/test_email_reconcile.py
git commit -m "feat(email-reconcile): reconcile() match+classify+dedupe"
```

---

### Task 5: Schema audit table + `apply_resolutions` (status-guarded, idempotent)

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql` (add `email_reconcile_actions`)
- Modify: `src/applypilot/fleet/email_reconcile.py`
- Test: `tests/test_email_reconcile.py`

**Interfaces:**
- Consumes: a psycopg connection; a `ReconcileResult`.
- Produces: `apply_resolutions(conn, result: ReconcileResult, *, include_probable: bool = False) -> dict` returning counts `{"flipped": int, "skipped": int}`. Per job: status-guarded `UPDATE apply_queue ... WHERE url=%s AND status='crash_unconfirmed'`, plus one `INSERT INTO email_reconcile_actions`.

- [ ] **Step 1: Write the failing test**

```python
class _ScriptCursor:
    def __init__(self, rowcounts): self._rc = list(rowcounts); self.executed = []; self.rowcount = 0
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if sql.strip().upper().startswith("UPDATE"):
            self.rowcount = self._rc.pop(0) if self._rc else 0

class _ScriptConn:
    def __init__(self, rowcounts): self._cur = _ScriptCursor(rowcounts); self.commits = 0
    def cursor(self): return self._cur
    def commit(self): self.commits += 1

def _res(confirmed=(), probable=()):
    return er.ReconcileResult(confirmed=list(confirmed), probable=list(probable),
                              unmatched_emails=0, jobs_total=0)

def _r(url, cls="confirmed"):
    return er.Resolution(job_url=url, message_id="m", method="company_domain", score=1.0,
                         stage="acknowledged", occurred_at="2026-06-29T10:00:00+00:00", classification=cls)

def test_apply_resolutions_flips_confirmed_and_audits():
    conn = _ScriptConn(rowcounts=[1])               # UPDATE affects 1 row
    counts = er.apply_resolutions(conn, _res(confirmed=[_r("u1")]))
    assert counts == {"flipped": 1, "skipped": 0}
    updates = [e for e in conn._cur.executed if e[0].strip().upper().startswith("UPDATE apply_queue".upper())]
    inserts = [e for e in conn._cur.executed if "INSERT INTO email_reconcile_actions" in e[0]]
    assert len(updates) == 1 and len(inserts) == 1
    assert "status='applied'" in updates[0][0].replace(" ", "") or "status = 'applied'" in updates[0][0]
    assert "status='crash_unconfirmed'" in updates[0][0].replace(" ", "")   # guarded

def test_apply_resolutions_skips_when_row_already_moved():
    conn = _ScriptConn(rowcounts=[0])               # UPDATE affects 0 rows (already not crash)
    counts = er.apply_resolutions(conn, _res(confirmed=[_r("u1")]))
    assert counts == {"flipped": 0, "skipped": 1}
    assert not any("INSERT INTO email_reconcile_actions" in e[0] for e in conn._cur.executed)

def test_apply_resolutions_excludes_probable_by_default():
    conn = _ScriptConn(rowcounts=[1])
    counts = er.apply_resolutions(conn, _res(probable=[_r("u2", cls="probable")]))
    assert counts == {"flipped": 0, "skipped": 0}    # probable not applied unless included
    assert not any(e[0].strip().upper().startswith("UPDATE") for e in conn._cur.executed)

def test_apply_resolutions_includes_probable_when_opted_in():
    conn = _ScriptConn(rowcounts=[1])
    counts = er.apply_resolutions(conn, _res(probable=[_r("u2", cls="probable")]), include_probable=True)
    assert counts == {"flipped": 1, "skipped": 0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -k apply_resolutions -v`
Expected: FAIL — `AttributeError: ... 'apply_resolutions'`

- [ ] **Step 3a: Add the audit table to `schema_v3.sql`**

Append near the other fleet tables:

```sql
-- email_reconcile_actions: audit + reversibility for the email-verification reconcile.
CREATE TABLE IF NOT EXISTS email_reconcile_actions (
    id              BIGSERIAL PRIMARY KEY,
    url             TEXT,
    message_id      TEXT,
    match_method    TEXT,
    match_score     REAL,
    stage           TEXT,
    prior_status    TEXT,
    how_to_reverse  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

- [ ] **Step 3b: Implement `apply_resolutions`**

Append to `email_reconcile.py`:

```python
def apply_resolutions(conn, result: ReconcileResult, *, include_probable: bool = False) -> dict:
    """Flip confirmed (and, if opted-in, probable) jobs crash_unconfirmed -> applied, guarded on
    the current status so it is idempotent and never clobbers a row another process moved. Writes
    one audit row per flip. One transaction per job."""
    targets = list(result.confirmed) + (list(result.probable) if include_probable else [])
    flipped = skipped = 0
    for r in targets:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET status='applied', apply_status='applied', apply_error=NULL, "
                "applied_at=COALESCE(applied_at, %s), updated_at=now() "
                "WHERE url=%s AND status='crash_unconfirmed'",
                (r.occurred_at, r.job_url),
            )
            if cur.rowcount == 0:
                skipped += 1
                continue
            cur.execute(
                "INSERT INTO email_reconcile_actions (url, message_id, match_method, match_score, "
                "stage, prior_status, how_to_reverse) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (r.job_url, r.message_id, r.method, r.score, r.stage, "crash_unconfirmed",
                 "Set apply_queue.status back to 'crash_unconfirmed', apply_status='crash_unconfirmed', "
                 "apply_error='failed:no_result_line' WHERE url matches."),
            )
            flipped += 1
        conn.commit()
    return {"flipped": flipped, "skipped": skipped}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -k apply_resolutions -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/email_reconcile.py src/applypilot/fleet/schema_v3.sql tests/test_email_reconcile.py
git commit -m "feat(email-reconcile): apply_resolutions + email_reconcile_actions audit table"
```

---

### Task 6: Entrypoint, report formatting, and script registration

**Files:**
- Create: `src/applypilot/fleet/email_reconcile_main.py`
- Modify: `pyproject.toml` (`[project.scripts]`)
- Modify: `src/applypilot/fleet/email_reconcile.py` (add `format_report`)
- Test: `tests/test_email_reconcile.py`

**Interfaces:**
- Consumes: `ReconcileResult`; `applypilot.outcome_scan.scan_outcomes`; `applypilot.apply.pgqueue.connect`; `applypilot.fleet.email_reconcile` functions.
- Produces: `format_report(result: ReconcileResult) -> str`; `email_reconcile_main.main(argv=None) -> int`.

- [ ] **Step 1: Write the failing test (pure report formatter)**

```python
def test_format_report_summarizes_counts():
    res = er.ReconcileResult(confirmed=[_r("u1")], probable=[_r("u2", cls="probable")],
                             unmatched_emails=5, jobs_total=480)
    text = er.format_report(res)
    assert "confirmed: 1" in text.lower()
    assert "probable: 1" in text.lower()
    assert "480" in text            # jobs_total surfaced
```

(`_r` is defined in the Task 5 tests; if running this task standalone, copy the `_r` helper into the test file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -k format_report -v`
Expected: FAIL — `AttributeError: ... 'format_report'`

- [ ] **Step 3a: Implement `format_report` in `email_reconcile.py`**

```python
def format_report(result: ReconcileResult) -> str:
    lines = [
        f"crash jobs considered: {result.jobs_total}",
        f"confirmed: {len(result.confirmed)}",
        f"probable: {len(result.probable)}",
        f"unmatched emails: {result.unmatched_emails}",
    ]
    for r in sorted(result.confirmed, key=lambda x: x.method):
        lines.append(f"  [confirmed] {r.method} {r.score:.2f} {r.stage} -> {r.job_url}")
    for r in sorted(result.probable, key=lambda x: -x.score):
        lines.append(f"  [probable]  {r.method} {r.score:.2f} {r.stage} -> {r.job_url}")
    return "\n".join(lines)
```

- [ ] **Step 3b: Create the entrypoint**

```python
# src/applypilot/fleet/email_reconcile_main.py
"""applypilot-fleet-reconcile-email: match crash_unconfirmed apply jobs to outcome emails and
(with --apply) flip confirmed ones to 'applied'. Dry-run by default. Home-side: needs the home
brain (read-only) and the fleet Postgres. ADVISORY unless --apply; never re-applies a job."""
from __future__ import annotations
import argparse
import os
import sqlite3
import sys

from applypilot.apply import pgqueue
from applypilot.fleet import email_reconcile as er


def _default_home_db() -> str:
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "ApplyPilot", "applypilot.db")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="applypilot-fleet-reconcile-email")
    p.add_argument("--dsn", default=None, help="Fleet Postgres DSN (default: env).")
    p.add_argument("--home-db", default=_default_home_db(), help="Home brain SQLite path.")
    p.add_argument("--scan-days", type=int, default=7, help="Gmail look-back for the Phase-0 scan.")
    p.add_argument("--no-scan", action="store_true", help="Skip the Phase-0 Gmail scan.")
    p.add_argument("--apply", action="store_true", help="Flip CONFIRMED matches to applied.")
    p.add_argument("--apply-probable", action="store_true", help="Also flip probable matches.")
    p.add_argument("--min-score", type=float, default=er.MIN_STRONG, help="Fuzzy confirm threshold.")
    args = p.parse_args(argv)

    if not args.no_scan:
        try:
            from applypilot.outcome_scan import scan_outcomes
            counts = scan_outcomes(days=args.scan_days)
            print(f"phase0 scan: {counts}")
        except Exception as exc:  # best-effort enrichment; reconcile still runs on existing data
            print(f"phase0 scan skipped ({type(exc).__name__}: {exc}); using existing email_events")

    home = sqlite3.connect(f"file:{args.home_db}?mode=ro", uri=True)
    try:
        emails = er.load_outcome_emails(home)
    finally:
        home.close()

    with pgqueue.connect(args.dsn) as conn:
        jobs = er.load_crash_jobs(conn)
        result = er.reconcile(emails, jobs, min_strong=args.min_score)
        print(er.format_report(result))
        if args.apply or args.apply_probable:
            counts = er.apply_resolutions(conn, result, include_probable=args.apply_probable)
            print(f"applied: {counts}")
        else:
            print("(dry-run; pass --apply to flip confirmed matches)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3c: Register the console script in `pyproject.toml`**

Under `[project.scripts]`, after the `applypilot-fleet-remediate` line, add:

```toml
applypilot-fleet-reconcile-email = "applypilot.fleet.email_reconcile_main:main"
```

- [ ] **Step 4: Run tests + lint**

Run: `".conda-env/python.exe" -m pytest tests/test_email_reconcile.py -v`
Expected: PASS (all tasks' tests)
Run: `".conda-env/python.exe" -m ruff check src/applypilot/fleet/email_reconcile.py src/applypilot/fleet/email_reconcile_main.py tests/test_email_reconcile.py`
Expected: All checks passed!

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/fleet/email_reconcile_main.py src/applypilot/fleet/email_reconcile.py pyproject.toml tests/test_email_reconcile.py
git commit -m "feat(email-reconcile): CLI entrypoint + report + script registration"
```

---

### Task 7: Live dry-run verification (real data, read-only)

**Files:** none (verification only)

- [ ] **Step 1: Editable reinstall so the new console script resolves**

Run: `".conda-env/python.exe" -m pip install -e . --no-deps -q`

- [ ] **Step 2: Run the reconcile in dry-run against live data (no writes)**

Run (from repo root, env has `APPLYPILOT_FLEET_DSN`):
`".conda-env/python.exe" -m applypilot.fleet.email_reconcile_main --no-scan`
Expected: a report printing `crash jobs considered: 480`, a `confirmed:` / `probable:` breakdown, and `(dry-run; ...)`. No DB writes (verify `SELECT count(*) FROM email_reconcile_actions` is unchanged / table empty).

- [ ] **Step 3: Record the yield**

Note the confirmed/probable counts in the PR description. Decide with the owner whether to run `--apply` (and whether to run a fresh `--scan-days 7` first to raise the yield).

---

## Self-Review

**Spec coverage:** §1 goal → Tasks 4–6; §2 (reuse fuzzy matcher) → Task 4; §3 safety invariants → Tasks 5 (status-guard, audit, no re-apply) & 6 (dry-run default, read-only home); §4 data flow → Task 6 (Phase 0 scan + Phase 1); §5 modules → Tasks 1–6; §6 match policy → Tasks 1 & 4; §7 error handling → Task 6 (best-effort scan, read-only home) & Task 5 (status-guard); §8 testing → every task; §9 interface → Task 6; §10 out-of-scope → respected (no re-attempt code anywhere); §11 success criteria → Task 7. No gaps.

**Placeholder scan:** No TBD/TODO; every code step shows complete code. (Task 3 Step 1 contains a deliberately-replaced fragment with the corrected behavioral test immediately following — implementer uses `test_load_crash_jobs_filters_no_result_line_bucket`.)

**Type consistency:** `OutcomeEmail`/`Resolution`/`ReconcileResult` fields and `classify_match`/`reconcile`/`apply_resolutions`/`format_report` signatures are consistent across Tasks 1→6. `match_email_to_job(sender, subject, body, applied_jobs)` and candidate keys (`url, application_url, company, title, site`) match `gmail_outcomes.py`. PG fake-conn patterns mirror `test_diagnoser.py`.
