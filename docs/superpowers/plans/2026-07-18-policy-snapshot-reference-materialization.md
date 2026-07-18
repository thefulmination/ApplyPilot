# Policy Snapshot Reference Materialization Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Materialize each non-null policy snapshot fingerprint as deterministic RFC 8785 reference bytes and bind lifecycle roles to the wrapper content hash.

**Architecture:** Extend the pure policy compiler with a snapshot-reference descriptor whose `policy_source_id` is exactly its canonical JSON text. Include the three existing lifecycle roles in importer and parity binding sets, while retaining original fingerprints in policy metadata and validating registered provenance on import.

**Tech Stack:** Python 3.12, RFC 8785, SHA-256, pytest, Ruff

---

### Task 1: Compile canonical snapshot-reference artifacts

**Files:**
- Modify: `src/applypilot/brain/policy_artifacts.py`
- Test: `tests/test_brain_policy_artifacts.py`

- [ ] **Step 1: Write failing exact-content, null-omission, and fourteen-row aggregate tests**

Add tests that parse the three emitted wrappers and assert the exact object:
```python
{
    "kind": "applypilot.policy.snapshot-reference",
    "lane": "ats",
    "policyVersion": "canonical-v7-ats",
    "role": "label_snapshot",
    "schemaVersion": 1,
    "sourceField": "label_snapshot",
    "sourceSha256": LABEL_SNAPSHOT,
}
```
Assert `artifact.sha256 == hashlib.sha256(artifact.content).hexdigest()`,
`artifact.sha256 != LABEL_SNAPSHOT`, and
`artifact.policy_source_id == artifact.content.decode("utf-8")`. Parameterize
one-null-field cases and compile fourteen rows where only the ATS and LinkedIn
candidate rows have all three hashes, yielding six wrappers total.

- [ ] **Step 2: Run tests and verify they fail**

Run: `.venv/Scripts/python -m pytest -q tests/test_brain_policy_artifacts.py`
Expected: FAIL because snapshot roles and `policy_source_id` are absent.

- [ ] **Step 3: Implement the minimal compiler**

Add `policy_source_id: str | None = None` to `ArtifactDescriptor`. Add the
ordered mapping `label_snapshot`, `pairwise_snapshot`, `outcome_snapshot`.
For each non-null hash, canonicalize the closed wrapper object with
`_canonical_bytes`, construct the descriptor with the existing media type,
SHA-256 the wrapper bytes, and set `policy_source_id=content.decode("utf-8")`.
Append wrappers after existing compiled artifacts.

- [ ] **Step 4: Run compiler tests and verify they pass**

Run: `.venv/Scripts/python -m pytest -q tests/test_brain_policy_artifacts.py`
Expected: PASS.

### Task 2: Bind and verify wrapper hashes in importer and parity

**Files:**
- Modify: `src/applypilot/brain/sqlite_to_postgres.py`
- Modify: `src/applypilot/brain/parity_adapters.py`
- Test: `tests/test_brain_sqlite_to_postgres.py`
- Test: `tests/test_brain_parity_adapters.py`
- Test: `tests/test_brain_pg_schema.py`

- [ ] **Step 1: Write failing binding and provenance tests**

Assert importer/parity bindable role sets include the three snapshot roles.
Extend the Postgres integration fixture to register each compiled snapshot
artifact with provenance
`{"policy_source_id": artifact.policy_source_id}`, and assert stored bindings
equal `compiled.artifact(role).sha256`, not the source fingerprints. Add
unit cases where missing, metadata-mismatched, or provenance-mismatched ledger
rows raise `BrainImportError`.

- [ ] **Step 2: Run focused tests and verify they fail**

Run: `.venv/Scripts/python -m pytest -q tests/test_brain_sqlite_to_postgres.py tests/test_brain_parity_adapters.py tests/test_brain_pg_schema.py -k "policy or snapshot"`
Expected: FAIL because snapshot roles are not bindable and provenance is not checked.

- [ ] **Step 3: Implement the minimal integration**

Add the three roles to both bindable sets. Select `provenance` in
`_require_artifact`; for descriptors with non-null `policy_source_id`,
require a mapping whose `policy_source_id` equals the descriptor value.
Retain the existing hash, length, media-type, and conflicting-binding checks.

- [ ] **Step 4: Run focused tests and verify they pass**

Run the Step 2 command.
Expected: PASS (Postgres-backed tests may skip only when their declared fixture
is unavailable; unit tests must pass).

### Task 3: Verify and commit

**Files:**
- Verify all files listed above plus the approved design and this plan.

- [ ] **Step 1: Run focused suites**

Run: `.venv/Scripts/python -m pytest -q tests/test_brain_policy_artifacts.py tests/test_brain_sqlite_to_postgres.py tests/test_brain_parity_adapters.py`
Expected: all pass.

- [ ] **Step 2: Run static checks**

Run: `.venv/Scripts/python -m ruff check src/applypilot/brain/policy_artifacts.py src/applypilot/brain/sqlite_to_postgres.py src/applypilot/brain/parity_adapters.py tests/test_brain_policy_artifacts.py tests/test_brain_sqlite_to_postgres.py tests/test_brain_parity_adapters.py`
Run: `.venv/Scripts/python -m py_compile src/applypilot/brain/policy_artifacts.py src/applypilot/brain/sqlite_to_postgres.py src/applypilot/brain/parity_adapters.py`
Run: `git diff --check`
Expected: all commands exit zero.

- [ ] **Step 3: Self-review and commit intended files**

Review `git diff --stat`, `git diff`, and `git status --short`; then commit
only the approved spec, plan, compiler, importer, parity, and focused tests with
message `feat(brain): materialize policy snapshot references`. Do not push.
