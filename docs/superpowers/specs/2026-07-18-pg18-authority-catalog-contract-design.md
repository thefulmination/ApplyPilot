# PostgreSQL 18 Authority Catalog Contract

## Goal

Authority schema ensure, verify, role reconciliation, installation, and bootstrap operations must run only against PostgreSQL 18. A server-major or catalog-shape mismatch must fail before advisory locks, evidence writes, role changes, DDL, or ledger mutations.

## Preflight boundary

Add one read-only `require_pg18_authority_catalog(cur)` helper. It reads `server_version_num`, requires major 18, and verifies that the security-relevant catalog fields used by the authority contract exist:

- `pg_constraint`: `conenforced` plus every constraint-shape field included in hashing, including `condeferrable`, `condeferred`, `convalidated`, `connoinherit`, foreign-key action/match fields, and `conperiod` where PostgreSQL 18 exposes it.
- `pg_auth_members`: `grantor`, `admin_option`, `inherit_option`, and `set_option`.

Missing or unknown fields fail closed. The helper performs only catalog reads and never acquires a lock or changes session state.

Call it first at every direct authority boundary: all in-transaction schema ensure/verify functions, direct candidate/artifact role reconcilers, fixed authority installation, and fleet bootstrap before its session advisory lock and evidence preparation. Wrapper functions inherit the guard through their in-transaction entry point.

## Catalog fingerprints

Replace ambiguous scalar catalog pins with explicitly PostgreSQL-18-named mappings/constants. Do not retain a PostgreSQL 16 fallback or normalize records across major versions.

Base and V5 constraint payloads include the complete available PostgreSQL 18 security shape: enforcement, validation, deferrability, inheritance, foreign-key action/match/equality-operator fields, exclusion operator fields, period flags, and referenced relation/index identifiers where already security-relevant.

All `pg_get_*` calls that feed hashes use non-pretty output consistently. Deep semantic verifiers outside hash generation keep their current rendering behavior. Every changed pin is regenerated from a fresh PostgreSQL 18 database only.

## Tests

On GGGTower PostgreSQL 16, red tests snapshot database objects/roles/ledger, relevant files, and advisory-lock availability; each direct entry point must raise an explicit PostgreSQL-18 preflight error with no drift. Catalog-shape test doubles cover wrong major and missing fields.

PostgreSQL 18 diagnostics provide the only accepted replacement hash values. Static checks verify no PostgreSQL 16 pins/fallbacks remain, preflight calls precede locks and mutations, and pretty-output changes are confined to hash generation.

## Non-goals

- Supporting PostgreSQL 16 for authority operations.
- Producing a cross-major normalized fingerprint.
- Mutating live fleet or Railway databases during this correction.
