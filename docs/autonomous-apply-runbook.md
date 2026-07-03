# Autonomous apply cadence — runbook

Turns the fleet into a self-running apply loop: it keeps the queue full + liveness-fresh, applies
continuously under the rolling daily cap, self-resumes after a cap window, and alerts you on your
phone if it goes silent. Three home scheduled tasks fill the gaps the other scheduler
(`register-fleet-tasks.ps1`) left open.

## Go-live (owner, on the home box)

Run once from an **elevated** PowerShell:
```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
.\register-apply-cycle.ps1
```
This registers three tasks (idempotent; re-run anytime; `-Unregister` removes them):

| Task | Cadence | What it does |
|---|---|---|
| **ApplyPilot VerifyLive** | 6h | `verify-live` — marks dead postings so applies skip them |
| **ApplyPilot ApplyCycle** | 4h | verify-live → push → approve → lift-canary → **resume-if-safe** → pull. Best-effort (runs all steps; a transient failure doesn't skip the cycle). Logs to `.fleet-logs\apply-cycle.log`. |
| **ApplyPilot DeadMan** | 20 min | read-only watcher; raises alerts (below). Logs to `.fleet-logs\deadman.log`. |

The apply *workers* run via the existing **FleetAgent** task (from `register-fleet-tasks.ps1`) — this
build just keeps them fed + resumed. Register both.

## The only throttle: the rolling daily cap
The lifetime cap (`spend_cap_usd`) was **removed** (set to 0). The sole throttle is
`cost_cap_daily_usd` — a **rolling 24-hour** cap (currently **$100**) that self-resets as spend ages
out. Apply until 24h spend ≥ cap → auto-pause → auto-resume next cycle once spend ages out. Change it:
```powershell
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" "host=localhost dbname=applypilot_fleet user=postgres" -c "UPDATE fleet_config SET cost_cap_daily_usd=150 WHERE id=1;"
```
At ~$0.60/apply, $100/day ≈ 165 applies/day of capacity.

## The DeadMan — your safety net (there is no lifetime cap now)
Every 20 min it checks the fleet and, on any of these, sets a **red banner on your phone console**
(`run-fleet-console.ps1` → the `?token=` URL) + writes `%LOCALAPPDATA%\ApplyPilot\fleet-ALERT.txt`
+ attempts a Windows toast:
- **silent_death** — fleet armed but no apply-worker heartbeat in 30 min (the 2.5-day-dead signal).
- **stalled_queue** — approved backlog exists but zero applies in 3h.
- **selfheal_dead** — the Watchdog OR the Fleet Doctor (the self-healers) is itself down.
- **running_hot** — rolling-24h spend ≥ 95% of the daily cap for 2+ checks (spending at max, and
  there's no lifetime backstop, so you should know).
It CLEARS the banner automatically when healthy. It is **advisory** — it never pauses/halts/resumes
anything itself.

## Safety guarantees
- **resume-if-safe never overrides a safety pause.** It clears only a *plain* `paused` (single atomic
  `UPDATE ... WHERE paused=TRUE AND ats_paused=FALSE`), only when the cap isn't exceeded. A Doctor
  `ats_paused` or a LinkedIn halt is **never** touched.
- **Never double-applies** (dedup + applied_at guards, unchanged).
- **Fleet/offsite push logic, the Doctor, and register-fleet-tasks.ps1 are byte-unchanged.**

## Stop autonomy
```powershell
.\register-apply-cycle.ps1 -Unregister
& "C:\Program Files\PostgreSQL\18\bin\psql.exe" "host=localhost dbname=applypilot_fleet user=postgres" -c "UPDATE fleet_config SET paused=TRUE WHERE id=1;"
```

## Current live state (2026-07-03, verified)
`spend_cap_usd=0` (removed), `cost_cap_daily_usd=$100`, rolling-24h spend **$31.51**, fleet
`paused=True` (ApplyCycle's resume-if-safe will clear it on first run — ats_paused is False and the cap
isn't exceeded). Live DeadMan check = **healthy** (paused = not a failure; both self-healers fresh).
