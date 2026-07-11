# Canonical Decision Runtime Authority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make immutable canonical decisions the only source that can enter or lease from ApplyPilot's ATS and LinkedIn queues.

**Architecture:** Python owns the additive SQLite/Postgres schema, policy activation, queue projection, and fail-closed enforcement. TypeScript writes draft policies and immutable decisions to SQLite; Python validates and promotes them, projects complete provenance to Postgres, and rejects every legacy score-only path.

**Tech Stack:** Python 3.11+, SQLite, PostgreSQL/psycopg 3, Typer, pytest, Ruff.

---

### Task 1: Add the canonical SQLite schema

**Files:**
- Modify: `src/applypilot/database.py`
- Create: `tests/test_canonical_decision_schema.py`

- [ ] **Step 1: Write failing schema tests**

Create a temporary brain with `init_db(path)` and assert:

```python
def test_init_db_creates_canonical_decision_schema(tmp_path):
    conn = database.init_db(tmp_path / "brain.db")
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert {"decision_policy_versions", "job_decisions", "reviewed_outcomes"} <= tables
    job_cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)")}
    assert {
        "canonical_decision_id", "canonical_policy_version", "canonical_action",
        "canonical_score", "canonical_decided_at",
    } <= job_cols


def test_policy_and_decision_constraints_reject_invalid_values(tmp_path):
    conn = database.init_db(tmp_path / "brain.db")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO decision_policy_versions(policy_version,status,created_at) VALUES('p','wrong','t')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO job_decisions(decision_id,job_url,policy_version,lane,qualification_verdict,action,created_at,input_hash) VALUES('d','u','p','ats','maybe','apply','t','h')")
```

- [ ] **Step 2: Run tests and confirm failure**

Run: `python -m pytest tests/test_canonical_decision_schema.py -q`

Expected: failure because the canonical tables and job projection columns do not exist.

- [ ] **Step 3: Implement additive schema**

Add the five projection columns to `_ALL_COLUMNS`, call `ensure_canonical_decision_tables(conn)` from `init_db()`, and create the tables exactly as the approved design specifies. Required constraints:

```python
CHECK(status IN ('draft','validated','canary','active','retired'))
CHECK(lane IN ('ats','linkedin'))
CHECK(qualification_verdict IN ('qualified','unqualified','uncertain'))
CHECK(action IN ('apply','review','reject'))
CHECK(review_status IN ('accepted','rejected','needs_review'))
UNIQUE(job_url, policy_version, input_hash)
```

Add indexes for policy status/lane, decision job/policy/action/expiry, and reviewed outcome job/status. Do not add destructive migrations or defaults that make a job apply-eligible.

- [ ] **Step 4: Run schema tests**

Run: `python -m pytest tests/test_canonical_decision_schema.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/applypilot/database.py tests/test_canonical_decision_schema.py
git commit -m "feat(brain): add canonical decision schema"
```

### Task 2: Implement the canonical decision repository

**Files:**
- Create: `src/applypilot/canonical_decisions.py`
- Create: `tests/test_canonical_decisions.py`

- [ ] **Step 1: Write failing repository tests**

Cover immutable insertion, idempotent same-input insertion, conflict rejection, projection, validation, activation, retirement, and one-active-policy-per-lane:

```python
repo.create_draft_policy(conn, policy)
repo.insert_decisions(conn, [decision])
assert repo.get_decision(conn, "d1")["action"] == "apply"
assert conn.execute("SELECT canonical_decision_id FROM jobs WHERE url='u1'").fetchone()[0] == "d1"
with pytest.raises(repo.ImmutableDecisionConflict):
    repo.insert_decisions(conn, [{**decision, "final_score": 2.0}])
repo.record_replay_metrics(conn, "p1", {"hard_negative_false_positives": 0})
repo.validate_policy(conn, "p1")
repo.activate_policy(conn, "p1", lane="ats")
```

- [ ] **Step 2: Confirm tests fail**

Run: `python -m pytest tests/test_canonical_decisions.py -q`

Expected: import failure for `applypilot.canonical_decisions`.

- [ ] **Step 3: Implement repository contracts**

Expose typed functions:

```python
create_draft_policy(conn, row) -> None
insert_decisions(conn, rows) -> int
get_decision(conn, decision_id) -> dict | None
record_replay_metrics(conn, policy_version, metrics) -> None
validate_policy(conn, policy_version) -> None
activate_policy(conn, policy_version, *, lane) -> None
retire_policy(conn, policy_version) -> None
eligible_decision(conn, job_url, *, lane, now=None) -> dict | None
```

