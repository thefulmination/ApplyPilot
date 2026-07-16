# Claude Controller — Serial Release Gate Verification

**Purpose:** Resolve the one X3 release gate that Codex recorded as unresolved.

Codex's `codex-release-remediation-report.md` currently states:

> Complete local serial suite: inconclusive because it exceeded the 30-minute local timeout without a summary. This is not represented as a pass or failure.

That was the correct call on the evidence Codex had — its 30-minute local budget cannot fit a suite that takes ~43 minutes. This file supplies the missing evidence. Codex should update its own report; this file does not modify it.

Plan gate being closed: **Task X3 — "Run the complete Python suite serially in the release environment."**

## Verdict

**PASS.**

```text
python -m pytest -q -p no:randomly
3990 passed, 17 skipped in 2609.30s (0:43:29)
```

- Zero `FAILED`. Zero `ERROR`.
- Commit under test: **`ef39c9b`** ("fix: normalize concurrent artifact root creation race").
- Applies unchanged to **`f118069`**: `git diff --name-only ef39c9b..f118069` touches `docs/` only (verified). No code differs.
- Raw output retained at `C:\tmp\claude-lanes-work\serial-PINNED-ef39c9b.txt`.

## Method (why this run is trustworthy)

Run from a **pinned, detached, isolated worktree** at `C:\tmp\serial-pinned` @ `ef39c9b`, created specifically so that concurrent Codex commits could not mutate the checkout mid-run.

An earlier attempt was run inside the live Codex worktree and is **discarded as tainted**: Codex advanced that branch (`d6728c2` → `ef39c9b`) while the suite was in flight, so runtime-read artifacts (e.g. `schema_v3.sql`) could change underneath a run already in progress. That result is not reported here and should not be relied on.

Claude C1 (`src/applypilot/resbuild_bridge.py`, source `fdf16af`) confirmed present in the pinned tree by blob comparison before the run.

## Not a false green

The gate is green because tests **ran and passed**, not because they were skipped:

- The previously-failing tests, executed directly at `ef39c9b`: `31 passed in 36.15s`
  (`test_fleet_v3_schema.py::test_mapped_scram_worker_starts_through_narrow_contract`,
  `test_otp_relay_worker.py::test_mapped_worker_otp_roundtrip_is_identity_and_lease_bound`,
  `test_fleet_pg_roles.py`).
- Skip count is **17**, identical to the recorded baseline. No skip regression, and no disposable-Postgres skip masquerading as a pass.

## Progression (three completed serial runs, 43–45 min each)

| Commit | Serial result |
|---|---|
| `2b53efd` — Codex's prior "final" head, before C1 | 5 failed, 3955 passed, 17 skipped, **26 errors** |
| `aad8882` — Claude C1 + C3 corrections cherry-picked | 5 failed, 3959 passed, 17 skipped, **26 errors** |
| `ef39c9b` — after Codex's isolation fixes | **0 failed, 3990 passed, 17 skipped, 0 errors** |

Two conclusions follow:

1. **The Claude cherry-picks caused zero regressions.** `2b53efd` → `aad8882` has an identical failure set; the only delta is `+4 passed`, exactly C1's four new tests.
2. **The 5 failures / 26 errors were pre-existing at Codex's own finalized head `2b53efd`,** and were closed by Codex's subsequent commits — `tests: isolate compatibility desired state table`, `ci: run postgres-backed tests against disposable service`, `ci: use postgres database for ACL integration tests`, `ci: match supported postgres catalog version`, `fix: normalize concurrent artifact root creation race`.

## Why this defect stayed invisible

Every failure was a mapped-role / SCRAM / `pg_roles` test sharing one session-scoped disposable Postgres cluster, poisoned by an earlier file in the same session.

- `-n 4` distributes tests across four sessions, so single-session cross-file contamination never materializes. The four-worker diagnostic therefore looked healthier than the code was.
- Focused gates never observe cross-file contamination at all.
- Only a **complete serial run** exposes it — which is precisely the gate the plan specifies, and the one no completed run existed for until now.

Corollary worth recording: the 13 ResBuild failures visible under `-n 4` do **not** occur serially. Those 20 tests already passed at `2b53efd` serially, by accident of ordering. C1 remains correct and valuable — it removes the ambient `FLEET_PG_DSN` dependency so the file is deterministic regardless of DSN, rather than passing by luck — but C1 was never what blocked the serial gate.

## Recommended edit to `codex-release-remediation-report.md`

Replace the "inconclusive" line with the PASS result above, citing `ef39c9b`, the command, `3990 passed, 17 skipped`, and this file. With that, X3's serial gate is genuinely closed rather than closed on a technicality.

## Still open (pre-existing; outside every Claude lane's file ownership)

1. **Full review of `d486d4a`** for other silently-dropped `UPDATE` clauses. That single WIP snapshot commit produced two independent silent safety regressions (G4 double-apply blocker, G6 infrastructure park) within only the ten files C3 inspected. The rest of the commit is unreviewed. Highest-leverage remaining item.
2. **Suite-wide `FLEET_PG_DSN` scrub** (autouse `conftest.py` fixture). `FLEET_PG_DSN` is set in the local environment to the production database; it is only harmless because it is passwordless and `pgqueue.py:161` fails closed. C1 removed that reliance for `test_resbuild_bridge.py` only. Other modules still depend on that guard for production isolation.
3. **Dockerfile non-root gap** (`USER root`, never reversed; no health/smoke command). Documented at `Dockerfile:49-59`, but C3's routing table covers only X1 and X2 — no lane owns it. Needs an owner or an explicitly accepted risk.
