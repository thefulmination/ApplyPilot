# Fleet Console Operations Runbook

This runbook is for the LAN-only dashboard served by `run-fleet-console.ps1`.
It is intentionally read-first: use the console to diagnose, then run live worker
or canary commands manually from the relevant lane runbook.

## Start Or Verify The Console

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
.\run-fleet-console.ps1
```

The header should show:

- `console <branch>@<commit>`
- `schema telemetry=ok audit=ok`
- `worker_versions ...`

If the commit is not the one you expect, the console is running from the wrong checkout.

## Read-Only Smoke Checks

```powershell
$base = "http://127.0.0.1:8787"
Invoke-RestMethod "$base/api/status"    | ConvertTo-Json -Depth 6
Invoke-RestMethod "$base/api/diagnosis" | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/api/agents"    | ConvertTo-Json -Depth 8
Invoke-RestMethod "$base/api/audit"     | ConvertTo-Json -Depth 6
```

Expected healthy console signs:

- `/api/status.deployment.console.commit` matches the checkout you started.
- `/api/status.deployment.schema.agent_telemetry` is `true`.
- `/api/agents.workers[*].machine_display_name` shows names like `TARPON`,
  `GGGTower`, `Home`, and `Paloma Mac`.
- `/api/audit.rows[0].action` includes `console_start` after a restart.

## Agent Routing And Model Telemetry

The `Agent Routing` section answers which apply agent/model is active:

- `Agent`: `codex`, `claude`, or another configured provider.
- `Model`: the live model name, for example `sonnet`.
- `Chain`: the dynamic fallback order, for example `codex>claude`.
- `Version`: the worker checkout version that produced the heartbeat.
- `Switch`: `startup` or `switch:<from>-><to>` when dynamic switching occurred.

If the verdict is `telemetry_missing`, the schema is installed but live workers
are not reporting the telemetry fields. Confirm `Version` first:

- Mixed, dirty, or old versions mean the workers need a controlled redeploy/restart.
- Version present but telemetry still missing means the worker was started from code
  that predates `WorkerLoop.set_agent_telemetry()` or was not restarted after deploy.

Do not infer Codex-vs-Claude from logs when this section says telemetry is missing.
Redeploy/restart the apply workers, then wait for fresh heartbeats.

## Version Drift

The header's `worker_versions` list is the fastest drift check. A clean fleet should
converge to one current version per role. Treat these as action items:

- `(unknown)`: worker did not report `sw_version`; restart after deploying current code.
- `.dirty`: worker checkout has uncommitted local edits; inspect before trusting it.
- Multiple commit hashes: fleet is split across deployments; reconcile before comparing
  worker performance.

Use the existing worker/task runbooks for live restart or registration steps. The
console itself should not auto-restart LinkedIn workers or re-arm canaries.

## Machine Health

`Machine Health` groups heartbeat rows by physical machine name. A stale count means
the worker row exists but has not beaten recently.

- `alive_workers` high and `stale_workers=0`: machine is currently reporting.
- `stale_workers>0`: restart only the affected worker stack after checking whether it
  is between jobs.
- `Paloma Mac` stale: use `docs/fleet-mac-worker-runbook.md`.
- `TARPON`, `GGGTower`, or `Home` stale: use the Windows fleet worker scripts and
  scheduled-task runbooks.

## Browser Health

Browser Health classifies recent worker log failures and links to the affected
worker logs. Use the log link first; it selects the worker in the log viewer without
running any action.

If a failure repeats on one worker, restart that worker stack. If it repeats on one
host across workers, pause or quarantine according to the Doctor/monitor runbooks.

## Why Not Applying

Read `Why Not Applying` before restarting workers:

- `ready_to_apply`: ATS workers have leaseable work; lack of applies is a worker/runtime issue.
- LinkedIn queued but `linkedin_canary_remaining=0`: this is the correct safety block.
  Re-arm only if you explicitly want LinkedIn Easy Apply active.
- ATS leaseable is zero but queued/approved is nonzero: check canary, dedup, pause, and
  governor rows before touching workers.

LinkedIn canary operations belong in `docs/fleet-linkedin-lane-runbook.md`.
ATS canary and approval operations belong in `docs/fleet-apply-lane-runbook.md`.
