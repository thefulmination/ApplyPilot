# Outcome-Loop Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop email→job misattribution (temporal guard + same-company disambiguation + quarantine tier), re-audit stored events, and put the outcome scan on a daily schedule.

**Architecture:** All matching changes live in `gmail_outcomes.match_email_to_job` (single source of truth used by the scan and the reconciler). The scan writer (`outcome_scan.py`) persists the new quarantine columns; consumers filter on them. A `--reaudit` mode replays the guards over stored rows with a reversible audit trail.

**Tech Stack:** Python 3.11 (`.conda-env\python.exe`), sqlite3, pytest, typer CLI, PowerShell scheduled task.

**Spec:** `docs/superpowers/specs/2026-07-03-outcome-loop-integrity-design.md` (approved 2026-07-03). This plan AMENDS two spec details discovered during planning (see Global Constraints #6, #7).

## Global Constraints

1. Run all tests with `.\.conda-env\python.exe -m pytest` from the repo root (`C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot`). The `.venv` is stale — never use it.
2. SHARED BRANCH (`applypilot-hardening-and-brainstorm-integration`, other sessions active): `git add` ONLY the files you touched, NEVER `-A`/`-u`. Do not touch `src/applypilot/apply/launcher.py`, `src/applypilot/fleet/diagnoser.py`, or any file you didn't change.
3. Temporal grace is exactly `MATCH_GRACE_SECONDS = 3600` (1 hour), one module constant in `gmail_outcomes.py`.
4. Quarantine reasons are exactly: `predates_application`, `ambiguous_company`, `no_timestamp`. Match statuses are exactly: `attributed`, `needs_review` (function-level third value `unmatched` is never persisted — unmatched rows keep `match_status=NULL`, `job_url=NULL`).
5. A confirmed apply is NEVER demoted by anything in this plan.
6. SPEC AMENDMENT (planning discovery): `match_email_to_job` has THREE production call sites, not one — `gmail_outcomes.py:861` (scan_inbox), `outcome_scan.py:46` (build_email_event), `fleet/email_reconcile.py:116` (reconcile). All three are updated. The reconciler's candidates are crash_unconfirmed jobs with NO `applied_at`; their guard timestamp is the PG row's `updated_at` (the crash mark) — the caller supplies it as `guard_after`.
7. SPEC AMENDMENT: `fleet/remediator.py`'s `has_confirming_email` veto (remediator.py:65) is deliberately NOT filtered to attributed events — it is NEGATIVE evidence (blocks re-queueing), and a quarantined "thanks for applying" from that company should still conservatively veto a re-queue. Only POSITIVE-evidence consumers (reconcile confirm flips) get guards.

---

### Task 1: MatchResult + temporal guard in the matcher

**Files:**
- Modify: `src/applypilot/gmail_outcomes.py` (function `match_email_to_job` at ~:588; `get_applied_jobs` at ~:640; internal caller at ~:861)
- Modify: `src/applypilot/outcome_scan.py:46` (caller)
- Test: `tests/test_gmail_outcomes.py` (existing 3-tuple unpack call sites at :396-:527 must keep passing — see Step 3)

**Interfaces:**
- Produces: `MatchResult` dataclass in `gmail_outcomes.py`:
  ```python
  @dataclass
  class MatchResult:
      job: dict | None
      method: str | None
      score: float | None
      status: str          # "attributed" | "needs_review" | "unmatched"
      reason: str | None = None   # quarantine reason when status == "needs_review"
      def astuple(self): return (self.job, self.method, self.score)
  ```
- Produces: `match_email_to_job(sender, subject, body, applied_jobs, *, min_overlap=0.25, occurred_at=None) -> MatchResult`. `occurred_at` is an ISO-8601 string (or None). Each job dict MAY carry `applied_at` (ISO str) and/or `guard_after` (ISO str); the guard timestamp for a job is `job.get("applied_at") or job.get("guard_after")`.
- Produces: `MATCH_GRACE_SECONDS = 3600` module constant.
- Guard semantics: a job is match-eligible iff its guard timestamp is None (no basis to judge — stays eligible) or `parse(occurred_at) + MATCH_GRACE_SECONDS >= parse(guard timestamp)`. If `occurred_at` is None but at least one candidate HAS a guard timestamp, the whole match returns `MatchResult(None, None, None, "needs_review", "no_timestamp")`. If every tier's winning candidates were removed by the guard (i.e., a tier WOULD have matched but all its hits are ineligible), return `MatchResult(None, None, None, "needs_review", "predates_application")`. If nothing matched at all: `MatchResult(None, None, None, "unmatched")`.
- `get_applied_jobs` SELECT gains `j.applied_at` (add `j.applied_at,` after `j.apply_status,` in the query at gmail_outcomes.py:~646).

- [ ] **Step 1: Write the failing tests** — append to `tests/test_gmail_outcomes.py`:

```python
# ---------------------------------------------------------------------------
# Temporal guard (audit 2026-07-02: 2/26 rejections provably predate their apply)
# ---------------------------------------------------------------------------
from applypilot.gmail_outcomes import MatchResult


class TestTemporalGuard:
    def _job(self, **kw):
        base = {"url": "https://boards.greenhouse.io/checkr/jobs/1",
                "application_url": "https://boards.greenhouse.io/checkr/jobs/1",
                "title": "Analyst", "site": "Checkr", "company": "Checkr",
                "applied_at": "2026-06-28T12:00:00+00:00"}
        base.update(kw)
        return base

    def test_email_predating_application_is_quarantined(self):
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io",
            "Your application to Checkr",
            "Thank you for applying to Checkr. Unfortunately...",
            [self._job()],
            occurred_at="2026-06-20T12:00:00+00:00",   # 8 days BEFORE applied_at
        )
        assert isinstance(r, MatchResult)
        assert r.job is None
        assert r.status == "needs_review"
        assert r.reason == "predates_application"

    def test_email_within_grace_passes(self):
        # acknowledgment 5 minutes BEFORE applied_at stamp (clock skew) still matches
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io",
            "Your application to Checkr",
            "Thank you for applying to Checkr.",
            [self._job()],
            occurred_at="2026-06-28T11:55:00+00:00",
        )
        assert r.status == "attributed"
        assert r.job is not None

    def test_exact_board_slug_is_also_guarded(self):
        # spec: the guard applies to EVERY tier, exact ones included
        job = self._job()
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io",
            "Update",
            "See https://boards.greenhouse.io/checkr/jobs/1",
            [job],
            occurred_at="2026-06-01T00:00:00+00:00",
        )
        assert r.status == "needs_review" and r.reason == "predates_application"

    def test_missing_occurred_at_with_guarded_candidates_quarantines(self):
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io", "Your application to Checkr",
            "Thanks for applying to Checkr.", [self._job()], occurred_at=None,
        )
        assert r.status == "needs_review" and r.reason == "no_timestamp"

    def test_no_timestamps_anywhere_stays_eligible(self):
        # candidates without applied_at/guard_after are judged as before (back-compat)
        job = self._job(); del job["applied_at"]
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io", "Your application to Checkr",
            "Thanks for applying to Checkr.", [job], occurred_at=None,
        )
        assert r.status == "attributed" and r.job is not None

    def test_guard_after_is_honored_for_crash_candidates(self):
        job = self._job(); del job["applied_at"]; job["guard_after"] = "2026-06-28T12:00:00+00:00"
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io", "Your application to Checkr",
            "Thanks for applying to Checkr.", [job],
            occurred_at="2026-06-20T12:00:00+00:00",
        )
        assert r.status == "needs_review" and r.reason == "predates_application"
```

- [ ] **Step 2: Run to verify failure** — `.\.conda-env\python.exe -m pytest tests/test_gmail_outcomes.py::TestTemporalGuard -q` → FAIL (ImportError: MatchResult / TypeError: unexpected keyword `occurred_at`).

- [ ] **Step 3: Implement.** In `gmail_outcomes.py`: add the dataclass + constant; inside `match_email_to_job`, FIRST compute the eligible-candidates list:

```python
def _parse_iso(v):
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None

MATCH_GRACE_SECONDS = 3600

# inside match_email_to_job(..., occurred_at=None):
    guarded = [j for j in applied_jobs if _parse_iso(j.get("applied_at") or j.get("guard_after"))]
    email_dt = _parse_iso(occurred_at)
    if guarded and email_dt is None:
        # candidates carry timestamps but the email has no parseable date -> never guess
        return MatchResult(None, None, None, "needs_review", "no_timestamp")
    def _eligible(j):
        g = _parse_iso(j.get("applied_at") or j.get("guard_after"))
        return g is None or email_dt + timedelta(seconds=MATCH_GRACE_SECONDS) >= g
    eligible = [j for j in applied_jobs if _eligible(j)] if email_dt else list(applied_jobs)
```

Run every existing tier over `eligible` instead of `applied_jobs`. After the tiers, detect the guard-only miss: re-run the tiers over the ORIGINAL `applied_jobs` list only if no match was found AND `len(eligible) < len(applied_jobs)`; a hit there means the guard was the only reason → `MatchResult(None, None, None, "needs_review", "predates_application")`. (Implement by extracting the existing tier cascade into `_match_tiers(sender, subject, body, jobs, min_overlap) -> tuple[job, method, score] | None` and calling it twice — no logic duplication.) Wrap the normal hit as `MatchResult(job, method, score, "attributed")` and the total miss as `MatchResult(None, None, None, "unmatched")`.
Update the two scan callers to keyword-pass `occurred_at` and unpack via the dataclass:
  - `gmail_outcomes.py:861`: `m = match_email_to_job(sender, subject, body, applied_jobs, occurred_at=occurred_at_iso)` — this caller (scan_inbox flow) parses the message date nearby; pass its ISO value; then `matched, method, score = m.astuple()`.
  - `outcome_scan.py:46` (build_email_event): compute `occurred = _occurred_at(msg.get("date", ""))` BEFORE the match call, pass `occurred_at=occurred`, use `m.job/m.method/m.score` and thread `m.status/m.reason` into the returned row dict as `"match_status": m.status if m.job or m.status == "needs_review" else None, "match_reason": m.reason` (persistence lands in Task 3; adding the dict keys now is harmless — `_COLUMNS` gates writes).
Keep existing tests passing WITHOUT rewriting them: they unpack 3-tuples — have them keep working by... they call `match_email_to_job(...)` positionally with no `occurred_at` and unpack 3 values. That breaks with a dataclass return. Update ONLY the unpack lines in the existing tests from `job, method, score = match_email_to_job(...)` to `job, method, score = match_email_to_job(...).astuple()` (mechanical, ~8 lines, listed at :396, :429, :471, :484, :496, :506, :517, :527).

- [ ] **Step 4: Run to verify pass** — `.\.conda-env\python.exe -m pytest tests/test_gmail_outcomes.py -q` → ALL pass (old + new).
- [ ] **Step 5: Commit** — `git add src/applypilot/gmail_outcomes.py src/applypilot/outcome_scan.py tests/test_gmail_outcomes.py && git commit -m "feat(outcomes): temporal guard + MatchResult in email->job matching"`

---

### Task 2: Same-company disambiguation

**Files:**
- Modify: `src/applypilot/gmail_outcomes.py` (inside the fuzzy tiers / `_match_tiers` from Task 1)
- Test: `tests/test_gmail_outcomes.py`

**Interfaces:**
- Consumes: `MatchResult`, `_match_tiers`, `_clean_company` (exists at gmail_outcomes.py:~429), `_token_overlap` (exists — used by `_best_name_match`).
- Produces: fuzzy/employer-level methods (`ats_domain`, `company_name`, `company_domain`) whose matched company has 2+ eligible applied jobs resolve by title-token overlap → method suffixed `+title`; no unique winner → `MatchResult(None, None, None, "needs_review", "ambiguous_company")`. `board_slug` / `linkedin_job_id` skip this check.

- [ ] **Step 1: Write the failing tests** — append to `tests/test_gmail_outcomes.py`:

```python
class TestSameCompanyDisambiguation:
    def _jobs(self):
        return [
            {"url": "https://boards.greenhouse.io/acme/jobs/1", "application_url": "https://boards.greenhouse.io/acme/jobs/1",
             "title": "Data Analyst", "site": "Acme", "company": "Acme",
             "applied_at": "2026-06-28T12:00:00+00:00"},
            {"url": "https://boards.greenhouse.io/acme/jobs/2", "application_url": "https://boards.greenhouse.io/acme/jobs/2",
             "title": "Chief of Staff", "site": "Acme", "company": "Acme",
             "applied_at": "2026-06-28T13:00:00+00:00"},
        ]

    def test_title_in_subject_disambiguates(self):
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io",
            "Your application for Chief of Staff at Acme",
            "Thank you for applying to Acme.",
            self._jobs(), occurred_at="2026-06-29T12:00:00+00:00",
        )
        assert r.status == "attributed"
        assert r.job["url"].endswith("/jobs/2")
        assert r.method.endswith("+title")

    def test_no_title_signal_quarantines(self):
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io",
            "Your application to Acme",
            "Thank you for applying to Acme. We received your application.",
            self._jobs(), occurred_at="2026-06-29T12:00:00+00:00",
        )
        assert r.status == "needs_review" and r.reason == "ambiguous_company"

    def test_single_job_company_unaffected(self):
        r = match_email_to_job(
            "no-reply@us.greenhouse-mail.io",
            "Your application to Acme",
            "Thanks for applying to Acme.",
            self._jobs()[:1], occurred_at="2026-06-29T12:00:00+00:00",
        )
        assert r.status == "attributed" and r.method in ("ats_domain", "company_name")
```

- [ ] **Step 2: Run to verify failure** — the first two fail (today the fuzzy tier arbitrarily returns the best `_token_overlap` job with no `+title` and no quarantine).
- [ ] **Step 3: Implement.** After a fuzzy/employer tier picks `job`: collect `peers = [j for j in eligible if _same_company(j, job)]` where `_same_company` compares case-folded `company` (fall back to `site`). If `len(peers) >= 2`: score each by title-token overlap `_token_overlap(j["title"], subject + " " + body[:600])`; a unique max with score > 0 wins → return with `method + "+title"`; otherwise return the ambiguous quarantine.
- [ ] **Step 4: Run** — `.\.conda-env\python.exe -m pytest tests/test_gmail_outcomes.py -q` → all pass.
- [ ] **Step 5: Commit** — `git add src/applypilot/gmail_outcomes.py tests/test_gmail_outcomes.py && git commit -m "feat(outcomes): same-company title disambiguation + ambiguity quarantine"`

---

### Task 3: Quarantine columns + scan persistence

**Files:**
- Modify: `src/applypilot/database.py` (email_events DDL block ends ~:637 — add migration right after the three `CREATE INDEX` lines)
- Modify: `src/applypilot/outcome_scan.py` (`_COLUMNS` at :71, `build_email_event` at :39, `scan_outcomes` summary at :169)
- Modify: `src/applypilot/cli.py` `outcomes-scan` command (:1670) — print the needs_review summary line
- Test: `tests/test_outcome_scan.py` (existing file; follow its fixture pattern for a temp brain)

**Interfaces:**
- Produces: `email_events` columns `match_status TEXT`, `match_reason TEXT`, `prev_job_url TEXT` (additive, idempotent — mirror the `PRAGMA table_info` + `ALTER TABLE` pattern used for jobs at database.py:318-331):

```python
    ee_existing = {row[1] for row in conn.execute("PRAGMA table_info(email_events)").fetchall()}
    for col in ("match_status", "match_reason", "prev_job_url"):
        if col not in ee_existing:
            conn.execute(f"ALTER TABLE email_events ADD COLUMN {col} TEXT")
```

- Produces: rows written by the scan carry `match_status` = `attributed` (job matched) | `needs_review` (quarantined: `job_url` stays NULL, `match_reason` set) | NULL (plain unmatched); `_COLUMNS` extended with `"match_status", "match_reason"` (NOT `prev_job_url` — that is reaudit-only).
- Produces: `scan_outcomes` counts dict gains `"needs_review": N`; the `outcomes-scan` CLI prints `needs_review: N (predates=X ambiguous=Y no_timestamp=Z)` when N > 0.

- [ ] **Step 1: Failing test** (append to `tests/test_outcome_scan.py`, reusing its existing in-memory-brain fixture style):

```python
def test_scan_persists_quarantine_columns(tmp_path):
    from applypilot import database
    from applypilot.outcome_scan import scan_outcomes
    conn = database.init_db(tmp_path / "brain.db")
    conn.execute(
        "INSERT INTO jobs (url, title, site, apply_status, applied_at) VALUES (?,?,?,?,?)",
        ("https://boards.greenhouse.io/checkr/jobs/1", "Analyst", "Checkr",
         "applied", "2026-06-28T12:00:00+00:00"))
    conn.commit()
    msg = {"message_id": "m1", "thread_id": "t1",
           "subject": "Your application to Checkr",
           "sender": "no-reply@us.greenhouse-mail.io",
           "date": "Sat, 20 Jun 2026 12:00:00 +0000",   # predates the apply
           "body": "Thank you for applying to Checkr."}
    counts = scan_outcomes(conn=conn, fetch_messages=lambda: [msg], client=None, concurrency=1)
    assert counts["needs_review"] == 1
    row = conn.execute("SELECT job_url, match_status, match_reason FROM email_events WHERE message_id='m1'").fetchone()
    assert row["job_url"] is None
    assert row["match_status"] == "needs_review"
    assert row["match_reason"] == "predates_application"
```

(If `extract_outcome` requires an LLM client, follow the existing stub pattern in `tests/test_outcome_scan.py` — it already fakes/none-s the client; mirror exactly what its current tests do.)
- [ ] **Step 2: Verify failure** (no such column match_status / KeyError needs_review).
- [ ] **Step 3: Implement** the migration + `_COLUMNS` extension + `build_email_event` writing the two new keys + `scan_outcomes` counting `needs_review` (increment when `row.get("match_status") == "needs_review"`) + the CLI summary print.
- [ ] **Step 4: Run** — `.\.conda-env\python.exe -m pytest tests/test_outcome_scan.py tests/test_outcome_schema.py -q` → pass.
- [ ] **Step 5: Commit** — `git add src/applypilot/database.py src/applypilot/outcome_scan.py src/applypilot/cli.py tests/test_outcome_scan.py && git commit -m "feat(outcomes): quarantine columns persisted by the scan + summary"`

---

### Task 4: Consumer hardening (reconcile positive-evidence guards)

**Files:**
- Modify: `src/applypilot/fleet/email_reconcile.py` (`load_outcome_emails` :68, `load_crash_jobs` :92, `reconcile` :110)
- Test: `tests/test_email_reconcile.py` (existing patterns)

**Interfaces:**
- Consumes: `MatchResult` (Task 1), `guard_after` job-dict key.
- Produces: reconcile re-matches raw emails against crash jobs itself — it does NOT trust the stored attribution, so do NOT filter `load_outcome_emails` on the stored `job_url`/`match_status='attributed'` (a quarantined-at-scan-time email may legitimately confirm a crash job). The hardening is exactly three changes: (a) `load_outcome_emails` EXCLUDES only rows with `match_status='needs_review' AND match_reason='no_timestamp'` (no reliable timestamp = cannot be temporally validated as evidence); (b) `load_crash_jobs` adds `updated_at` to its SELECT and sets `"guard_after": r["updated_at"].isoformat() if hasattr(r["updated_at"], "isoformat") else r["updated_at"]` on each candidate; (c) `reconcile()` passes `occurred_at=e.occurred_at` to `match_email_to_job` and treats `r.status != "attributed"` as unmatched (`unmatched += 1; continue`). Net effect: an email PREDATING a crash attempt can never confirm it.
- Produces: `remediator.py` UNCHANGED (Global Constraint #7 — document with a one-line comment at remediator.py:65: `# Deliberately reads ALL email_events incl. quarantined: negative evidence stays conservative.`).

- [ ] **Step 1: Failing test** (append to `tests/test_email_reconcile.py`, following its existing fixture style for emails/jobs):

```python
def test_email_predating_crash_attempt_cannot_confirm():
    from applypilot.fleet.email_reconcile import reconcile, OutcomeEmail
    emails = [OutcomeEmail(
        message_id="m1", sender="no-reply@us.greenhouse-mail.io",
        subject="Your application to Acme",
        body="Thank you for applying to Acme. See https://boards.greenhouse.io/acme/jobs/1",
        company="Acme", title="Analyst", job_url=None, stage="applied_confirmation",
        occurred_at="2026-06-01T00:00:00+00:00",           # BEFORE the crash attempt
    )]
    jobs = [{"url": "https://boards.greenhouse.io/acme/jobs/1",
             "application_url": "https://boards.greenhouse.io/acme/jobs/1",
             "company": "Acme", "title": "Analyst", "site": "boards.greenhouse.io",
             "dedup_key": "k1", "guard_after": "2026-06-29T12:00:00+00:00"}]
    result = reconcile(emails, jobs)
    assert result.confirmed == [] and result.probable == []
    assert result.unmatched_emails == 1
```

- [ ] **Step 2: Verify failure** (today it confirms via board_slug).
- [ ] **Step 3: Implement** (a)/(b)/(c) above + the remediator comment.
- [ ] **Step 4: Run** — `.\.conda-env\python.exe -m pytest tests/test_email_reconcile.py -q` → pass.
- [ ] **Step 5: Commit** — `git add src/applypilot/fleet/email_reconcile.py src/applypilot/fleet/remediator.py tests/test_email_reconcile.py && git commit -m "fix(reconcile): temporal guard on crash-confirmation evidence"`

---

### Task 5: `outcomes-scan --reaudit`

**Files:**
- Create: `src/applypilot/outcome_reaudit.py`
- Modify: `src/applypilot/cli.py` `outcomes-scan` command (:1670) — add `--reaudit` flag that calls it and prints the report instead of scanning
- Test: `tests/test_outcome_reaudit.py` (new)

**Interfaces:**
- Consumes: `match_email_to_job` (Task 1 signature), `get_applied_jobs`.
- Produces: `reaudit_email_events(conn) -> dict` in `outcome_reaudit.py`:
  - For every `email_events` row with `job_url IS NOT NULL`: re-run the temporal guard against the CURRENT jobs table (`SELECT applied_at FROM jobs WHERE url = row.job_url`) and the ambiguity check (recompute via `match_email_to_job(row.sender, row.subject, row.body_text or '', applied_jobs, occurred_at=row.occurred_at)` — a result whose status is `needs_review` OR whose matched url differs from the stored `job_url` means the stored attribution fails today's guards).
  - Failing row: `UPDATE email_events SET prev_job_url = job_url, job_url = NULL, match_status='needs_review', match_reason=? WHERE message_id=?`.
  - Passing row: `UPDATE email_events SET match_status='attributed' WHERE message_id=? AND match_status IS NULL` (legacy backfill).
  - Returns `{"checked": N, "flipped": {"predates_application": X, "ambiguous_company": Y, "no_timestamp": Z}, "backfilled": B, "flipped_ids": [...]}` and is idempotent (second run flips 0).
  - Never touches rows already `needs_review`; never deletes; reversal documented in the report footer: `UPDATE email_events SET job_url = prev_job_url, match_status='attributed', match_reason=NULL WHERE message_id = '<id>'`.

- [ ] **Step 1: Failing tests** — `tests/test_outcome_reaudit.py`:

```python
from __future__ import annotations


def _brain(tmp_path):
    from applypilot import database
    conn = database.init_db(tmp_path / "brain.db")
    conn.execute("INSERT INTO jobs (url, title, site, company, apply_status, applied_at) VALUES (?,?,?,?,?,?)",
                 ("https://boards.greenhouse.io/checkr/jobs/1", "Analyst", "Checkr", "Checkr",
                  "applied", "2026-06-28T12:00:00+00:00"))
    conn.commit()
    return conn


def _event(conn, message_id, job_url, occurred_at, subject="Your application to Checkr"):
    conn.execute(
        "INSERT INTO email_events (message_id, job_url, occurred_at, sender, subject, stage, body_text, scanned_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (message_id, job_url, occurred_at, "no-reply@us.greenhouse-mail.io", subject,
         "applied_confirmation", "Thank you for applying to Checkr.", "2026-07-01T00:00:00+00:00"))
    conn.commit()


def test_reaudit_flips_predating_attribution_and_is_idempotent(tmp_path):
    from applypilot.outcome_reaudit import reaudit_email_events
    conn = _brain(tmp_path)
    _event(conn, "bad1", "https://boards.greenhouse.io/checkr/jobs/1", "2026-06-20T12:00:00+00:00")
    _event(conn, "good1", "https://boards.greenhouse.io/checkr/jobs/1", "2026-06-29T12:00:00+00:00")

    r = reaudit_email_events(conn)
    assert r["flipped"]["predates_application"] == 1 and "bad1" in r["flipped_ids"]
    assert r["backfilled"] >= 1
    bad = conn.execute("SELECT job_url, prev_job_url, match_status, match_reason FROM email_events WHERE message_id='bad1'").fetchone()
    assert bad["job_url"] is None and bad["prev_job_url"] == "https://boards.greenhouse.io/checkr/jobs/1"
    assert bad["match_status"] == "needs_review" and bad["match_reason"] == "predates_application"
    good = conn.execute("SELECT match_status FROM email_events WHERE message_id='good1'").fetchone()
    assert good["match_status"] == "attributed"

    r2 = reaudit_email_events(conn)
    assert sum(r2["flipped"].values()) == 0   # idempotent


def test_reaudit_empty_brain_ok(tmp_path):
    from applypilot.outcome_reaudit import reaudit_email_events
    conn = _brain(tmp_path)
    r = reaudit_email_events(conn)
    assert r["checked"] == 0
```

- [ ] **Step 2: Verify failure** (module doesn't exist).
- [ ] **Step 3: Implement** `outcome_reaudit.py` per the interface + wire `--reaudit: bool = typer.Option(False, "--reaudit", help="Re-run match guards over stored email_events (no Gmail calls); reversible via prev_job_url.")` into `outcomes-scan`.
- [ ] **Step 4: Run** — `.\.conda-env\python.exe -m pytest tests/test_outcome_reaudit.py tests/test_outcomes_cli.py -q` → pass.
- [ ] **Step 5: Commit** — `git add src/applypilot/outcome_reaudit.py src/applypilot/cli.py tests/test_outcome_reaudit.py && git commit -m "feat(outcomes): --reaudit replays match guards over stored events (reversible)"`

---

### Task 6: Daily scan task + docs

**Files:**
- Modify: `register-fleet-tasks.ps1` (add a home-machine task using the existing `Register-FleetTask` + wrapper pattern at :204-:240; add the task name to `Get-TaskNamesForMachine` for home at :119)
- Modify: `docs/superpowers/specs/2026-07-03-outcome-loop-integrity-design.md` (append the two SPEC AMENDMENT notes from Global Constraints #6/#7)

**Interfaces:**
- Produces: scheduled task `ApplyPilot OutcomesScan` (home only): daily 07:00, wrapper runs `run-applypilot.ps1 outcomes-scan` (NOT `scan-gmail` — `outcomes-scan` is the email_events writer) with `-StartWhenAvailable`.

- [ ] **Step 1: Implement** the task block (mirror the FleetAgent wrapper-generation pattern exactly; trigger `New-ScheduledTaskTrigger -Daily -At 7:00am`, settings `New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Hours 2)`), gated to `$Machine -eq "home"`.
- [ ] **Step 2: Validate** — `powershell -NoProfile -Command "[System.Management.Automation.Language.Parser]::ParseFile('register-fleet-tasks.ps1', [ref]$null, [ref]$e) | Out-Null; if ($e.Count) { $e } else { 'PARSE OK' }"` → PARSE OK. (Do NOT register from the sandbox — the owner runs the script.)
- [ ] **Step 3: Append the spec amendments** (verbatim from Global Constraints #6 and #7) under a `## Amendments (2026-07-03 planning)` heading in the spec.
- [ ] **Step 4: Commit** — `git add register-fleet-tasks.ps1 docs/superpowers/specs/2026-07-03-outcome-loop-integrity-design.md && git commit -m "feat(outcomes): daily outcomes-scan task (home) + spec amendments"`

---

### Task 7: Full regression + live dry verification

- [ ] **Step 1:** `.\.conda-env\python.exe -m pytest tests/test_gmail_outcomes.py tests/test_outcome_scan.py tests/test_email_reconcile.py tests/test_outcome_reaudit.py tests/test_outcomes_cli.py tests/test_outcome_schema.py -q` → all pass.
- [ ] **Step 2:** Read-only sanity vs the LIVE brain (mode=ro): count events that WOULD flip — `SELECT COUNT(*) FROM email_events e JOIN jobs j ON j.url = e.job_url WHERE e.occurred_at < j.applied_at` — expect ≈2 (the audit's known-bad rows). Report the number; do NOT run the real `--reaudit` (owner runs it in his env; my sandbox writes to AppData go to an overlay).
- [ ] **Step 3:** Report: tests, the live would-flip count, and the owner runbook line: `.\run-applypilot.ps1 outcomes-scan --reaudit` then `.\register-fleet-tasks.ps1` (re-run to pick up the new task).