Use `BEGIN IMMEDIATE` transactions. `insert_decisions()` may no-op only when the complete persisted row equals the proposed row; otherwise raise `ImmutableDecisionConflict`. Project to `jobs.canonical_*` only after insertion succeeds. `activate_policy()` must reject missing replay metrics, nonzero locked hard-negative false positives, or a policy not in `validated|canary`.

- [ ] **Step 4: Run repository and schema tests**

Run: `python -m pytest tests/test_canonical_decisions.py tests/test_canonical_decision_schema.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/applypilot/canonical_decisions.py tests/test_canonical_decisions.py
git commit -m "feat(brain): enforce immutable canonical decisions"
```

### Task 3: Add reviewed outcomes and deterministic backfill

**Files:**
- Create: `src/applypilot/canonical_backfill.py`
- Create: `tests/test_canonical_backfill.py`
- Modify: `src/applypilot/outcome_review.py`
- Modify: `tests/test_outcome_review.py`

- [ ] **Step 1: Write failing outcome and backfill tests**

Assert that unrelated recommendation mail is rejected before matching, unreviewed rows never appear in model input, accepted rows preserve attribution evidence, and rerunning an import yields identical counts/hashes:

```python
assert classify_review_candidate(sender="Indeed <donotreply@match.indeed.com>", subject="5 new jobs") == "rejected"
report1 = backfill_research_artifacts(conn, fixture_dir)
report2 = backfill_research_artifacts(conn, fixture_dir)
assert report1 == report2
assert report1["pairwise"]["written"] == 2
assert report1["pairwise"]["sha256"] == report2["pairwise"]["sha256"]
```

- [ ] **Step 2: Confirm failure**

Run: `python -m pytest tests/test_canonical_backfill.py tests/test_outcome_review.py -q`

Expected: missing backfill module and recommendation-mail rejection behavior.

- [ ] **Step 3: Implement safe backfill and review projection**

Implement parsers for research score JSONL, label JSONL, pairwise JSONL, KG artifacts, and reviewed email events. Each parser must canonicalize JSON with sorted keys, calculate SHA-256, upsert by existing primary key, and report `read/written/skipped/hash`. Never infer `accepted`; imported raw events default to `needs_review` unless an explicit existing review says otherwise.

Change outcome trust so `job_url` presence alone is insufficient. Reject known recommendation/newsletter senders and subjects before company/title matching.

- [ ] **Step 4: Run focused tests**

Run: `python -m pytest tests/test_canonical_backfill.py tests/test_outcome_review.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/applypilot/canonical_backfill.py src/applypilot/outcome_review.py tests/test_canonical_backfill.py tests/test_outcome_review.py
git commit -m "feat(outcomes): require reviewed canonical feedback"
```

### Task 4: Add Postgres policy and queue provenance schema

**Files:**
- Modify: `src/applypilot/apply/fleet_schema.sql`
- Modify: `src/applypilot/fleet/schema_v3.sql`
- Modify: `tests/conftest.py`
- Modify: `tests/test_fleet_v3_schema.py`

- [ ] **Step 1: Write failing schema assertions**

Require both queues to contain:

```python
required = {
    "decision_id", "policy_version", "decision_action", "qualification_verdict",
    "qualification_score", "qualification_floor",
    "preference_score", "outcome_score", "decision_confidence",
    "decision_created_at", "decision_expires_at",
}
```

Require `fleet_config.ats_policy_version` and `fleet_config.linkedin_policy_version`. Add a schema test proving existing `linkedin_queue` receives columns through explicit `ALTER TABLE`, not only `LIKE apply_queue` creation.

- [ ] **Step 2: Confirm failure**

Run: `python -m pytest tests/test_fleet_v3_schema.py -q`

Expected: missing provenance and policy columns.

- [ ] **Step 3: Implement additive PG migrations**

