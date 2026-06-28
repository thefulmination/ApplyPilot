# Fleet Apply Lane A — Offsite-ATS Go-Live — Design Spec

**Date:** 2026-06-27
**Status:** design, pending review (adversarially critiqued by a 4-lens panel — safety-gates / apply-contract / completeness / scope-coupling; 6 blockers found and folded in)
**Repo:** `New project/ApplyPilot` (Python tool)
**Depends on:** the fleet v3 foundation (`fleet/queue.py`, `fleet/worker.py`, `fleet/governor.py`, `fleet/sync.py`, the watchdog lane) + the proven live apply path (`apply/launcher.py::run_job`, the env contract in `apply/container_worker.py`). See [`2026-06-26-distributed-residential-fleet-design.md`](2026-06-26-distributed-residential-fleet-design.md) and [`2026-06-27-fleet-watchdog-monitoring-design.md`](2026-06-27-fleet-watchdog-monitoring-design.md).

## 1. Goal & success criteria

Make the distributed fleet **submit offsite ATS applications** (Greenhouse/Lever/Ashby/Workday — never LinkedIn), behind safety gates the owner signs off on. The live apply path (`launcher.run_job` — Playwright + AI agent, confirmation-required-before-APPLIED) and most of the v3 apply queue layer exist and are tested; the gaps are (a) **the wiring** (no entrypoint injects a real `apply_fn`), (b) **a worker-loop contract change** the original wiring assumption missed, and (c) **five safety-gap fixes** the critique surfaced.

**Done when:** (a) `applypilot-fleet-apply` runs a per-slot apply loop whose `apply_fn` wraps `launcher.run_job`, an e2e test (seeded Postgres, stub `apply_fn`) proves a leased+approved offsite job is applied AND a `failed:no_result_line` becomes `crash_unconfirmed` (never re-leased); (b) the owner-approval gate is the only path to a leasable job; (c) the canary auto-pause counter halts the fleet after at most K **lease attempts**, fleet-wide, proven atomic under concurrency; (d) the apply lane respects `should_halt`/`paused` at BOTH the worker loop and the lease; (e) the apply lane's cost gate uses a ledger it actually writes; (f) `applied_set` is backfilled from home apply-history so the fleet never re-applies a home-applied job; (g) the home `push`/`approve`/`pull`/`canary` driver is wired; (h) a canary runbook + the v1-fleet-off precondition are documented.

**Non-goals (Sub-project A):** the LinkedIn lane (`_tick_linkedin`, `lease_linkedin`, the single-event halt) — deferred to **Sub-project B**, built after A is proven; the broker/friend-machine RPC; resume tailoring (apply as-is); a UI; the fuzzy near-duplicate Jaccard port (an exact `company|title` push exclusion is the A floor; fuzzy port is a B-or-later follow-up).

## 2. Scope boundary — offsite only

Offsite-ATS only. LinkedIn is excluded structurally and stays on the home box / single IP via the supervised path:
- `_PUSH_APPLY_SELECT` filters `application_url NOT LIKE '%linkedin.com%'` (verified sync.py:54) — LinkedIn URLs never enter `apply_queue`.
- `lease_apply` serves only `lane='ats'` (verified queue.py:28); the dormant `lease_linkedin`/`linkedin_queue` scaffold stays untouched in A.
- Fleet workers run a fresh Chrome profile (no `li_at` cookie) → offsite-capable only.

The four lease SQL constants (`_LEASE_APPLY/_COMPUTE/_SEARCH/_LINKEDIN`) are independent (verified) — changes to `_LEASE_APPLY` cannot ripple into the other lanes. Worst case for this lane is a recoverable per-destination ATS block, not an account ban. The LinkedIn ban risk is entirely in Sub-project B.

## 3. Architecture

### 3.1 Worker entrypoint — `applypilot-fleet-apply` (and the worker-loop contract fix)
A new `src/applypilot/fleet/apply_worker_main.py`. **It is NOT a pure mirror of `compute_worker_main`** — the apply path needs three things compute doesn't:

