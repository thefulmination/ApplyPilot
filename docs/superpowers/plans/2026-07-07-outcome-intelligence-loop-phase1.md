# Outcome Intelligence Loop Phase 1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the trust backbone for the outcome monitor: durable review records, effective-state resolution, and a review CLI/export/dashboard path that stops treating raw `email_events` as truth.

**Architecture:** Keep `email_events` as the single scan-writer evidence table. Add a new append-only `email_event_reviews` table plus pure assembler functions that derive an effective event/application view from the latest review state. Wire CLI, dashboard, and exports to those effective views without changing the scan path.

**Tech Stack:** Python 3.11, Typer, Rich, sqlite3, pytest, existing ApplyPilot outcome modules.

---

## File Structure

- `src/applypilot/database.py`
  Add the `email_event_reviews` table and indexes in the existing outcome-table migration path.
- `src/applypilot/outcome_review.py`
  New pure/data-access helpers for review writes, effective-event assembly, and review queue queries.
- `src/applypilot/outcome_dashboard.py`
  Switch application rows to use effective events and expose trust/action metadata.
- `src/applypilot/outcome_export.py`
  Export raw events plus effective timelines with explicit trust-state fields.
- `src/applypilot/cli.py`
  Add `outcomes-review queue` and `outcomes-review resolve`.
- `tests/test_outcome_review.py`
  New direct tests for review storage and effective-state logic.
- `tests/test_outcome_dashboard.py`
  Extend existing tests to prove corrected/ignored events change the displayed timeline.
- `tests/test_outcome_export.py`
  Extend export tests to prove trust-state fields are present and ignored events are excluded from effective timelines.
- `tests/test_outcomes_review_cli.py`
  New CLI tests for queue and resolve commands.

---

### Task 1: Add durable review storage

**Files:**
- Modify: `src/applypilot/database.py`
- Test: `tests/test_outcome_review.py`

- [ ] Write a failing test that initializes a temp DB, runs `database.init_db()`, and asserts that `email_event_reviews` exists with the expected columns.
- [ ] Run: `.\.conda-env\python.exe -m pytest tests/test_outcome_review.py::test_init_db_creates_email_event_reviews -v`
- [ ] Add `CREATE TABLE IF NOT EXISTS email_event_reviews (...)` plus indexes in `ensure_outcome_tables()`.
- [ ] Re-run the same test and confirm it passes.

### Task 2: Implement review writes and effective-state resolution

**Files:**
- Create: `src/applypilot/outcome_review.py`
- Test: `tests/test_outcome_review.py`

- [ ] Write failing tests for:
  - latest review wins
  - `ignore` removes an event from effective timelines
  - `reassign_job` moves an event to another job in the effective view
  - `change_stage` / `change_outcome` override raw values
  - unresolved queue includes raw `match_status='needs_review'`
- [ ] Run targeted pytest for those tests and confirm failure.
- [ ] Implement:
  - `record_review(...)`
  - `list_reviews(...)`
  - `build_effective_events(conn)`
  - `build_effective_events_for_job(conn, job_url)`
  - `list_review_queue(conn)`
- [ ] Re-run targeted pytest and confirm pass.

### Task 3: Wire dashboard rows to effective events

**Files:**
- Modify: `src/applypilot/outcome_dashboard.py`
- Test: `tests/test_outcome_dashboard.py`

- [ ] Write failing tests proving that an ignored/corrected event changes `current_stage`, `outcome`, and visible timeline rows.
- [ ] Run the targeted dashboard tests and confirm failure.
- [ ] Change dashboard assembly to consume effective events and expose:
  - `trust_state`
  - `needs_action`
  - `effective_events`
- [ ] Re-run targeted dashboard tests and confirm pass.

### Task 4: Wire exports to effective state

**Files:**
- Modify: `src/applypilot/outcome_export.py`
- Test: `tests/test_outcome_export.py`

- [ ] Write failing tests proving the timeline export includes trust-state metadata and excludes ignored events from effective application timelines.
- [ ] Run targeted export tests and confirm failure.
- [ ] Update export assembly to include:
  - raw events unchanged
  - effective timeline rows with trust/action metadata
  - trusted/untrusted summary counts
- [ ] Re-run targeted export tests and confirm pass.

### Task 5: Add review CLI

**Files:**
- Modify: `src/applypilot/cli.py`
- Test: `tests/test_outcomes_review_cli.py`

- [ ] Write failing CLI tests for:
  - `applypilot outcomes-review queue`
  - `applypilot outcomes-review resolve --message-id ... --resolution trusted`
  - `applypilot outcomes-review resolve --message-id ... --resolution ignored`
- [ ] Run targeted CLI tests and confirm failure.
- [ ] Implement the `outcomes-review` Typer sub-app with `queue` and `resolve` commands.
- [ ] Re-run targeted CLI tests and confirm pass.

### Task 6: Verify the whole slice

**Files:**
- Test: `tests/test_outcome_review.py`
- Test: `tests/test_outcome_dashboard.py`
- Test: `tests/test_outcome_export.py`
- Test: `tests/test_outcomes_review_cli.py`

- [ ] Run:
  `.\.conda-env\python.exe -m pytest tests/test_outcome_review.py tests/test_outcome_dashboard.py tests/test_outcome_export.py tests/test_outcomes_review_cli.py -v`
- [ ] Confirm all tests pass and note any gaps.
- [ ] Commit only the phase-1 implementation files.

---

## Self-Review

- Spec coverage:
  This plan covers Phase 1 from the approved design: review storage, effective-state assembly, CLI review queue, and trust-aware dashboard/export consumers.
- Placeholder scan:
  No TODO/TBD placeholders remain.
- Type consistency:
  The plan consistently treats `email_events` as raw evidence and `outcome_review.py` as the effective-state resolver consumed by CLI/dashboard/export code.
