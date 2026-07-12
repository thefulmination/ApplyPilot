# Apply Cost Reduction Phase 2B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a durable submit boundary and independent positive-evidence verifier so Greenhouse can move from paid agent submission to a fail-closed deterministic canary.

**Architecture:** The owner migrates an append-only `apply_attempts` ledger. The Greenhouse adapter creates a prepared attempt, checkpoints immediately before its only submit action, and delegates outcome classification to a pure verifier. Queue closure continues through the existing worker result path, with attempt and verification evidence carried in result metadata and unresolved post-click outcomes mapped to `crash_unconfirmed`.

**Tech Stack:** Python 3.11+, Playwright, psycopg 3, PostgreSQL, pytest, existing Greenhouse adapter, launcher metadata, and fleet-v3 owner migration.

---

## Scope

This implements rollout steps 4-6 from `docs/superpowers/specs/2026-07-09-apply-cost-reduction-phase2-design.md` for Greenhouse only. Ashby, low-cost agent fallback, email verification, and automatic route promotion remain later slices. Adapter submit stays opt-in through the existing two environment gates.

## File Structure

- Create `src/applypilot/fleet/apply_attempts.py`: validated attempt creation and state transitions.
- Create `src/applypilot/apply/submission_verifier.py`: pure evidence normalization and precedence.
- Modify `src/applypilot/fleet/schema_v3.sql`: owner-migrated attempt table and unresolved-submit uniqueness.
- Modify `src/applypilot/fleet/schema.py`: worker compatibility requirements for the new ledger.
- Modify `src/applypilot/apply/greenhouse_submit.py`: checkpoint callbacks and verifier integration.
- Modify `src/applypilot/apply/launcher.py`: create/checkpoint/finalize attempts and publish route metadata.
- Modify `src/applypilot/fleet/apply_worker_main.py`: inject a narrowly scoped attempt store into launcher execution.
- Modify `src/applypilot/fleet/queue.py`: preserve structured attempt/verifier metadata in result events.
- Add focused tests under `tests/test_apply_attempts.py`, `tests/test_submission_verifier.py`, and existing Greenhouse/launcher/worker/schema test files.

---

### Task 1: Durable Attempt Ledger

**Files:**
- Create: `src/applypilot/fleet/apply_attempts.py`
- Modify: `src/applypilot/fleet/schema_v3.sql`
- Modify: `src/applypilot/fleet/schema.py`
- Test: `tests/test_apply_attempts.py`
- Test: `tests/test_fleet_v3_schema.py`

- [ ] **Step 1: Write failing schema and transition tests**

Cover idempotent owner migration, the columns and partial unique index from the approved design, `create_prepared`, legal transitions, rejection of illegal transitions, and prevention of a second unresolved submit for one non-null dedup key.

- [ ] **Step 2: Run the failing tests**

Run:

```powershell
..\..\.conda-env\python.exe -m pytest tests/test_apply_attempts.py tests/test_fleet_v3_schema.py -q
```

Expected: failure because the table/module does not exist.

- [ ] **Step 3: Add the owner schema**

Add `apply_attempts` with the exact states `prepared`, `submit_started`, `submitted_unverified`, `verified`, `contradicted`, `quarantined`, and `failed_pre_submit`; JSONB evidence defaults to `{}`. Add a partial unique index over `dedup_key` where state is `submit_started` or `submitted_unverified`.

- [ ] **Step 4: Implement validated transitions**

Expose:

```python
def create_prepared(conn, *, queue_name: str, url: str, dedup_key: str | None,
                    worker_id: str, route: str, route_version: str | None,
                    evidence: dict | None = None) -> str: ...

def transition(conn, attempt_id: str, *, expected: str, state: str,
               verification_method: str | None = None,
               verification_ref: str | None = None,
               evidence: dict | None = None) -> dict: ...
```

