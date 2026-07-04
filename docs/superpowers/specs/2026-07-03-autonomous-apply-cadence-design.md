# Autonomous apply cadence + dead-man alert — design

**Approved:** 2026-07-03 (owner chose full-autonomous apply, spend-cap-only throttle). Redirect
target after Build 12 (KG guard) was found already-built. Addresses the 7/02 audit's #1 leverage
item ("execution, not selection") + its "who watches the watcher" gap.

## Problem
The other session's `register-fleet-tasks.ps1` already schedules discover (DiscoveryScrape m2 →
DiscoveryIngest home), score (ComputeScore m4 ← ComputeIngest home), outcomes (OutcomeScan 6h),
and self-heal (Doctor 5m / Watchdog). THREE stages of the audit's chain are NOT on any cadence:
1. **verify-live** — no scheduled liveness re-check (~15% of apply-eligible jobs are dead).
2. **apply-queue push+approve** — nothing moves freshly-scored gate-passers into `apply_queue`
   and approves them, so new inventory never reaches the appliers automatically.
3. **dead-man alert** — the register script only PRINTS a manual psql check; nothing fires when
   the fleet goes silent (it sat dead 2.5 days undetected).

## Verified cap semantics (the autonomy throttle — spend-cap-only per owner)
- `cost_cap_daily_usd` (live: **$100**) → `_cost_cap_exceeded` (queue.py:293) checks
  `SUM(llm_usage.cost_usd) WHERE ts >= now()-interval '24 hours'` — a ROLLING 24h window that
  SELF-RESETS as spend ages out. `llm_usage` is populated (2,371 rows live). This is the primary
  continuous-autonomy throttle: apply until 24h spend ≥ $100 → auto-pause → auto-resume as spend
  ages out. No manual reset needed.
- `spend_cap_usd` (live: **$250**) → checked vs `SUM(apply_queue.est_cost_usd)` (CUMULATIVE, only
  grows; live $166.96). Hitting it pauses the apply lane **permanently** until manually raised — a
  hard lifetime backstop. The DeadMan MUST warn as this approaches, else the permanent pause looks
  like a silent death.
- `cost_cap_total_usd` (live: $0 = uncapped). Optional lifetime llm_usage cap; not relied on.

## Components (all NEW, additive; nothing existing is modified)

### A. VerifyLive scheduled task (home, every 6h)
Wrapper runs `run-applypilot.ps1 verify-live` (owner env, live brain, backs up). Re-checks
liveness of apply-eligible jobs so dead postings are filtered before the ApplyCycle pushes them.

### B. ApplyCycle scheduled task (home, every 4h) — the autonomous loop
A wrapper that runs, in sequence, the existing `applypilot-fleet-apply-home` subcommands against
the live PG:
1. `push --score-floor 7 --include-research` — move fresh live gate-passers (offsite, canonical or fleet research score≥7) into `apply_queue`.
2. `approve --all-pushed` — stamp `approved_batch` so they are leasable (push alone does NOT).
3. Ensure the canary is NOT the blocker: `lift-canary` (owner chose spend-cap-only, so the
   canary one-time gate is disabled; the rolling daily cap is the throttle).
4. **Guarded self-resume** (required for true autonomy — the `paused` flag is STICKY, only ever
   SET by the lease gate, never auto-cleared): if `queue._cost_cap_exceeded(conn)` is False, clear
   a plain pause — `UPDATE fleet_config SET paused=FALSE WHERE id=1 AND ats_paused=FALSE`. This
   self-resumes the fleet after a rolling-cap window frees capacity. **HARD SAFETY RULE: never
   touch `ats_paused`/`ats_pause_source` (Doctor-owned, H8 catastrophe guard) or any LinkedIn halt
   — the `AND ats_paused=FALSE` guard means a Doctor safety pause is NEVER overridden.** If the
   daily cap IS currently exceeded, leave `paused` as-is (the cap owns it; it resumes next cycle
   once 24h spend ages out).
5. `pull` — sync terminal results back into the brain (idempotent; never demotes an apply).
The already-running FleetAgent workers do the applying. Cadence = every 4h keeps the queue full,
approved, liveness-fresh, and self-resumed whenever the rolling window has headroom.

