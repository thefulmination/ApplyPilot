# Tarpon Runtime Consolidation Report

Run date: 2026-07-16

Branch: `codex/consolidation-python-tarpon-20260716`
Base SHA: `2b3a7c83118df840dda60c9b728f29e3dc0c1b9d`
Integrated code head before evidence commit: `000d284cd213187f7004d1993276fff8d7515ccb`

## Integrated Sources

- `codex/runtime-postgres-p0-p1`: applied ordered commits `01c86438b835c2e20e7043159c78367febc774f8` through `bf3e1d2a2eab668c331a546f04089c7c8a2d46a9`.
- `codex/postgres-canonical-brain-phase1`: applied snapshot delta `6a0e3dce0377e6ff7f54cbdbc4d01bcb2bd820a3`.
- `codex/audit-remediation-runtime`: applied `a5f085614d9498ba0e193fd794b76b823c42dc41`, `0578682020e720963aaf79feebe7f26573211f4b`, `93f0c475297332eb3ea428cae8fa1af3dfb57fed`.
- `codex/fleet-dbhot-stage1`: applied ordered commits `e6b39a790cd09608930725670a2eb6afd3998cc1` through `2dfbaa310618e7e3d9e18526fa932b0c39f70ca6`.
- `codex/snapshot-python-main-safety-20260716`: applied `4cde021ddd6f5cdbd4d8f8d9bdaff8d863113c89`.
- `codex/p0-crash-idempotent-evidence-20260713-a25132`: applied `57205199faf6e9fcf1142327424c1e412ac78f0e`.

## Unresolved Integration Blocker

- `codex/fleet-dbhot-live-hotfix` was assigned to Tarpon with head `706bf4297de143bcc6e4edb15bb040617b530562`, but neither that head nor its ordered commits were present in this checkout or fetched remotes. `git cat-file -t ddad31f30ae49f09d28f888d42e01e85fc22bdfb` and `git cat-file -t 9218ee17961ddd44b4c995c6ea5981f28f893d29` failed with missing objects. `git show-ref` had no `fleet-dbhot-live-hotfix` ref.

## Conflict Resolutions

- `tests/test_fleet_v3_schema.py`: import-only conflicts from audit, DB-hot, and safety snapshot commits were resolved by retaining all required imports, then removing unused `threading` and `ThreadPoolExecutor` after verification.
- `fleet-blackout-query.py`: resolved toward explicit `FLEET_PG_DSN` / `APPLYPILOT_FLEET_DSN` validation, normalized authority comparison, credential redaction, and fail-closed `BLOCKED|...|blackout-query-error` output.
- `tests/test_fleet_db_hot_paths.py`: combined audit read-only/no-migration tests with stage1 DSN validation, semantic DSN equivalence, canonical authority, and credential redaction tests.
- `tests/test_fleet_machine_blackout_scripts.py`: retained stricter exact `OK` status validation, blocked/malformed status cases, and wrong-label/wrong-role fail-closed cases.
- `tests/test_emergency_legacy_authority_containment.py`: retained existing wrapper snapshot/candidate helpers and added the crash-boundary evidence helper; the parametrized crash test now uses the helper for `post_create` and `pre_hardlink`.
- `src/applypilot/fleet/migrations/manifest-v1.json`: normalized to LF/final newline because `migrator.load_manifest` rejects CRLF manifests.

## Verification

- `.\.venv\Scripts\python.exe -m ruff check src tests`
  - Exit: 1.
  - Result: failed on pre-existing repository-wide lint debt plus one integration artifact. The integration artifact in `tests/test_fleet_v3_schema.py` was fixed. Repo-wide lint still reports many unrelated `F401`, `E402`, `E701`, `E702`, `E731`, and `E741` findings.
- `.\.venv\Scripts\python.exe -m pytest tests/test_fleet_v3_schema.py tests/test_fleet_pgqueue.py tests/test_fleet_v3_broker.py tests/test_fleet_v3_governor_queue.py tests/test_fleet_v3_worker.py tests/test_fleet_pg_roles.py -q --basetemp .pytest-tarpon-focused1`
  - Exit: 0.
  - Result: `109 passed, 191 skipped in 1.31s`.
  - Skip reason: disposable Postgres binary environment was unavailable.
- Initial same focused command without `--basetemp` reached test completion but pytest exited 1 during Windows temp cleanup with `PermissionError` on `pytest-current`; rerun with workspace basetemp succeeded.
- `.\.venv\Scripts\python.exe -m pytest tests/test_outcome_timeline.py tests/test_emergency_admission_policy.py tests/test_apply_preflight_liveness.py -q --basetemp .pytest-tarpon-focused2-python`
  - Exit: 0.
  - Result: `47 passed in 1.08s`.
- `.\.venv\Scripts\python.exe -m pytest tests/test_outcome_timeline.py tests/test_emergency_admission_policy.py tests/test_apply_preflight_liveness.py tests/test_emergency_legacy_authority_containment.py -q --basetemp .pytest-tarpon-focused2`
  - Exit: 1.
  - Result: containment tests failed immediately because `pwsh` was not installed.
- Same command with a temporary `pwsh.exe` shim to Windows PowerShell:
  - Exit: timeout after 305 seconds.
  - Result: not passing; Windows PowerShell is not a valid substitute for PowerShell 7 in this suite.
- Probe: `.\.venv\Scripts\python.exe -m pytest tests/test_emergency_legacy_authority_containment.py::test_pure_after_state_evaluation_reports_unresolved_targets_and_is_nonoperational -q --basetemp .pytest-tarpon-containment-probe`
  - Exit: 1 under the Windows PowerShell shim.
  - Result: payload returned an empty `unresolved_targets` set where the test expected `task`, `service`, `process`, and `wrapper`.
- `.\.venv\Scripts\python.exe -m pytest tests/test_fleet_migration_manifest.py -q --basetemp .pytest-tarpon-migrations2`
  - Exit: 1.
  - Result: `1 failed, 3 passed, 5 skipped`.
  - Failure: `migrator.verify_predecessor` reports `schema_v3.sql` blob mismatch. The immutable manifest pins legacy blob `0741a6e675d2ea42a3bb0d785fd4c0c444e96b3d`; the integrated branch has current blob `f8fc0f9574f1fa7eaa5e8e9fa4a2fae774594079`.
- `git diff --check 2b3a7c83118df840dda60c9b728f29e3dc0c1b9d..HEAD`
  - Exit: 0.

## Disposable Database Evidence

- Production database used: no.
- Required disposable database identity: `applypilot_consolidation_tarpon`.
- Actual real-Postgres verification: blocked. No `conda`, `initdb`, `pg_ctl`, or `psql` command was available on PATH, and no `APPLYPILOT_PGTEST_BIN` was configured. Tests using the `fleet_pg` disposable cluster fixture skipped rather than running real Postgres checks.

## Remaining Risks

- Missing `codex/fleet-dbhot-live-hotfix` objects prevent full assigned-row integration.
- Real-Postgres migration checks did not run because the disposable Postgres toolchain is absent.
- PowerShell containment verification did not pass because PowerShell 7 `pwsh` is absent and Windows PowerShell is semantically incompatible for at least one probe.
- The immutable migration manifest predecessor check conflicts with later integrated schema changes and needs an explicit baseline strategy before this lane can be considered complete.