Add columns to base `apply_queue`, then explicit `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements for `apply_queue`, `linkedin_queue`, and `fleet_config` in `schema_v3.sql`. Do not populate policy versions automatically. Reset the two policy fields in `fleet_db` fixture setup.

- [ ] **Step 4: Run schema tests**

Run: `python -m pytest tests/test_fleet_v3_schema.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/applypilot/apply/fleet_schema.sql src/applypilot/fleet/schema_v3.sql tests/conftest.py tests/test_fleet_v3_schema.py
git commit -m "feat(fleet): add canonical queue provenance schema"
```

### Task 5: Replace score fallback selectors with canonical projection

**Files:**
- Modify: `src/applypilot/fleet/sync.py`
- Modify: `tests/test_fleet_v3_sync.py`
- Modify: `tests/test_fleet_linkedin_push.py`

- [ ] **Step 1: Replace legacy tests with failing canonical-selection tests**

Seed jobs with audit/fit/research scores but no canonical decision and assert zero pushes. Seed an active, unexpired `apply` decision and assert one complete row. Assert `review`, `reject`, stale, policy-mismatch, and unqualified rows are skipped. Add distinct ATS and LinkedIn policy cases.

- [ ] **Step 2: Confirm failure**

Run: `python -m pytest tests/test_fleet_v3_sync.py tests/test_fleet_linkedin_push.py -q`

Expected: legacy score-only rows are still pushed.

- [ ] **Step 3: Implement canonical selectors**

Replace both SQLite selectors with joins equivalent to:

```sql
FROM jobs j
JOIN job_decisions d ON d.decision_id = j.canonical_decision_id
JOIN decision_policy_versions p ON p.policy_version = d.policy_version
WHERE d.action='apply'
  AND d.qualification_verdict='qualified'
  AND p.status IN ('canary','active')
  AND p.lane=d.lane
  AND (d.expires_at IS NULL OR d.expires_at > :now)
```

Return all queue provenance fields. Remove `include_research` from `push_apply_eligible()` and callers. Do not read `audit_score`, `fit_score`, or `research_fit_score` for authorization.

- [ ] **Step 4: Run selector tests**

Run: `python -m pytest tests/test_fleet_v3_sync.py tests/test_fleet_linkedin_push.py -q`

Expected: all canonical cases pass and score-only rows remain absent.

- [ ] **Step 5: Commit**

```powershell
git add src/applypilot/fleet/sync.py tests/test_fleet_v3_sync.py tests/test_fleet_linkedin_push.py
git commit -m "feat(fleet): push only canonical apply decisions"
```

### Task 6: Make queue writes and leases fail closed

**Files:**
- Modify: `src/applypilot/fleet/queue.py`
- Modify: `src/applypilot/apply/pgqueue.py`
- Modify: `src/applypilot/apply/container_worker.py`
- Modify: `src/applypilot/apply/fleet_sync.py`
- Modify: `tests/test_fleet_apply_lane.py`
- Modify: `tests/test_fleet_linkedin_lane.py`
- Modify: `tests/test_fleet_pgqueue.py`
- Modify: `tests/test_fleet_v3_governor_queue.py`

- [ ] **Step 1: Write failing push/lease/bypass tests**

Assert `push_apply_jobs()` and `push_linkedin_jobs()` raise `ValueError` for missing provenance. Assert lease returns `None` for missing decision ID, wrong active lane policy, action other than apply, expired decision, or qualification below the configured floor. Assert legacy `lease_one()` and `push_jobs()` refuse score-only operations.

- [ ] **Step 2: Confirm failure**

Run: `python -m pytest tests/test_fleet_apply_lane.py tests/test_fleet_linkedin_lane.py tests/test_fleet_pgqueue.py tests/test_fleet_v3_governor_queue.py -q`

Expected: legacy rows remain leasable.

- [ ] **Step 3: Implement validation and lease predicates**

Validate each row before SQL:

```python
CANONICAL_QUEUE_FIELDS = {
    "decision_id", "policy_version", "decision_action", "qualification_verdict",
    "qualification_score", "qualification_floor",
    "preference_score", "outcome_score", "decision_confidence",
    "decision_created_at", "decision_expires_at",
}
missing = CANONICAL_QUEUE_FIELDS - row.keys()
if missing or row["decision_action"] != "apply" or row["qualification_verdict"] != "qualified":
    raise ValueError(f"canonical decision provenance required: {sorted(missing)}")
