# Final branch disposition matrix

Generated 2026-07-16 from `codex/cloud-cutover-runtime-integration` at
`8c6c14969ab6728c7c79bfdc495d5fdf5746869a`.

This matrix distinguishes graph ancestry from semantic equivalence. A branch is
not considered contained merely because its work was described in a report.

| Branch | Head | Disposition | Evidence / release action |
|---|---|---|---|
| `claude/apply-runtime-triage` | `1e0e4a1` | archive-only | Documentation triage; no runtime release dependency identified. |
| `claude/container-build-gate` | `bf6338a` | required, verify in CI | Container/Dockerfile gate is represented by the passing container gate report; retain branch until the release CI run is linked. |
| `claude/resbuild-postgres-authority` | `fdf16af` | archive-only | Resbuild test isolation; no production runtime dependency identified. |
| `codex/a1-admission-gate` | `c88c263` | required, verify in CI | Admission-gate changes are not ancestors of this branch; do not claim contained without a cherry-pick or patch-equivalence test. |
| `codex/a1-containment-corrections` | `241abc4` | archive-only pending security review | Emergency containment corrections remain separate safety history; preserve until the containment suite is proven against the release SHA. |
| `codex/audit-remediation-runtime` | `93f0c47` | patch-equivalent / contained by evidence | Runtime report records `a5f0856`, `0578682`, and `93f0c475` as integrated. |
| `codex/fleet-dbhot-live-hotfix` | `706bf42` | patch-equivalent / contained by evidence | Runtime report records the six safety commits as semantically integrated; `9ec4910` is represented by `fab3b1d` and `a340968`. |
| `codex/fleet-dbhot-stage1` | `2dfbaa3` | patch-equivalent / contained by evidence | Runtime report records the stage-1 authority normalization through `2dfbaa3`. |
| `codex/fleet-dbhot-stage1-rollback` | `f364bfe` | rollback-only | Retain as rollback reference; never merge into the forward release. |
| `codex/p0-crash-idempotent-evidence-20260713-a25132` | `5720519` | archive-only pending test proof | Evidence snapshot is retained; release must cite the tested containment implementation, not only this WIP branch. |
| `codex/postgres-canonical-brain-phase1` | `6a0e3dc` | patch-equivalent / contained by evidence | Runtime report records the canonical brain phase-one snapshot as integrated. |
| `codex/runtime-postgres-p0-p1` | `bf3e1d2` | patch-equivalent / contained by evidence | Runtime report records the authority ledger and brain/schema commits as integrated. |
| `codex/snapshot-python-main-safety-20260716` | `4cde021` | patch-equivalent / contained by evidence | Runtime report records the Python main safety snapshot as integrated. |

## Required closure before branch deletion

1. Run the focused tests for rows marked `required, verify in CI` against the
   release SHA and attach the result here.
2. Replace “patch-equivalent / contained by evidence” with a concrete commit
   mapping if any file-level comparison disagrees with the prior runtime report.
3. Push the integration branch and tag before deleting any worktree or branch.
4. Keep the rollback branch and tag reachable from the remote archive.

The matrix is intentionally conservative: unresolved rows block production
promotion, but do not block staging evidence collection while all lanes remain
paused.
