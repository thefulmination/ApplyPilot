# PostgreSQL 18 Authority Catalog Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fail closed before any authority mutation unless the database is exact PostgreSQL 18, and fingerprint the complete stable PostgreSQL 18 constraint shape with authoritative PG18-generated pins.

**Architecture:** A single read-only preflight in `schema.py` owns the exact server/catalog contract and is invoked first at every schema, role, install, and bootstrap boundary. Hash functions serialize stable qualified identities and non-pretty definitions into a PostgreSQL-18-only immutable pin map; PostgreSQL 16 is used only to prove zero drift on rejection.

**Tech Stack:** Python 3.12, psycopg 3, PostgreSQL 16/18 system catalogs, pytest, Ruff.

---

### Task 1: PostgreSQL 16 zero-drift red tests

**Files:**
- Modify: `tests/test_brain_pg_schema.py`
- Modify: `tests/test_pg_role_bootstrap.py`

- [ ] Add parameterized schema-boundary tests covering V1/V4/V5/V6/V7 ensure and verify entry points. Snapshot roles, public relations, ledger, and advisory locks; assert `RuntimeError` matching `PostgreSQL 18 authority catalog contract required`, then assert identical snapshots.
- [ ] Add direct candidate-role, artifact-role, fixed-install, and bootstrap cases. Bootstrap uses fresh `DurableEvidencePaths` and asserts absent evidence files, unchanged database state, and no fleet bootstrap advisory lock.
- [ ] Run `\.venv\Scripts\python.exe -m pytest -q tests/test_brain_pg_schema.py tests/test_pg_role_bootstrap.py -k "pg18 or unsupported_major"`.
- [ ] Expected: new tests fail because PostgreSQL 16 currently reaches schema/catalog operations or locks.

### Task 2: Exact read-only PG18 preflight

**Files:**
- Modify: `src/applypilot/brain/schema.py`
- Modify: `src/applypilot/fleet/pg_roles.py`
- Test: `tests/test_brain_pg_schema.py`
- Test: `tests/test_pg_role_bootstrap.py`

- [ ] Define immutable ordered tuples `_PG18_CATALOG_SHAPES` for `pg_catalog.pg_constraint` and `pg_catalog.pg_auth_members`, using the exact name/type sequences in the approved design.
- [ ] Implement the guard with this control flow; `_PG18_CATALOG_SHAPES` supplies the approved exact tuples:

```python
def require_pg18_authority_catalog(cur) -> None:
    cur.execute("SELECT current_setting('server_version_num')::integer AS server_version_num")
    version = cur.fetchone()["server_version_num"]
    if version // 10000 != 18:
        raise RuntimeError(
            "PostgreSQL 18 authority catalog contract required: "
            f"server_version_num={version}"
        )
    for relation, expected in _PG18_CATALOG_SHAPES.items():
        cur.execute(
            "SELECT attname,format_type(atttypid,atttypmod) AS data_type "
            "FROM pg_attribute WHERE attrelid=%s::regclass AND attnum>0 "
            "AND NOT attisdropped ORDER BY attnum",
            (relation,),
        )
        actual = tuple((row["attname"], row["data_type"]) for row in cur.fetchall())
        if actual != expected:
            raise RuntimeError(
                f"PostgreSQL 18 authority catalog shape mismatch for {relation}: {actual!r}"
            )
```
- [ ] Call the helper before any other query in every approved direct boundary. In `bootstrap_database_roles`, call it after idle/autocommit validation but before SCRAM derivation, advisory lock, inventory, or evidence work.
- [ ] Re-run Task 1 tests. Expected: all PG16 rejection tests pass with zero drift.
- [ ] Commit the guard and red/green tests.

### Task 3: PG18-only complete hash query shape

**Files:**
- Modify: `src/applypilot/brain/schema.py`
- Modify: `tests/test_brain_pg_schema.py`

- [ ] Replace scalar pins with this nested immutable shape. Verification rejects the sentinel explicitly; it is a release stop, never a fallback.

```python
_UNPINNED_PG18_CATALOG_HASH = "PG18_PIN_REQUIRED"
_PG_CATALOG_HASHES = MappingProxyType({
    18: MappingProxyType({
        name: _UNPINNED_PG18_CATALOG_HASH
        for name in ("base", "current_base", "v5", "current_v5", "v6", "v7")
    })
})
```
- [ ] Extend base and V5 constraint SELECTs with every approved scalar/array field. Resolve namespace, relation, type, index, parent constraint, referenced relation, and operator OIDs to stable qualified identities; never serialize raw cluster-local OIDs.
- [ ] Use `pg_get_expr(...,false)`, `pg_get_constraintdef(...,false)`, `pg_get_indexdef(oid,0,false)`, `pg_get_triggerdef(...,false)`, and `pg_get_viewdef(...,false)` consistently only inside hash-generation functions.
- [ ] Add static tests asserting the exact constraint field set, absence of old scalar pin names/PG16 hashes, immutable map structure, and non-pretty hash calls.
- [ ] Run Ruff, pycompile, and PG16 zero-drift tests; commit the query-shape boundary and immediately send its SHA to the PG18 diagnostic lane.

### Task 4: Authoritative PG18 pins and PG18 NOT NULL constraints

**Files:**
- Modify: `src/applypilot/brain/schema.py`
- Modify: `tests/test_brain_pg_schema.py`

- [ ] On disposable PostgreSQL 18.4, run fresh V1, V4, V5, V6, and V7 ensure/verify plus upgrade paths against the exact Task 3 SHA and return six lowercase SHA-256 values keyed `base`, `current_base`, `v5`, `current_v5`, `v6`, `v7`.
- [ ] Replace every fail-closed sentinel with the returned six-value immutable PG18 map and add exact pin assertions.
- [ ] Update manual PG18 checks for `contype='n'`: V6 request and registration counts include 11 and 13 NOT NULL constraints; V7 topology includes two NOT NULL constraints with `conenforced=true`.
- [ ] Run the fresh PG18 matrix again. Expected: all ensure, verify, install, role reconciliation, bootstrap, and upgrade tests pass with exact hashes.
- [ ] Commit authoritative PG18 pins and manual constraint expectations.

### Task 5: Final GGGTower and release verification

**Files:**
- Verify only; preserve `.pytest-evidence/`, `.pytest-full-suite-retry/`, and the untracked coordinator plan.

- [ ] Run PG16 unsupported-major tests and confirm zero DB/file/lock drift.
- [ ] Run Ruff on the four changed Python files, `py_compile` on those files, and `git diff --check`.
- [ ] Confirm tracked scope, exact V1-V7 SQL hashes, PG18 pin map, commit chain, and no push/live mutation.
