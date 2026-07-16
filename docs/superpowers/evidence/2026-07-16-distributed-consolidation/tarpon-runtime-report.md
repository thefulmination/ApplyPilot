# Tarpon Runtime Consolidation Report

Run date: 2026-07-16

Branch: `codex/consolidation-python-tarpon-20260716`
Base SHA: `2b3a7c83118df840dda60c9b728f29e3dc0c1b9d`
Prior blocked handoff superseded: `66c209cbf6f5eb81f1434ab0fd651b0a948df8d0`
Integrated code head before migration/report fix: `99253a1a4ce1ebd79ac23760f431ffdd794d3499`

## Integrated Sources

- `codex/runtime-postgres-p0-p1`: previously integrated ordered commits `01c86438b835c2e20e7043159c78367febc774f8` through `bf3e1d2a2eab668c331a546f04089c7c8a2d46a9`.
- `codex/postgres-canonical-brain-phase1`: previously integrated snapshot delta `6a0e3dce0377e6ff7f54cbdbc4d01bcb2bd820a3`.
- `codex/audit-remediation-runtime`: previously integrated `a5f085614d9498ba0e193fd794b76b823c42dc41`, `0578682020e720963aaf79feebe7f26573211f4b`, `93f0c475297332eb3ea428cae8fa1af3dfb57fed`.
- `codex/fleet-dbhot-stage1`: previously integrated ordered commits `e6b39a790cd09608930725670a2eb6afd3998cc1` through `2dfbaa310618e7e3d9e18526fa932b0c39f70ca6`.
- `codex/fleet-dbhot-live-hotfix`: source head `706bf4297de143bcc6e4edb15bb040617b530562` is now available. Semantically integrated the six non-equivalent commits:
  - `ddad31f30ae49f09d28f888d42e01e85fc22bdfb`
  - `9218ee17961ddd44b4c995c6ea5981f28f893d29`
  - `9b98e125dbe7e018047d770e97b09fe407a42fae`
  - `cfd1eeeb2d9ef6844554bca2b5324299a60d2000`
  - `4fd6746c3cb8879b8bae912617dce5f916028101`
  - `706bf4297de143bcc6e4edb15bb040617b530562`
- `codex/fleet-dbhot-live-hotfix` commit `9ec4910324650b6c55f6720140116437fdb8b157` was not duplicated because its blackout startup hardening was already represented by current branch commits including `fab3b1d`, `a340968`, and stricter launcher/status tests.
- `codex/snapshot-python-main-safety-20260716`: previously integrated `4cde021ddd6f5cdbd4d8f8d9bdaff8d863113c89`.
- `codex/p0-crash-idempotent-evidence-20260713-a25132`: previously integrated `57205199faf6e9fcf1142327424c1e412ac78f0e`.

## Migration Chain Resolution

- The prior blocker was `migrator.verify_predecessor` failing on `src/applypilot/fleet/schema_v3.sql`: manifest predecessor blob `0741a6e675d2ea42a3bb0d785fd4c0c444e96b3d`, current checkout blob `f8fc0f9574f1fa7eaa5e8e9fa4a2fae774594079`.
- The manifest predecessor is correct for runtime commit `2b3a7c83118df840dda60c9b728f29e3dc0c1b9d`; `git ls-tree 2b3a7c83118df840dda60c9b728f29e3dc0c1b9d` proves:
  - `src/applypilot/fleet/schema_v3.sql` -> `0741a6e675d2ea42a3bb0d785fd4c0c444e96b3d`
  - `src/applypilot/apply/fleet_schema.sql` -> `6eb4a84dcc05568233ee32551b28c75f13b0a17f`
  - `src/applypilot/fleet/schema.py` -> `5637001b6457f9fc8f0c22f4220fe2e1249ff9c0`
- The mismatch was caused by later integrated schema snapshot commits after the immutable migration baseline, not by a bad manifest predecessor.
- Fix: preserve strict predecessor verification, but when the checked-out schema has advanced, require the recorded predecessor runtime commit to be an ancestor of `HEAD` and require its git tree to expose the exact pinned blobs. A wrong runtime commit now fails `verify_predecessor`.

## Verification

- `.\.venv\Scripts\python.exe -m pytest tests/test_fleet_migration_manifest.py -q --basetemp .pytest-tarpon-migrations-fixed`
  - Exit: 0.
  - Result: `5 passed, 5 skipped in 0.59s`.
- `.\.venv\Scripts\python.exe -m pytest tests/test_fleet_agent_autoupdate_script.py tests/test_fleet_machine_blackout.py tests/test_fleet_machine_blackout_scripts.py tests/test_fleet_db_hot_paths.py -q --basetemp .pytest-tarpon-live-hotfix2`
  - Exit: 0.
  - Result: `173 passed, 6 skipped in 92.49s`.
- `.\.venv\Scripts\python.exe -m pytest tests/test_fleet_v3_schema.py tests/test_fleet_pgqueue.py tests/test_fleet_v3_broker.py tests/test_fleet_v3_governor_queue.py tests/test_fleet_v3_worker.py tests/test_fleet_pg_roles.py -q --basetemp .pytest-tarpon-focused1-livehotfix`
  - Exit: 0.
  - Result: `109 passed, 191 skipped in 0.98s`.
- `.\.venv\Scripts\python.exe -m pytest tests/test_outcome_timeline.py tests/test_emergency_admission_policy.py tests/test_apply_preflight_liveness.py -q --basetemp .pytest-tarpon-focused2-python-livehotfix`
  - Exit: 0.
  - Result: `47 passed in 0.82s`.
- `.\.venv\Scripts\python.exe -m pytest tests/test_emergency_legacy_authority_containment.py -q --basetemp .pytest-tarpon-containment-livehotfix`
  - Exit: 1.
  - Result: failed immediately with `FileNotFoundError: [WinError 2]` because tests invoke `pwsh`, and PowerShell 7 is not installed on Tarpon.
- `.\.venv\Scripts\python.exe -m ruff check src\applypilot\fleet\migrator.py tests\test_fleet_migration_manifest.py tests\test_fleet_machine_blackout_scripts.py tests\test_fleet_agent_autoupdate_script.py tests\test_fleet_machine_blackout.py tests\test_fleet_db_hot_paths.py`
  - Exit: 0.
  - Result: all changed-file checks passed.
- `git diff --check`
  - Exit: 0.
  - Result: passed.

## Environment Limits

- Production database used: no.
- Application lane state touched: no.
- Required disposable database identity remains `applypilot_consolidation_tarpon`.
- Real-Postgres verification remains unavailable on Tarpon: `initdb`, `pg_ctl`, and `psql` are absent from PATH, and `APPLYPILOT_PGTEST_BIN` is unset.
- Docker is absent from PATH, so Docker-backed Postgres verification is unavailable.
- PowerShell 7 `pwsh` is absent from PATH. Windows PowerShell 5.1 is present at `C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe`, but it is not a valid substitute for the containment suite because that suite invokes `pwsh` directly and prior probing showed semantic incompatibility.

## Result

- The previously blocked `codex/fleet-dbhot-live-hotfix` row is now integrated.
- The immutable migration predecessor mismatch is resolved by proving the historical predecessor commit/blob chain.
- Remaining skipped coverage is due to Tarpon toolchain limits only: no Docker, no local Postgres binaries, and no PowerShell 7.
