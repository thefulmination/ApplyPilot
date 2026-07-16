# Codex Release Remediation Report

Run date: 2026-07-16

Branch: `codex/cloud-cutover-runtime-integration`
Plan: `docs/superpowers/plans/2026-07-16-parallel-release-remediation.md`
Verified candidate head: `5f75b7cb3e150cf6bf96b4664de80c3c20dac393`

## Integrated Work

- `26f0d3b` sanitizes the fleet-version test subprocess without weakening the fail-closed database environment contract.
- `ae48526` updates the OTP relay double for the canonical Postgres pending-request procedure.
- `29b2b36` updates apply/auth doubles for lease interaction markers and canonical connection propagation.
- `93c4dce` restores ATS prior-evidence lease blockers and adds a ledger-guarded infrastructure-failure transition with attempt, governor, and canary refunds.
- `27cc59e` makes challenge tests seed controller-owned parked state instead of forging worker lease authority.
- `d1638fb` integrates Claude C2's non-publishing container build and smoke gate.
- `01638c7` integrates Claude C3's read-only apply-runtime triage report.
- `e0b05d9` aligns the supported Python matrix with the pinned NumPy runtime and installs JobSpy without allowing it to replace pinned dependencies.
- `86b9e41` makes the fleet environment contract test portable under the pytest console entrypoint.
- `bfc4d62` separates Windows-native and portable CI contracts, restores Railway entrypoint executable mode, ensures MCP and fleet helper paths are self-contained, and gives migration verification full predecessor history.
- `b721b52` makes browser tests platform-deterministic and hardens brain snapshot recovery across POSIX hard-link ctime changes.
- `5f90feb` preserves receipt ACLs after durable atomic replacement and removes ambient libpq variables from the mapped-control fixture.
- `dde99e3` integrates Claude C1's Postgres-authority dependency injection for ResBuild and isolates SQLite compatibility fixtures from the ambient fleet DSN.

Claude C1 source commit: `fdf16af`. The commit was pushed to `claude/resbuild-postgres-authority` and integrated into this branch as `dde99e3`.

## Root Causes Repaired

1. Stale apply/auth doubles bypassed required lease markers and `conn=` propagation.
2. The canonical ATS lease function had lost prior browser-interaction and remediator-requeue exclusions.
3. Untouched browser-preflight failures consumed attempts and emitted false application evidence instead of refunding infrastructure reservations.
4. Challenge tests created leases that violated the canonical lease ledger and session binding.
5. CI mixed Windows handle, PowerShell 5.1, `cmd.exe`, and `msvcrt` contracts into Ubuntu jobs.
6. Apply workers wrote MCP configuration before ensuring the application directory existed.
7. Root fleet helpers depended on the caller placing the repository root on `sys.path`.
8. Shallow CI checkout prevented cryptographic verification of the pinned migration predecessor.
9. Brain recovery treated a legitimate POSIX hard-link ctime change as replacement; recovery now binds device, inode, size, and mtime before and after hashing, then verifies SHA-256 and SQLite integrity.
10. Durable receipt replacement did not explicitly reapply the captured destination ACL after `File.Replace`.

## Local Verification

- Apply/auth focused gate: `133 passed in 19.39s`.
- Fleet, schema, role, and version focused gate: `129 passed in 143.84s`.
- Portability and workflow gate: `113 passed`.
- Brain, apply-channel, and mapped-role gate: `92 passed`.
- Final Windows failure regression gate: `3 passed`.
- ResBuild authority gate after C1 integration: `24 passed in 2.43s`.
- `python -m ruff check src`: passed.
- Touched-file Ruff checks: passed.
- Workflow YAML parse: passed.
- `git diff --check`: passed.
- Full four-worker diagnostic before C1: `3971 passed, 17 skipped, 13 failed`; all 13 failures were legacy ResBuild SQLite fixtures reaching an ambient Postgres DSN.
- Complete local serial suite: inconclusive because it exceeded the 30-minute local timeout without a summary. This is not represented as a pass or failure.

## Final CI Evidence

Final workflow: https://github.com/thefulmination/ApplyPilot/actions/runs/29516416588

- Python 3.11: `2230 passed, 982 skipped in 103.45s`.
- Python 3.12: `2230 passed, 982 skipped in 95.00s`.
- Python 3.12 wheel and source distribution: passed.
- Windows-native contracts: `894 passed, 1 skipped in 984.51s`.
- Container candidate build: passed without registry login or push.
- Deterministic container smoke check: passed.
- Overall workflow conclusion: `success`.

The skipped CI tests require external services, credentials, or integration infrastructure not present on GitHub-hosted runners. They remain explicit release-environment follow-up gates, not silent passes.

## Scope Decision

Codex-owned X1, X2, and X3 integration/release-gate work are complete, including Claude C1. The parallel remediation plan is complete through code integration and CI verification. Production deployment, staging validation, and canary approval remain separate operational steps.

No production PostgreSQL, secrets, deployment, worker command, application lane, or container registry was touched. Production application lanes remain paused pending separate staging/canary approval after C1 is integrated and verified.
