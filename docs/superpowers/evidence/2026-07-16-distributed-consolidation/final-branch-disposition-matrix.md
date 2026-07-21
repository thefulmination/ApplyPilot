# Final Python branch disposition matrix

Revalidated 2026-07-17 against `codex/cloud-cutover-runtime-integration` at
`7d338516224b28c32d2e066f0110fd551b626fa7`. The canonical branch matches
`myfork/codex/cloud-cutover-runtime-integration`.

Graph ancestry and patch equivalence are separate. A source branch stays
reachable whenever exact patch containment is incomplete, even when prior
semantic review concluded that its intended behavior was integrated.

| Branch | Head | Exact patch containment | Disposition / release action |
|---|---|---:|---|
| `claude/apply-runtime-triage` | `1e0e4a1` | 3/3 | Archive-only; fully patch-contained. |
| `claude/container-build-gate` | `bf6338a` | 1/1 | Patch-contained by `d1638fb`; container CI gate passed. |
| `claude/resbuild-postgres-authority` | `fdf16af` | 1/1 | Patch-contained by `dde99e3`; archive source branch. |
| `codex/a1-admission-gate` | `c88c263` | 2/2 | Patch-contained; remote archive exists at `myfork/archive/codex-a1-admission-gate-20260717`. |
| `codex/a1-containment-corrections` | `241abc4` | 5/5 | Patch-contained; retain through release tagging and any separately required security signoff. |
| `codex/audit-remediation-runtime` | `93f0c47` | 1/3 | Retain. Concrete patch mappings remain required for `a5f0856` and `0578682`. |
| `codex/fleet-dbhot-live-hotfix` | `706bf42` | 4/7 | Retain. Concrete patch mappings remain required for `9ec4910`, `ddad31f`, and `4fd6746`. |
| `codex/fleet-dbhot-stage1` | `2dfbaa3` | 6/11 | Retain. Five commits remain semantically reviewed but not exactly patch-proven. |
| `codex/fleet-dbhot-stage1-rollback` | `f364bfe` | rollback-only | Preserve remotely; never merge into the forward release. |
| `codex/p0-crash-idempotent-evidence-20260713-a25132` | `5720519` | 0/1 | Archive-only evidence snapshot; do not treat it as the tested containment implementation. |
| `codex/postgres-canonical-brain-phase1` | `6a0e3dc` | 5/5 | Fully patch-contained; archive after release tagging. |
| `codex/runtime-postgres-p0-p1` | `bf3e1d2` | 7/7 | Fully patch-contained; archive after release tagging. |
| `codex/snapshot-python-main-safety-20260716` | `4cde021` | 0/1 | Retain until its six-file snapshot has a concrete commit mapping. |

## Additional live remote heads

The requested 13-row matrix covers local non-ancestor branches. Live remote
inspection also found non-ancestor heads not represented by local source
worktrees: `myfork/applypilot-hardening-and-brainstorm-integration` at
`b4ff785`, `myfork/dev`/`origin/dev` at `81bfccd`, and live `myfork/main` at
`0cfb555`. They remain remote history and are not release inputs.

## Closure state

- The 13 source worktrees were clean at revalidation.
- The canonical branch is pushed with no ahead/behind difference.
- `.wheelcheck/` is an ignored local wheel-verification output and is not a
  release input.
- No final RC4 tag exists yet; source branches must not be deleted until the
  corrected combined release is committed and tagged.
- Incomplete patch-proof rows are conservative retention blockers, not staging
  database or paused-worker blockers.

No worktree or branch may be removed until its required commits are reachable
from a pushed canonical ref or a deliberately retained archive ref.
