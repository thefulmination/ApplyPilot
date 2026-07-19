# PostgreSQL 18 Authority Catalog Contract

## Goal

Authority schema ensure, verify, role reconciliation, installation, and bootstrap operations must run only against PostgreSQL 18. A server-major or catalog-shape mismatch must fail before advisory locks, evidence writes, role changes, DDL, or ledger mutations.

## Preflight boundary

Add one read-only `require_pg18_authority_catalog(cur)` helper. It reads `server_version_num`, requires major 18, and verifies exact ordered name/type tuples for both catalogs. Extra, missing, reordered, or retyped columns fail closed.

- `pg_constraint`: `oid:oid`, `conname:name`, `connamespace:oid`, `contype:"char"`, `condeferrable:boolean`, `condeferred:boolean`, `conenforced:boolean`, `convalidated:boolean`, `conrelid:oid`, `contypid:oid`, `conindid:oid`, `conparentid:oid`, `confrelid:oid`, `confupdtype:"char"`, `confdeltype:"char"`, `confmatchtype:"char"`, `conislocal:boolean`, `coninhcount:smallint`, `connoinherit:boolean`, `conperiod:boolean`, `conkey:smallint[]`, `confkey:smallint[]`, `conpfeqop:oid[]`, `conppeqop:oid[]`, `conffeqop:oid[]`, `confdelsetcols:smallint[]`, `conexclop:oid[]`, `conbin:pg_node_tree`.
- `pg_auth_members`: `oid:oid`, `roleid:oid`, `member:oid`, `grantor:oid`, `admin_option:boolean`, `inherit_option:boolean`, `set_option:boolean`.

The helper performs only catalog reads and never acquires a lock or changes session state.

Call it first at this direct-boundary matrix:

- Schema: `ensure_brain_schema_v1_in_transaction`, `verify_brain_schema_v1` through `_verify_contract`, and the V4, V5, V6, and V7 `ensure_*_in_transaction` and `verify_*_in_transaction` functions. Connection wrappers inherit the guard before any schema mutation through these calls.
- Role authority: `ensure_brain_candidate_roles_in_transaction`, its connection wrapper, `ensure_brain_artifact_authority_roles_in_transaction`, and `_install_brain_authority_in_transaction`.
- Bootstrap: `bootstrap_database_roles` before the session advisory lock, inventory, rollback rendering, evidence preparation, fencing, role reconciliation, or authority installation. `_install_brain_authority_in_transaction` remains independently guarded for direct callers.

At each boundary the guard precedes search-path changes, advisory locks, DDL, ledger writes, role/ACL/membership changes, and file evidence writes.

## Catalog fingerprints

Replace ambiguous scalar catalog pins with an immutable `MappingProxyType` keyed by PostgreSQL major `18`, whose value is another immutable mapping or tuple containing exactly: `base`, `current_base`, `v5`, `current_v5`, `v6`, and `v7`. There is no `.get()`, default, PostgreSQL 16 entry, or scalar fallback. Every verifier selects an exact named PostgreSQL 18 pin after the preflight.

Base and V5 constraint payloads include all of these exact fields without qualification: `contype`, `condeferrable`, `condeferred`, `conenforced`, `convalidated`, `conrelid`, `contypid`, `conindid`, `conparentid`, `confrelid`, `confupdtype`, `confdeltype`, `confmatchtype`, `conislocal`, `coninhcount`, `connoinherit`, `conperiod`, `conkey`, `confkey`, `conpfeqop`, `conppeqop`, `conffeqop`, `confdelsetcols`, `conexclop`, plus non-pretty `pg_get_constraintdef` output. Cluster-local OIDs are never serialized raw: namespace/relation/type/index/parent/referenced identities are rendered as stable qualified names, and operator OID arrays are rendered in order as qualified operator signatures under the fixed hash search path. Attribute-number arrays and scalar flags are serialized directly. PostgreSQL 18 `contype='n'` NOT NULL constraints remain in the payload and are represented explicitly in manual V6/V7 exact checks.

All `pg_get_*` calls that feed hashes use non-pretty output consistently. Deep semantic verifiers outside hash generation keep their current rendering behavior. Every changed pin is regenerated from a fresh PostgreSQL 18 database only.

## Tests

On GGGTower PostgreSQL 16, red tests snapshot database objects, roles, ledger, evidence files, and advisory-lock availability; each direct entry point in the matrix must raise an explicit PostgreSQL-18 preflight error with no drift. Catalog-shape test doubles cover wrong major, extra, missing, reordered, and retyped fields.

Fresh disposable PostgreSQL 18 tests must cover ensure and verify at V1, V4, V5, V6, and V7, direct role reconciliation/install/bootstrap, upgrades, exact pin assertions, and the final catalog hashes. PostgreSQL 18 diagnostics provide the only accepted replacement hash values. Static checks verify no PostgreSQL 16 pins/fallbacks remain, preflight calls precede locks and mutations, and pretty-output changes are confined to hash generation.

## Non-goals

- Supporting PostgreSQL 16 for authority operations.
- Producing a cross-major normalized fingerprint.
- Mutating live fleet or Railway databases during this correction.