### C. DeadMan scheduled task (home, every 20 min) — the circuit breaker
Reads the fleet PG (SELECT-only) and raises an ALERT on any of:
1. **Silent death:** fleet armed (`paused=false AND ats_paused=false`) AND
   `MAX(worker_heartbeat.last_beat) < now()-interval '30 min'` (no live worker) — the 2.5-day-dead
   signal.
2. **Stalled queue:** `apply_queue` has `queued`+`approved` rows AND zero `status='applied'` in the
   last 3h while armed (workers running but nothing progressing).
3. **Spend running hot:** the lifetime `spend_cap_usd` cap was REMOVED by the owner (2026-07-03,
   set to 0) — the rolling `cost_cap_daily_usd` ($100/24h) is now the only throttle. So instead of
   warning on a lifetime ceiling: the DeadMan ALWAYS surfaces cumulative total spend
   (`SUM(llm_usage.cost_usd)`) on the console, and ALERTS if rolling-24h spend ≥ 95% of
   `cost_cap_daily_usd` for ≥2 consecutive checks (the fleet is pinned at the daily ceiling =
   spending at max rate continuously — the owner should know, since there is no lifetime backstop).
4. **Watchdog/Doctor dead:** their heartbeats stale (they are the self-heal; if they die, nothing
   heals).
Alert delivery on a headless box (no email infra): (a) write a `doctor`/`ats_paused`-independent
alert row/flag into `fleet_config` (a new `deadman_alert` TEXT column, additive) that the Build-11
console surfaces as a RED banner the owner sees from his phone; (b) write a timestamped
`%LOCALAPPDATA%\ApplyPilot\fleet-ALERT.txt`; (c) best-effort Windows toast (BurntToast if present,
else a no-op — never crash the check). DeadMan CLEARS the alert when all conditions are healthy.
DeadMan NEVER pauses/halts anything itself (advisory only — it watches, the owner/Doctor act).

### D. register-apply-cycle.ps1 (ops, owner-run once)
A SEPARATE registration script (NOT edited into the other session's `register-fleet-tasks.ps1` —
avoids a shared-branch collision on an actively-evolving file). Registers A/B/C as home Task
Scheduler tasks using the same idempotent unregister-then-register + wrapper pattern
register-fleet-tasks.ps1 uses. `-Unregister` removes them.

## Error handling
- Any subcommand failure in ApplyCycle logs loudly and continues to the next cycle (a failed push
  must not wedge the cadence); the wrapper exits non-zero so Task Scheduler's Last-Result shows it.
- DeadMan is READ-ONLY on the fleet + best-effort on delivery; a delivery failure (no BurntToast)
  degrades to file+console, never crashes.
- All tasks are home-only + StartWhenAvailable (survive a missed window / the box's power-cuts).

## Testing
- Python: a pure `deadman_check(pg_conn, *, now) -> list[Alert]` (in a new fleet module) unit-tested
  against a seeded disposable PG (fleet_db fixture): stale-heartbeat→alert, fresh→none,
  90%-of-spend_cap→alert, stalled-queue→alert, all-healthy→clears. The alert-DELIVERY (file/console/
  toast) is thin and file-based, tested by asserting the file + the fleet_config flag write.
- Console: the RED banner renders when `deadman_alert` is set (extend the Build-11 console + a smoke
  test); token-gating unaffected (it's a GET/status field).
- PowerShell: parse-gate register-apply-cycle.ps1 (owner runs it; not executed from the sandbox).

## Success criteria
- After the owner runs `register-apply-cycle.ps1`, the fleet keeps `apply_queue` full + liveness-
  fresh + approved on a 4h cadence, applies continuously under the rolling $100/day cap, and the
  owner gets a console red banner within ~20 min of any silent death / stall / approaching-lifetime-
  cap — the failure mode that went undetected for 2.5 days.

## Non-goals
No change to `register-fleet-tasks.ps1`, the Doctor, Watchdog, `fleet_sync.py` push logic, or the
canary/approval primitives. No new apply mechanism — only scheduling of existing subcommands + a
read-only watcher. Not a replacement for the Doctor (that heals; DeadMan only alerts on what the
Doctor can't fix or when the Doctor itself is dead).
