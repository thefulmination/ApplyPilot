# RC5 Preflight Branch Disposition Matrix

Revalidated on 2026-07-17 before RC5 creation. This addendum preserves the
historical RC4 matrices and updates only the canonical release positions.

| Repository | Canonical integration branch | Exact head | Published remote head |
|---|---|---|---|
| Runtime | `codex/cloud-cutover-runtime-integration` | `55f670f0a137630eefce91a357f6e6d62d77afad` | `myfork/codex/cloud-cutover-runtime-integration` at the same SHA |
| Brain | `codex/cloud-cutover-brain-integration` | `3a63760d49a38a1d99954c9523050a4e263e7e03` | `origin/codex/cloud-cutover-brain-integration` at the same SHA |

The current heads supersede RC4's `4c57287` Runtime and `89a9944` Brain tag
targets. RC4 remains immutable historical evidence; it is not an RC5 release
candidate.

## Runtime Source Branches

| Branch | Head | Verified prior disposition | RC5 action |
|---|---|---|---|
| `claude/apply-runtime-triage` | `1e0e4a1` | 3/3 patch-contained | Archive-only. |
| `claude/container-build-gate` | `bf6338a` | 1/1 patch-contained | Archive-only. |
| `claude/resbuild-postgres-authority` | `fdf16af` | 1/1 patch-contained | Archive-only. |
| `codex/a1-admission-gate` | `c88c263` | 2/2 patch-contained | Retained archive ref. |
| `codex/a1-containment-corrections` | `241abc4` | 5/5 patch-contained | Retain through release tagging. |
| `codex/audit-remediation-runtime` | `93f0c47` | 1/3 patch-proven | Retain; two commit mappings remain unresolved. |
| `codex/fleet-dbhot-live-hotfix` | `706bf42` | 4/7 patch-proven | Retain; three commit mappings remain unresolved. |
| `codex/fleet-dbhot-stage1` | `2dfbaa3` | 6/11 patch-proven | Retain; five commit mappings remain unresolved. |
| `codex/fleet-dbhot-stage1-rollback` | `f364bfe` | Rollback-only | Preserve; never merge into the forward release. |
| `codex/p0-crash-idempotent-evidence-20260713-a25132` | `5720519` | 0/1 patch-proven | Archive-only evidence snapshot. |
| `codex/postgres-canonical-brain-phase1` | `6a0e3dc` | 5/5 patch-contained | Retain archive ref through RC5 tagging. |
| `codex/runtime-postgres-p0-p1` | `bf3e1d2` | 7/7 patch-contained | Retain archive ref through RC5 tagging. |
| `codex/snapshot-python-main-safety-20260716` | `4cde021` | 0/1 patch-proven | Retain until its six-file snapshot has a concrete mapping. |

## Brain Source Branches

| Branch | Head | Verified prior disposition | RC5 action |
|---|---|---|---|
| `codex/distributed-consolidation-control-20260716` | `17174ae` | 0/4 patch-proven | Archive-only coordination/evidence history; not a brain input. |
| `codex/postgres-brain-shadow` | `6d6c311` | 5/5 patch-contained | Retain archive ref through RC5 tagging. |
| `codex/staged-convergence-rc` | `506d00a` | 5/5 patch-contained | Retain remote archive ref through RC5 tagging. |
| `codex/unified-brain-integration` | `55c020b` | 2/2 patch-contained | Retain archive ref through RC5 tagging. |

## Release Rule

No listed branch is an RC5 release input merely because it remains reachable.
No worktree or branch may be removed until the eventual RC5 compatibility
manifest, immutable artifact receipts, and release tag are all pushed. The
partially patch-proven Runtime rows and the rollback branch remain explicit
retention blockers, not authorization to merge their histories into the
forward release.
