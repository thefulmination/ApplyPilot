# Fleet pull-updater (Windows, between-jobs) — design

Date: 2026-07-03. Status: approved by owner (between-jobs semantics confirmed).

## Problem

Code reaches worker boxes (m2/m4) by one-time `git clone` at setup; nothing keeps them
current. A `git push` from the home box today changes nothing on any machine until an
operator manually pulls + restarts. The audit (2026-07-03) confirmed: `self_update`
remote command is scaffold-only, no version enforcement, no automated pull path.

## Decision

Extend `fleet-agent.ps1` — the ONE actuator that already starts/stops local apply
workers — with an **opt-in** auto-update loop. No second actor touches workers, so the
single-actuator invariant holds.

- `-AutoUpdate` switch + `-UpdateEverySec 900` (15 min checks).
- Registered wrappers pass `-AutoUpdate` on **non-home** machines only. The home box is
  the dev origin: it pushes, never pulls. (Its dirty/ahead tree would no-op anyway; the
  flag makes intent explicit.)
- Update = `git fetch` the current branch's upstream remote (worker boxes cloned
  `applypilot-private` as `origin`; home names it `private` — so the branch upstream is
  resolved, never hardcoded), then **fast-forward only**.

## Between-jobs gate (the confirmed semantic)

An update may only stop/restart workers when this box is between jobs:

1. `worker_heartbeat`: no row `worker_id LIKE '<Label>-%'` with `state <> 'idle'` and
   `last_beat` within 150s (covers apply `m2-0` AND discovery `m2-disc-0` ids).
2. `apply_queue` / `linkedin_queue`: no live lease **held by a live worker**
   (`lease_owner LIKE '<Label>-%'`, `status='leased'`, `lease_expires_at > now()`,
   expiry within a sane horizon (`< now()+1 day`), AND the owner has a fresh heartbeat).
   Live-verified 2026-07-03: challenge-PARKED rows hold leases ~10 years out by design
   (m2 had 105 of them from workers dead since 6/30) and orphaned leases have no process
   to interrupt — neither may block, or the gate would never open.

Implemented in `src/applypilot/fleet/update_gate.py` (PG-tested) + thin
`fleet-agent-update-gate.py` script (mirrors `fleet-agent-query.py`). Polarity is
**fail-closed**: any DB error → BUSY → no update. (The desired-state query is fail-open
KEEP by design; updating blind is the opposite risk, so the gate inverts polarity.)

While an update is pending, the gate is re-checked every agent tick (~20s) to catch
idle windows; the fetch/compare itself only runs every `UpdateEverySec`.

## Update procedure (once gate passes)

1. Stop local apply workers (they are between jobs by gate definition).
2. `git merge --ff-only <remote>/<branch>`. Refuse (log, skip) if tree dirty or history
   diverged — worker boxes must never own local commits.
3. If `pyproject.toml` changed in old..new: `pip install -e .` (editable installs make
   this the only reinstall trigger). Pip failure → LOUD log, continue (old deps + new
   code beats dead workers; operator sees the log + console).
4. If the agent's own files changed (`fleet-agent.ps1`, `fleet-agent-query.py`,
   `fleet-agent-update-gate.py`, `src/applypilot/fleet/update_gate.py`): log + `exit 1`.
   The FleetAgent scheduled task (RestartCount 10 / 1 min) relaunches it on new code;
   its first reconcile respawns workers. Manual (non-task) runs must relaunch by hand —
   logged explicitly.
5. Otherwise workers respawn on the next reconcile tick (same loop iteration order:
   update runs BEFORE reconcile, so respawn is immediate).

Logging: timestamped append to `.fleet-logs\fleet-agent-update.log` + console.

## Staleness bounds

- Apply workers: new code within ~15 min of push + first idle window.
- Discovery workers (not agent-managed): the pull updates the working tree; scrapers
  pick it up at their 6-hourly `DiscoveryScrape` respawn. Bound: ≤6h. Acceptable —
  scrapers are stateless and PG-driven.
- fleet-agent itself: next self-update exit → ≤1 min relaunch.

## Known residual race (accepted, bounded)

Between the gate's IDLE verdict and `Stop-Process`, a worker could lease a job (~ms
window). Consequence: kill mid-lease → lease expires (1200s) → requeue; the existing
crash_unconfirmed / double-apply guards own this path. Full closure = worker-side
`drain` via remote_commands (separate task, next in queue). Not a data-loss vector.

## Rollout (chicken-and-egg, once per box)

The updater cannot deliver itself. Per worker box, once:

1. `git pull` by hand (brings in fleet-agent.ps1 with -AutoUpdate + the gate files).
2. Re-run `register-fleet-tasks.ps1 -Machine <label>` — regenerates the FleetAgent
   wrapper so it passes `-AutoUpdate`. Existing registered tasks keep their OLD wrapper
   until this re-run; the flag does not appear by magic.

After that, all future code reaches the box automatically.

## Reviewed residual risks (adversarial review 2026-07-03)

- Gate-pass→kill race (~ms): a mid-kill lease is reclaimed by the running watchdog's
  reclaim leg (stuck detection + reclaim); crash_unconfirmed guards own the outcome
  path. Accepted.
- `pip install -e .` under a running-but-idle discovery worker: mid-SCRAPE is blocked by
  the gate (non-idle heartbeat); an idle lease-loop process keeps its loaded modules and
  is respawned fresh at the next 6-hourly DiscoveryScrape trigger. Accepted.
- Chromium orphan risk on Stop-Process is identical to the pre-existing gen-bump /
  scale-down kill paths (updater kills only gate-verified-idle workers, which is
  strictly safer); the existing supervisor orphan cleanup owns it.
- Two agents on one box (manual + scheduled) is a pre-existing operational hazard
  ("run this ONCE" contract), unchanged by this feature.

Follow-up worth doing (separate task): report git short-SHA as `sw_version` on the
heartbeat so the console shows exactly which commit each box runs.

## Non-goals (v1)

- No canary version pinning / staged rollout (2-machine fleet; YAGNI — revisit at scale).
- No `self_update` remote-command handler (superseded by pull model; command channel
  wiring for restart/pause/drain is the NEXT task).
- No jobspy special-case at update time (setup-time concern; `--no-deps` pin unchanged).
- No home-box auto-update.
