# res_build → apply-gate bridge (v1) — runbook

**What it does.** Puts *your* review work (the res_build fit map / the jobs you reviewed and
kept) into ApplyPilot's apply gate. Until now the apply pipeline selected jobs purely by
`COALESCE(audit_score, fit_score)` — DeepSeek's `fit_score` + ApplyPilot's deterministic audit
— and used **none** of res_build's output. This bridge promotes your kept jobs by writing
`audit_score` + `decision_source='res_build'`, which the gate already prefers, so they get
selected — **including the ones ApplyPilot's own ranker scores below the apply threshold.**

**Safety properties**
- **Reversible.** Every real promote writes a snapshot of each touched row's prior state first;
  `-Revert` restores it. Revert only touches rows still tagged `decision_source='res_build'`.
- **LinkedIn excluded by default.** The LinkedIn lane is separate/supervised; v1 promotes the
  offsite-ATS subset only.
- **Applyable-only.** Rows already applied, or marked `duplicate_of_url`, are skipped.
- **Inert until you run the fleet.** Promotion only *stages* jobs as apply-eligible in the
  brain. Nothing applies until you push to the fleet / run `applypilot apply`.
- **Never double-applies.** Reuses `import_decisions`, which guards on `applied_at IS NULL`.

## Run it (from `New project\ApplyPilot`)

```powershell
# 1) PREVIEW — no writes. See how many promote and how many the bridge unlocks.
.\bridge-resbuild.ps1 -DryRun

# 2) FIRST CANARY — promote just the top 15 by your own score, end-to-end.
.\bridge-resbuild.ps1 -Limit 15

# 3) Verify they're now apply-eligible (read-only count of promoted rows in the live brain):
.\.conda-env\Library\bin\sqlite3.exe -readonly "$env:LOCALAPPDATA\ApplyPilot\applypilot.db" `
  "SELECT COUNT(*) AS promoted, ROUND(AVG(audit_score),1) AS avg_score FROM jobs WHERE decision_source='res_build';"

# 4) Stage to the fleet when ready (this is what actually queues them to apply):
.\run-applypilot.ps1 ... # your normal apply path, e.g. applypilot-fleet-apply-home push

# 5) PROMOTE THE FULL offsite set once you're happy:
.\bridge-resbuild.ps1

# REVERSE the last promote at any time:
.\bridge-resbuild.ps1 -Revert
```

### Knobs
- `-Limit N` — promote only the top-N by **your** score (smallest safe batch first). `0` = all.
- `-Decider human|model|either` — whose decision to export. `human` (default) = the jobs **you**
  personally kept. `model` = res_build's ranker picks. `either` = union.
- `-ExcludeHost a.com b.com` — hosts to skip (default `linkedin.com`).
- `-IncludeApplied` — also consider already-applied/duplicate rows (default: skip).
- `-DryRun` — preview only.

### Direct CLI (if you don't want the wrapper)
```powershell
# export (in res_build):
node_modules\.bin\tsx src/cli/applypilotExportApplyList.ts --decider=human --out=apply-list.jsonl
# promote (in ApplyPilot, via run-applypilot so it hits the live brain + backs up):
.\run-applypilot.ps1 resbuild-promote apply-list.jsonl --snapshot apply-list.snapshot.json --limit 15
.\run-applypilot.ps1 resbuild-revert apply-list.snapshot.json
```

## What it does NOT do (by design)
- It does **not** swap ApplyPilot's ranker wholesale. Your own ranker experiments showed the
  res_build score doesn't beat the production ranker globally (the ~0.80 ceiling is label noise,
  43.5% pairwise flip). This is a **targeted override** for the jobs you explicitly approved —
  especially the ones the exp-axis ranker buries — not a global re-rank.
- It does **not** apply anything by itself. Staging ≠ applying.
- It does **not** touch LinkedIn (catastrophe lane) unless you remove it from `-ExcludeHost`.

## Measured on the live brain (2026-06-30, decider=human)
- 468 jobs you kept → **206 offsite-applyable** after LinkedIn-exclude + applyable-only.
- **134 of those score below the apply threshold (7)** today — i.e. the standard ApplyPilot
  ranker would never apply them, but you marked them apply. That gap is what this bridge closes.