1. **The full env contract, set BEFORE importing `applypilot`.** Reuse/extract `container_worker._setup_env` (container_worker.py:34-50): `APPLYPILOT_BASE_RESUME=1` (apply-as-is, G7), a throwaway `APPLYPILOT_DB_PATH` (so `run_job`'s home-SQLite cost write doesn't crash on a brainless worker), `CHROME_WORKER_DIR`/`APPLY_WORKER_DIR`/`APPLYPILOT_DIR`, the LLM `ANTHROPIC_BASE_URL`/`AUTH_TOKEN`, `APPLYPILOT_AGENT_TIMEOUT`, `APPLYPILOT_LANE_FILTER=0`. Do not just set `BASE_RESUME` — port the whole block.
2. **The `apply_fn` returns `run_job`'s mapped STATUS, not HTML.** The real call is `from applypilot.apply import launcher; launcher.run_job(job, port, worker_id, model, agent) -> (status_str, duration_ms)` (there is NO `container_worker.run_job`). The wrapper does what `container_worker.main` does around the call: `chrome.launch_chrome` → `run_job` → read `launcher._last_run_stats[worker_id]` for real cost → `cleanup_worker` in `finally` → map via `container_worker._map_status(status_str) -> (queue_status, apply_error)`. `apply_fn(job)` returns `(queue_status, apply_status, apply_error, est_cost_usd)` — already classified.
3. **`_tick_apply` gains a status-passthrough branch (worker-loop change — the critical fix).** Today `_tick_apply` runs `classify_fn(html,…)` to *re-derive* the outcome, but `run_job` already classified and returns no HTML — re-classifying empty HTML would write a **phantom `applied`** and mask a crash. Fix: when `apply_fn` returns an explicit terminal status (the new contract), `_tick_apply` **bypasses `captcha.classify`** and routes directly: `applied → write_apply_result(status='applied')`; a wall (`captcha`/`login_issue`) → `park_challenge` + raise `auth_challenge`; `failed:no_result_line`/`failed:timeout`/`worker_error` → `write_apply_result(status='crash_unconfirmed')` (so it enters `applied_set` and is never re-leased); other `failed:*` → `write_apply_result(status='failed')`. `write_apply_result` already takes a status and needs no HTML (verified queue.py:61-102) — only `_tick_apply`'s loop body must change. Also fix the stale `worker.py` module docstring (it wrongly says `apply_fn` wraps `container_worker.run_job` and returns `html`).
4. **The drive loop calls `should_halt` — it is NOT `run_forever`.** `run_forever`/`compute_worker_main` do NOT check `should_halt`; the apply loop must, at the top of each iteration, call `apply.pgqueue.should_halt(conn)` and idle when true (mirroring `container_worker.py:269`), with a short backoff on `action=='error'` (avoid a hot crash loop if Chrome won't launch). This is genuinely new code.

Registered as `applypilot-fleet-apply = "applypilot.fleet.apply_worker_main:main"`.

### 3.2 Home driver — `applypilot-fleet-apply-home`
New `src/applypilot/fleet/apply_home_main.py` subcommands (exact signatures verified):
- `push` — `sync.push_apply_eligible(score_floor=N, approved_batch=None, limit=M)` stages eligible offsite jobs UNAPPROVED. **Plus** runs the `applied_set` backfill (§4.6) so the lease-time dedup sees home history.
- `approve` — generates a batch token (uuid4/ISO timestamp), then `queue.approve_jobs(conn, urls, token)` (positional `batch`, stamps only `status='queued'`). `--all-pushed` first runs `SELECT url FROM apply_queue WHERE status='queued' AND approved_batch IS NULL`, then approves those under one token. **Refuses (or loudly warns) if the canary is not armed** (so the §7 ordering — arm canary, then approve — can't be silently inverted).
- `pull` — `sync.pull_apply_results()` (never demotes a confirmed apply).
- `canary <K>` — `UPDATE fleet_config SET canary_enabled=TRUE, canary_remaining=K, paused=FALSE`. `lift-canary` — `canary_enabled=FALSE, canary_remaining=NULL`.
- `status` — queue depth by status + `paused`/`canary_remaining`/`spend_cap_usd` vs spend, and applied/parked/crash counts, so the owner sees attempts-vs-applies.

### 3.3 The v1-fleet-off precondition (lease-bypass isolation)
v1 and v3 share one `apply_queue` on one Postgres. The v1 `container_worker` leases via `pgqueue.lease_one`, which filters ONLY `status='queued'` — it bypasses approval, canary, paused, cost-cap, and `applied_set`. So a running v1 worker is a second ungated door. The Railway v1 fleet is **decommissioned** (already off), but A's go-live REQUIRES: (a) the runbook states no v1 `container_worker` runs against the apply DB during the canary; (b) **retire/gate `pgqueue.lease_one`** — add the same `approved_batch IS NOT NULL` (and ideally `lane='ats'`) guard to `_LEASE_SQL`, OR have `container_worker` call `lease_apply`, so there is ONE gated door; (c) a test asserts an unapproved row is not leasable by `lease_one` after the gate. (Decision in §8: gate `lease_one` — cheapest durable fix.)

### 3.4 What the watchdog already covers
`pgqueue.reclaim_stale_leases` (safe pre-launch re-queue vs `crash_unconfirmed`), `governor.evaluate_breakers`/`clear_expired_breakers`/`roll_window`, and `_enforce_cap` (the **compute** cap → `paused`) all run on the watchdog tick. No new work. `park_challenge` correctly freezes a walled lease out of reclaim (verified).

## 4. Safety gates

### 4.1 G1 — Offsite-only (BUILT)
`_PUSH_APPLY_SELECT` LinkedIn exclusion + `lane='ats'` lease + fresh-profile workers. Test asserts a LinkedIn URL is not push-eligible. (Residual: an aggregator `application_url` that resolves to LinkedIn at runtime is not literally excluded — see §4.6 aggregator note + §7.)

### 4.2 G2 — Owner-approval gate (lease gate BUILT; driver NEW)
`lease_apply` requires `approved_batch IS NOT NULL` (verified) — unapproved rows are invisible. The new `approve` driver is the only thing that stamps a batch; `push` stages unapproved. The `approved_batch` is a presence stamp, not a signed token — acceptable for a single-owner box (the only PG writer is the owner's home driver); the real residual is accidental over-approval, bounded by G3 and the canary-armed-before-approve check (§3.2).

### 4.3 G3 — Canary auto-pause counter (NEW — the key catastrophe gate; must be ATOMIC)
A fleet-wide hard ceiling on the first lease attempts, enforced in the lease so it cannot be raced or over-approved past.
- New `fleet_config` columns (idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS`, matching the v3 pattern): `canary_enabled BOOLEAN NOT NULL DEFAULT FALSE`, `canary_remaining INTEGER`.
- **The fixture must reset them** (`tests/conftest.py` fleet_config reset adds `canary_enabled=FALSE, canary_remaining=NULL`) or the columns leak across tests → flaky suite.
- **Atomic enforcement at lease — mirror `_LEASE_LINKEDIN`'s lock+reserve, NOT a naive WHERE+UPDATE.** `_LEASE_APPLY` currently has no shared-row lock, so a naive `canary_remaining>0` guard + separate decrement lets two concurrent workers both pass and overshoot. The fix mirrors the proven LinkedIn pattern: add a CTE `cfg AS (SELECT canary_enabled, canary_remaining FROM fleet_config WHERE id=1 FOR UPDATE)` so every apply lease serializes on the single `fleet_config` row; guard `next_job` on `(NOT cfg.canary_enabled OR cfg.canary_remaining > 0)`; and a `reserve AS (UPDATE fleet_config SET canary_remaining = canary_remaining - 1, paused = (canary_remaining - 1 <= 0) WHERE id=1 AND canary_enabled AND EXISTS (SELECT 1 FROM next_job) RETURNING 1)` so the decrement + auto-pause happen under the same lock, only when a job is actually reserved (a no-op lease must not decrement). A concurrency test (N>K workers, K=1) asserts exactly one lease.
- **K bounds lease ATTEMPTS, not confirmed applies** (decrement-at-lease — the safe direction). A captcha-park or a safe pre-launch reclaim+re-lease consumes a slot, so the fleet may pause with fewer than K confirmed applies. The runbook + `status` say "K attempts" and show `canary_remaining` next to applied/parked/crash counts so the owner isn't surprised.
- Owner flow: `canary K` arms; after auto-pause, review; then `canary K` again or `lift-canary`.

### 4.4 G4 + G5 — Halt + cost cap, reconciled to ONE apply-lane mechanism (NEW — gap-fix)
The critique found two *different* cost systems; the apply lane uses the one its spend actually lands in.
- **The apply lane's cost ledger is `apply_queue.est_cost_usd`** (written by `write_apply_result`), NOT the PG `llm_usage` ledger (which only `write_compute_result` writes — so the originally-specified `_cost_cap_exceeded` reuse would have NEVER tripped). The apply-lane spend gate is `apply.pgqueue.should_halt` = `fleet_config.paused OR (spend_cap_usd > 0 AND SUM(apply_queue.est_cost_usd) >= spend_cap_usd)` (verified). The owner sets `spend_cap_usd` via `set_spend_cap`. The v3 `cost_cap_*`/`llm_usage` cap stays the **compute** lane's; this is documented so an operator reading `status` sees one coherent apply-lane spend number.
- **Two enforcement points, both keyed off the same `fleet_config` row:** (a) the worker loop calls `should_halt(conn)` before each lease and idles when true (genuinely new code — §3.1.4); (b) `_LEASE_APPLY`'s `next_job` CTE gains `NOT (SELECT paused FROM fleet_config WHERE id=1)` so a paused fleet serves no apply lease (belt to the worker's suspenders). `paused` is `NOT NULL DEFAULT FALSE`, so the guard is two-valued and transparent for un-armed rows. The canary auto-pause (G3), the watchdog cap-pause, and `should_halt` all converge on `fleet_config.paused`.

### 4.5 G6 — Double-apply guard, extended to cover HOME history (NEW backfill — load-bearing)
The built fleet guards are real but **blind to jobs you already applied to outside the fleet** (the home supervised path / the `applications` ledger). Two fixes:
- **`applied_set` backfill (load-bearing).** `applied_set` is the lease-time R9 dedup but is populated only by fleet writes. The `push`/sync step backfills it (idempotent `INSERT … ON CONFLICT DO NOTHING`) with `dedup_key(company, title)` for every home job that is `apply_status='applied'` OR `apply_error IN ('no_confirmation','crash_unconfirmed')` **AND** every `applications` ledger row with `status='applied'`. Now the lease-time guard `NOT EXISTS (applied_set …)` actually sees home history.
- **Push cross-check the durable ledger.** `_PUSH_APPLY_SELECT` adds the `applications`-ledger exclusion (mirroring home `acquire_job`: `NOT IN (SELECT … FROM applications WHERE status='applied')` on `COALESCE(application_url,url)`), not just `jobs.apply_status` — because "a lost `jobs.apply_status` can't re-open it." Plus an exact `company|title` already-applied exclusion (the SQL floor; the fuzzy Jaccard port is deferred, §1 non-goals).
- The rest is BUILT: `_PUSH_APPLY_SELECT` excludes `apply_status/apply_error` crash states; `lease_apply` excludes `applied_set` dedup; `write_apply_result` UPSERTs `applied_set` on `applied`/`crash_unconfirmed`; `pull` never demotes a confirmed apply; the agent must SEE confirmation; `reclaim_stale_leases` pins a possibly-submitted crash to `crash_unconfirmed` (never re-leased).
- **Residual (documented, §7):** the unresolved-aggregator case (a listing whose real ATS/company is revealed only at runtime) can evade dedup; home defers these (`is_unresolved_aggregator`) and the push does not. A is offsite-push-fast; this residual is acknowledged, and a coarse aggregator-host exclusion at push is a follow-up.

### 4.6 G7 — Apply resume as-is (BUILT) / G8 — Throttle, gap-jitter, breakers (BUILT)
G7: `APPLYPILOT_BASE_RESUME=1` (set by the entrypoint env contract, §3.1.1). G8: `lease_apply` atomically enforces per-host gap jitter + breaker state; the watchdog drives the governor. No new code.

## 5. Error handling
- A crash mid-apply → `reclaim_stale_leases` → `crash_unconfirmed` (never re-leased). A captcha/login wall → `park_challenge` (frozen lease) + an `auth_challenge` row; the worker never auto-resolves. A `failed:no_result_line`/`timeout` → `crash_unconfirmed` (the new status-passthrough, §3.1.3) — never a phantom `applied`. DB blips → reconnect (`conn_factory`). The home driver's commands are idempotent.

## 6. Testing (subagent-driven TDD, against the `fleet_db` disposable Postgres)
Regression baseline (must stay green): `tests/test_fleet_v3_governor_queue.py` (the apply-lease tests) + `tests/test_fleet_v3_worker.py` (the apply-tick e2e). New tests:
- **Worker contract:** `apply_fn` returning `applied` → row `applied` + `applied_set` UPSERTed; `apply_fn` returning `failed:no_result_line` → `crash_unconfirmed`, NOT `applied`, and not re-leasable; a wall → parked. (Proves the status-passthrough bypasses `captcha.classify`.)
- **G2:** unapproved queued row not leasable; after `approve` it is.
- **G3 (concurrency):** `canary_enabled, canary_remaining=1`, two concurrent lease attempts → exactly ONE lease, the other None, `fleet_config.paused=TRUE`; a no-op lease (no job) does not decrement; `lift-canary` re-enables. + the conftest canary reset.
- **G4/G5:** `paused=TRUE` → `lease_apply` None AND the worker loop idles via `should_halt`; `spend_cap_usd` breached (SUM(apply_queue.est_cost_usd)) → `should_halt` true.
- **G6:** a `dedup_key` present in `applied_set` (incl. backfilled-from-home) is not leasable; a home-`applications`-applied job is excluded at push; `pull` does not demote a confirmed apply.
- **v1 isolation (§3.3):** after gating `lease_one`, an unapproved row is not leasable by `lease_one`.
- **Home driver:** `push`(+backfill)/`approve`(token + `--all-pushed` + canary-armed check)/`pull`/`canary`/`lift-canary`/`status` each do the right DB effect.
- Full fleet suite stays green.

## 7. Owner-run (canary runbook) + residual risk
`docs/fleet-apply-lane-runbook.md`: (0) **confirm no v1 `container_worker`/Railway fleet runs against the apply DB**; run on the home Postgres; the watchdog must be running. (1) `apply-home pull` first (ingest any prior results so the brain-level exclusion + backfill are authoritative). (2) `apply-home push --limit N` (stages unapproved + backfills `applied_set`). (3) `apply-home canary 3` (arm the ceiling) THEN `apply-home approve --all-pushed` (the approve refuses if canary isn't armed). (4) start ONE `applypilot-fleet-apply --worker-id w1` on the home box; it makes ≤3 lease **attempts** then auto-pauses. (5) `apply-home pull` + review (submitted? right resume? confirmation seen? any phantom?). (6) `apply-home canary 10` for a wider round, or `lift-canary` for full volume + set `spend_cap_usd`.

**Residual risks (acknowledged):** `approved_batch` is a presence stamp (single-owner box); the unresolved-aggregator double-apply case is not caught by dedup (offsite-push-fast accepts it; a coarse aggregator exclusion is a follow-up); a per-destination ATS block is possible and recoverable (the governor breaker throttles that host, not the account); the fuzzy near-dup Jaccard port is deferred. **LinkedIn ban risk is entirely out of scope for A.**

## 8. Decided questions
- Scope = offsite-ATS only; LinkedIn → Sub-project B. **Decided (owner).**
- G3 canary = a fleet-wide auto-pause counter, **atomic via a `fleet_config FOR UPDATE` lock + reserve CTE** (mirroring `_LEASE_LINKEDIN`), decrement-at-lease (bounds attempts). **Decided (owner + critique).**
- G4+G5 reconciled: the apply-lane cost/halt gate is `should_halt` (`spend_cap_usd` vs `SUM(apply_queue.est_cost_usd)`) + a lease paused-guard, NOT the `llm_usage`-based `_cost_cap_exceeded`. **Decided (critique).**
- G6 extended with an `applied_set` backfill from home apply-history + a push `applications`-ledger cross-check. **Decided (critique).**
- The apply_fn returns `run_job`'s mapped status; `_tick_apply` gets a status-passthrough branch (bypass `captcha.classify`). **Decided (critique).**
- The apply entrypoint ports `container_worker._setup_env` + runs an explicit `should_halt` loop (NOT `run_forever`). **Decided (critique).**
- `pgqueue.lease_one` is gated (`approved_batch IS NOT NULL`) so there is one gated door; v1 Railway fleet stays off during the canary. **Decided (critique).**
- Sub-project B (LinkedIn lane) is a separate spec after A is proven. **Decided (owner).**