Use compare-and-swap SQL (`WHERE attempt_id=%s AND state=%s`), merge evidence with JSONB concatenation, and commit only after one row is returned. Invalid state edges raise `ValueError`; stale expected state raises `AttemptTransitionError`.

- [ ] **Step 5: Run focused tests and commit**

Run the command from Step 2 and expect all tests to pass.

Commit:

```powershell
git add src/applypilot/fleet/apply_attempts.py src/applypilot/fleet/schema_v3.sql src/applypilot/fleet/schema.py tests/test_apply_attempts.py tests/test_fleet_v3_schema.py
git commit -m "feat(fleet): add durable apply attempt ledger"
```

### Task 2: Independent Submission Verifier

**Files:**
- Create: `src/applypilot/apply/submission_verifier.py`
- Test: `tests/test_submission_verifier.py`

- [ ] **Step 1: Write failing precedence tests**

Cover verified allowlisted response identifiers, known success URL plus success state, allowlisted confirmation DOM, matched confirmation email, contradicted validation errors, and unverified screenshot/button-only evidence.

- [ ] **Step 2: Run the failing tests**

```powershell
..\..\.conda-env\python.exe -m pytest tests/test_submission_verifier.py -q
```

- [ ] **Step 3: Implement the pure verifier**

Define immutable evidence/result records and:

```python
def verify_submission(evidence: SubmissionEvidence) -> VerificationResult: ...
```

Return only `verified`, `unverified`, or `contradicted`. Positive methods are `response_id`, `success_url_dom`, `confirmation_dom`, and `confirmation_email`; screenshot and disabled-button signals can only annotate an unverified result. Normalize text case/whitespace and match only explicit allowlisted markers.

- [ ] **Step 4: Run tests and commit**

```powershell
..\..\.conda-env\python.exe -m pytest tests/test_submission_verifier.py -q
git add src/applypilot/apply/submission_verifier.py tests/test_submission_verifier.py
git commit -m "feat(apply): add independent submission verifier"
```

### Task 3: Greenhouse Exactly-Once Integration

**Files:**
- Modify: `src/applypilot/apply/greenhouse_submit.py`
- Modify: `src/applypilot/apply/launcher.py`
- Test: `tests/test_greenhouse_submit.py`
- Test: `tests/test_apply_launcher.py`

- [ ] **Step 1: Write failing checkpoint and fallback tests**

Assert that an incomplete plan creates no attempt and falls through; a ready plan creates `prepared`; the callback changes it to `submit_started` immediately before one click; verified evidence returns `applied`; missing evidence after the click returns `crash_unconfirmed`; and no agent fallback occurs after `submit_started`.

- [ ] **Step 2: Run the failing tests**

```powershell
..\..\.conda-env\python.exe -m pytest tests/test_greenhouse_submit.py tests/test_apply_launcher.py -q
```

- [ ] **Step 3: Add checkpoint hooks without queue coupling**

Extend `execute_form`/`apply_greenhouse` with injected `before_submit` and `verify` callables. `before_submit` executes after all fields are filled and before `page.click('#submit_app')`. It must succeed before the click; an exception returns a pre-submit failure and leaves the page unsubmitted.

- [ ] **Step 4: Wire the launcher attempt store**

For adapter-owned runs, create the prepared attempt only after a complete plan. Transition to `submit_started` in the callback. Finalize `verified` only from the verifier; finalize `contradicted` for explicit validation errors; finalize `quarantined` and return `crash_unconfirmed` for unverified post-click evidence. Publish `attempt_id`, checkpoint state, verification method/ref, adapter version, and normalized evidence in `_last_run_stats`.

- [ ] **Step 5: Run tests and commit**

```powershell
..\..\.conda-env\python.exe -m pytest tests/test_greenhouse_submit.py tests/test_apply_launcher.py -q
git add src/applypilot/apply/greenhouse_submit.py src/applypilot/apply/launcher.py tests/test_greenhouse_submit.py tests/test_apply_launcher.py
git commit -m "feat(apply): checkpoint Greenhouse adapter submits"
```

