# Codex Release Remediation Report

Run date: 2026-07-16

Branch: `codex/cloud-cutover-runtime-integration`
Plan: `docs/superpowers/plans/2026-07-16-parallel-release-remediation.md`
Candidate head before this evidence commit: `01638c7`

## Integrated Work

- `26f0d3b` sanitizes the fleet-version test subprocess while preserving the fail-closed database environment contract.
- `ae48526` updates the OTP relay test double for the canonical Postgres pending-request procedure.
- `29b2b36` updates apply/auth test doubles for lease interaction markers and canonical connection propagation.
- `93c4dce` restores canonical ATS prior-evidence lease blockers and adds a lease-ledger-guarded infrastructure-failure transition with attempt, governor, and canary refunds.
- `27cc59e` makes controller challenge tests seed explicit controller-owned parked state instead of forging worker lease authority.
- `d1638fb` integrates Claude C2's non-publishing container build and smoke gate.
- `01638c7` integrates Claude C3's read-only apply-runtime triage report.

Claude C1 (`claude/resbuild-postgres-authority`) has not returned a repair commit and remains outside the completed Codex scope.

## Root Causes Repaired

1. Correct lease-marker and `conn=` production contracts were being bypassed by stale apply/auth test doubles.
2. The canonical `fleet_worker_lease_ats` migration dropped the prior browser-interaction and remediator-requeue exclusion predicates, allowing unsafe re-leasing.
3. Untouched browser-preflight failures were routed through generic terminalization, consuming an attempt, omitting infrastructure counters/refunds, and emitting false application evidence.
4. Challenge controller tests manually created leases that could not satisfy the canonical worker lease ledger and active-lease session binding.
5. A canary-capacity test used one host for every row and was blocked by the independent host pacing governor after its first lease.
6. The version-script subprocess inherited a forbidden legacy DSN variable from the developer machine.

## Verification

- Apply/auth focused gate: `133 passed in 19.39s`.
- Fleet, schema, role, and version focused gate: `129 passed in 143.84s`.
- Container static contract: `7 passed in 0.40s`.
- `python -m ruff check src`: passed.
- Touched-file Ruff gate: passed.
- Workflow YAML parse: passed.
- `git diff --check`: passed before commits.
- Full four-worker diagnostic: `3971 passed, 17 skipped, 13 failed in 1088.65s`.
- All 13 diagnostic failures are in `tests/test_resbuild_bridge.py` and belong to unfinished Claude C1. Each reaches `pgqueue.connect()` from a legacy SQLite fixture without an explicitly injected Postgres authority dependency.
- Complete serial suite: inconclusive. The command exceeded the 30-minute local timeout and returned no test summary. This is not a passing or failing result.

## Container And CI

- Local Docker build: not run because Docker is not installed on this machine.
- The integrated GitHub Actions `container-build` job builds without registry login or push and runs deterministic image smoke checks.
- Workflow run URL: pending branch push and non-publishing dispatch.

## Release Decision

The Codex-owned X1 and X2 repairs are complete and focused-green. The overall release remains **not ready** until:

1. Claude C1 repairs and verifies `tests/test_resbuild_bridge.py` without weakening Postgres authority.
2. A complete serial or deterministic sharded Python suite finishes successfully.
3. The non-publishing container image build and smoke job succeeds in GitHub Actions.

No production PostgreSQL, secrets, deployment, worker command, application lane, or container registry was touched.