```

Persist fields in both UPSERTs. Add SQL guards comparing queue policy to the lane policy in `fleet_config`, requiring `decision_action='apply'`, `qualification_verdict='qualified'`, non-null decision ID, nonexpired decision, and `qualification_score >= qualification_floor`. Preserve the final-score `approval_threshold` guard plus canary, pause, governor, dedupe, and spend guards.

Make legacy `push_jobs()`/`lease_one()` raise a clear runtime error directing callers to v3 canonical queues; update or retire `container_worker` and `apply.fleet_sync` callers so no bypass remains.

- [ ] **Step 4: Run focused and concurrency tests**

Run: `python -m pytest tests/test_fleet_apply_lane.py tests/test_fleet_linkedin_lane.py tests/test_fleet_pgqueue.py tests/test_fleet_v3_governor_queue.py -q`

Expected: all tests pass, including existing atomic canary tests.

- [ ] **Step 5: Commit**

```powershell
git add src/applypilot/fleet/queue.py src/applypilot/apply/pgqueue.py src/applypilot/apply/container_worker.py src/applypilot/apply/fleet_sync.py tests/test_fleet_apply_lane.py tests/test_fleet_linkedin_lane.py tests/test_fleet_pgqueue.py tests/test_fleet_v3_governor_queue.py
git commit -m "feat(fleet): fail closed without canonical policy"
```

### Task 7: Add audited promotion, rollback, and status commands

**Files:**
- Modify: `src/applypilot/cli.py`
- Modify: `src/applypilot/fleet/apply_home_main.py`
- Modify: `src/applypilot/fleet/linkedin_home_main.py`
- Modify: `src/applypilot/import_decisions.py`
- Create: `tests/test_canonical_cli.py`
- Modify: `tests/test_fleet_apply_home.py`
- Modify: `tests/test_fleet_linkedin_home.py`
- Modify: `tests/test_import_decisions.py`

- [ ] **Step 1: Write failing CLI tests**

Use `CliRunner` to cover `canonical status`, `canonical validate`, `canonical promote --lane ats`, `canonical retire`, `canonical backfill`, and outcome review. Promotion must require replay metrics and explicit lane. Retirement must pause the lane and invalidate queued rows for that policy. `import-decisions` must no longer write audit authority.

- [ ] **Step 2: Confirm failure**

Run: `python -m pytest tests/test_canonical_cli.py tests/test_fleet_apply_home.py tests/test_fleet_linkedin_home.py tests/test_import_decisions.py -q`

Expected: canonical command group does not exist.

- [ ] **Step 3: Implement owner commands and status**

Register a `canonical_app = typer.Typer()` subgroup. Keep activation in Python. Promotion updates SQLite policy status and the selected PG `fleet_config.*_policy_version` transactionally as two explicit audited steps; on PG failure, leave policy validated and report no activation. Status reports missing/stale/mismatched provenance counts per lane.

Change legacy promotion commands to import records as migration/review inputs only; they must not set `audit_score`, `canonical_action`, or queue approval.

- [ ] **Step 4: Run CLI tests**

Run: `python -m pytest tests/test_canonical_cli.py tests/test_fleet_apply_home.py tests/test_fleet_linkedin_home.py tests/test_import_decisions.py -q`

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/applypilot/cli.py src/applypilot/fleet/apply_home_main.py src/applypilot/fleet/linkedin_home_main.py src/applypilot/import_decisions.py tests/test_canonical_cli.py tests/test_fleet_apply_home.py tests/test_fleet_linkedin_home.py tests/test_import_decisions.py
git commit -m "feat(policy): add audited canonical promotion controls"
```

### Task 8: Document rollout and verify the runtime branch

**Files:**
- Create: `docs/canonical-decision-v2-runbook.md`
- Modify: `docs/superpowers/specs/2026-06-25-unified-brain-pipeline-design.md`

- [ ] **Step 1: Write the runbook**

Document schema migration, backfill/reconciliation, draft scoring handoff, replay review, ATS and LinkedIn policy promotion, queue invalidation, rollback, and the rule that ATS stays paused until explicit promotion. Mark the old spec's advisory-authority sections as superseded by the July 10 design.

- [ ] **Step 2: Run complete runtime verification**

```powershell
$env:PYTHONPATH='src'
python -m pytest -q
python -m ruff check src tests
git diff --check
```

Expected: all tests pass, Ruff reports no errors, and `git diff --check` is clean.

- [ ] **Step 3: Verify no legacy authority remains**

Run:

```powershell
rg -n "COALESCE\(audit_score|include_research|research_fit_score.*approval|def lease_one|def push_jobs" src/applypilot
```

Expected: no fleet authorization query uses audit/fit/research fallback; legacy functions are explicit refusal shims only.

- [ ] **Step 4: Commit**

```powershell
git add docs/canonical-decision-v2-runbook.md docs/superpowers/specs/2026-06-25-unified-brain-pipeline-design.md
git commit -m "docs: add canonical decision rollout runbook"
```