### Task 4: Worker Wiring and Telemetry

**Files:**
- Modify: `src/applypilot/fleet/apply_worker_main.py`
- Modify: `src/applypilot/fleet/queue.py`
- Modify: `src/applypilot/fleet/worker.py`
- Test: `tests/test_apply_worker_main.py`
- Test: `tests/test_fleet_v3_worker.py`
- Test: `tests/test_fleet_apply_lane.py`

- [ ] **Step 1: Write failing integration tests**

Assert that production passes the open fleet connection, queue/dedup identity, and worker id to the attempt store; result events retain attempt/verifier metadata; verified adapter results close `applied`; quarantined adapter results close `crash_unconfirmed`; and parked pre-submit challenges record route and accrued cost without pretending to be terminal.

- [ ] **Step 2: Run the failing tests**

```powershell
..\..\.conda-env\python.exe -m pytest tests/test_apply_worker_main.py tests/test_fleet_v3_worker.py tests/test_fleet_apply_lane.py -q
```

- [ ] **Step 3: Implement narrow dependency injection and metadata persistence**

Do not let adapter code open a second database connection. Build an attempt-store facade at the fleet boundary, pass it through the apply callable, and copy structured metadata into `apply_result_events.result_metadata`. For challenge parking, append a nonterminal `status='challenge_pending'` event with route, model cost, browser cost, and tool counts; do not alter the canonical queue terminal denominator.

- [ ] **Step 4: Run tests and commit**

```powershell
..\..\.conda-env\python.exe -m pytest tests/test_apply_worker_main.py tests/test_fleet_v3_worker.py tests/test_fleet_apply_lane.py -q
git add src/applypilot/fleet/apply_worker_main.py src/applypilot/fleet/queue.py src/applypilot/fleet/worker.py tests/test_apply_worker_main.py tests/test_fleet_v3_worker.py tests/test_fleet_apply_lane.py
git commit -m "feat(fleet): persist adapter attempt telemetry"
```

### Task 5: Regression, Migration, and Disabled-by-Default Proof

**Files:**
- Modify only files required by failures found in this task.

- [ ] **Step 1: Run focused Phase 2B tests**

```powershell
..\..\.conda-env\python.exe -m pytest tests/test_apply_attempts.py tests/test_submission_verifier.py tests/test_greenhouse_submit.py tests/test_apply_launcher.py tests/test_apply_worker_main.py tests/test_fleet_v3_worker.py tests/test_fleet_apply_lane.py tests/test_fleet_v3_schema.py -q
```

- [ ] **Step 2: Prove submit remains disabled by default**

Run the Greenhouse gate tests with both environment variables absent and assert the adapter does not own a real submit. Then run shadow-only tests with `APPLYPILOT_GREENHOUSE_ADAPTER=1` and submit disabled.

- [ ] **Step 3: Run lint and full suite**

```powershell
..\..\.conda-env\python.exe -m ruff check src/applypilot/fleet/apply_attempts.py src/applypilot/apply/submission_verifier.py src/applypilot/apply/greenhouse_submit.py src/applypilot/apply/launcher.py src/applypilot/fleet/apply_worker_main.py src/applypilot/fleet/queue.py src/applypilot/fleet/worker.py
..\..\.conda-env\python.exe -m pytest -q
git diff --check
```

- [ ] **Step 4: Owner migration and shadow rollout**

With ATS and global gates paused, run `applypilot-fleet-apply-home status` from the owner checkout so `ensure_schema_v3` creates the ledger. Verify table/index existence, deploy the pinned build, and enable only `APPLYPILOT_GREENHOUSE_ADAPTER=1`; keep `APPLYPILOT_GREENHOUSE_ADAPTER_SUBMIT` unset.

- [ ] **Step 5: Shadow acceptance**

Collect at least 20 complete Greenhouse inventories. Require zero submits, zero duplicate/ambiguous state, and route metadata on every shadow-ready form before a separate five-submit canary is armed.
