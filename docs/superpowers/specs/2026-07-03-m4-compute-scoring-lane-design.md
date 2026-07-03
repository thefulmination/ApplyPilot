# m4 compute/scoring lane — design + bring-up

Date: 2026-07-03. Status: BUILT + verified end-to-end (push 10 → score 3 @ $0.0018 → pull 3).

## Goal

Use m4's capacity to run the expensive LLM score/audit pass off the home laptop. Mirrors
the discovery lane's shape (worker script + home feed/harvest loop + scheduled tasks).

## What the lane does (accurately)

`push_compute_eligible` (sync.py) selects brain jobs with
`COALESCE(audit_score, fit_score) >= score_floor` (default 7), best-first → `compute_queue`.
So this is the **paid second-opinion pass on already-promising jobs**, NOT the raw unscored
backlog — the right use of API budget (don't spend LLM on jobs the cheap scorer ranked low).
Results are **advisory** (`research_fit_score`/`research_decision`; never demote `fit_score`).

## Components (all new this change)

- **`run-fleet-compute.ps1`** (m4): launches N=5 `applypilot-fleet-compute` workers, ids
  `m4-score-0..4`. IP-free. Loads `~/.applypilot/.env` for the DeepSeek key and REFUSES to
  start if `config.get_tier() < 2` (tells the operator to copy the home `.env`). Mirrors
  `run-fleet-discovery.ps1` (multi-window `-Index` child spawn, kill-respawn clean slate).
- **`run-compute-home-loop.ps1`** (home): each cycle `push` (fill queue) + `pull` (harvest).
  Resolves the LIVE brain path with the same refuse-if-missing guard as the discovery loop.
- **`register-fleet-tasks.ps1`**: `ComputeScore` task on m4 (5 scorers, self-heal hourly),
  `ComputeIngest` task on home (push+pull every 15 min). Added to `Get-TaskNamesForMachine`
  so `-Unregister` cleans them too. Verify-checklist entries for home + m4.

## Cost + safety (verified)

- Compute is gated by `fleet_config.cost_cap_daily_usd` / `cost_cap_total_usd` vs actual
  `llm_usage` (`queue._cost_cap_exceeded`) — **SEPARATE** from `spend_cap_usd` (apply/LinkedIn).
  Raising the compute cap does NOT loosen the apply ceiling. Set `cost_cap_daily_usd`=100
  (runaway guard; real cost is pennies — 3 jobs = $0.0018). `spend_cap_usd`=250 untouched.
- Compute leases gate ONLY on cost caps — NOT on `fleet_config.paused`. So scoring runs even
  while the apply lane is paused (it is today). m4 APPLY workers, by contrast, idle until the
  owner unpauses. Documented in the m4 verify checklist.
- IP-free: no browser, no site traffic → no apply-IP hygiene, safe over Tailscale.

## Bring-up (operator, on m4)

m4 is an existing apply box (`GGGTOWER`, desired_workers=2). One-time:
1. Copy the home box's `.applypilot\.env` (holds `DEEPSEEK_API_KEY`) to m4's `~/.applypilot\.env`.
2. `cd C:\ApplyPilot; git stash (if dirty); git pull`
3. `.\register-fleet-tasks.ps1 -Machine m4`  → registers FleetAgent (apply, `-AutoUpdate`)
   + ComputeScore (5 scorers). On HOME, re-run `-Machine home` once to add ComputeIngest.

## Non-goals (v1)

- No fleet_desired_state control of compute worker count (fixed at 5 via the task; edit the
  wrapper/`-Workers` to change). FleetAgent manages APPLY only; `m4-score-*` ids deliberately
  don't match its `<Label>-<digits>` regex.
- No pause-gating of compute (existing R14 design: cost-cap only). If you want "pause stops
  scoring too", that's a separate change to `lease_compute`.
- Ensemble/multi-provider off (DeepSeek only); flip via `-Providers` / `.env`.
