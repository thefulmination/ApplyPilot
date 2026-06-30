# Fleet Apply Lane B — LinkedIn — Design Spec

**Date:** 2026-06-28
**Status:** design, pending review (4-lens adversarial critique folded in — 7 blockers fixed)
**Repo:** `New project/ApplyPilot` (Python tool)
**Depends on:** Sub-project A (offsite apply lane — BUILT: the `apply_fn` status-passthrough contract, the canary/approval pattern, `should_halt` drive loop, the shared `applied_set` + `backfill_applied_set`), the fleet v3 foundation, and the watchdog lane. See [`2026-06-27-fleet-apply-lane-A-offsite-design.md`](2026-06-27-fleet-apply-lane-A-offsite-design.md).

## 1. Goal & success criteria

Let the fleet submit **LinkedIn Easy-Apply** applications — the catastrophe-class lane (a LinkedIn ban is the single irreversible failure) — behind the hardest enforcement available. The one-IP gate, single-account mutex, rolling-24h cap, approval gate, and `li_at` profile exist; B adds the **worker tick**, an **atomic single-event halt**, a **separate LinkedIn canary**, the **mandatory supervised-conflict interlock**, the LinkedIn push/result/reclaim/park helpers, and brings A's never-phantom-apply routing + the shared dedup to LinkedIn.

**Done when:** (a) `_tick_linkedin` (role `linkedin`, added to the role allowlist + dispatch) leases via `lease_linkedin`, applies via the same `apply_fn` contract, and routes off `run_job`'s verdict — proven by tests incl. `failed:no_result_line → crash_unconfirmed`; (b) a single challenge atomically sets `account:linkedin.halted_until` **at park time in one transaction**, and with `min_gap ≥ agent-timeout` no second lease can run before the halt lands (proven by a test that drives the apply LONGER than min_gap); (c) the separate LinkedIn canary (default K=1) caps applies at K then blocks the lease (proven); (d) the **mandatory interlock** prevents the supervised and fleet LinkedIn drivers from running together; (e) `reclaim_linkedin` (crash_unconfirmed-only) is wired into the watchdog; (f) the home driver is built; (g) a runbook documents dry-run-first.

**Non-goals (B):** the unified visual console (Sub-project C); any change to the offsite lane (A); resume tailoring; a second LinkedIn account.

## 2. Scope boundary — LinkedIn, owner-IP only, single worker

LinkedIn applies run **only from the owner's IP/home box** (server-side `lease_linkedin` reject of `public_ip != owner_ip`) and as a **single worker** (the runbook + the interlock enforce one LinkedIn driver). The fleet never scrapes on the LinkedIn account. `applied_set` is shared with offsite — a company|title applied on LinkedIn is deduped against offsite and vice versa **(but the dedup check must be ADDED to `_LEASE_LINKEDIN`, §4.7).**

## 3. Architecture

### 3.1 `_tick_linkedin` + `ROLE_LINKEDIN` (`fleet/worker.py`)
Add `ROLE_LINKEDIN = "linkedin"` to the `WorkerLoop.__init__` role allowlist (worker.py:134 — today it raises `ValueError` on any other role) AND a dispatch branch in `run_once` BEFORE the `_tick_apply` fallback (else a `linkedin` worker silently runs the offsite tick). Per tick, in order:
1. **Interlock + session + halt pre-checks (Python belts; the lease SQL is the real enforcement):**
   - **Interlock (§5.3, MANDATORY):** acquire the shared LinkedIn-driver token (`pg_try_advisory_lock(hashtext('linkedin_driver'))` held for the worker's lifetime, or a `fleet_config.linkedin_driver_owner` flag). If not held → refuse to run (the supervised lane owns it).
   - **Session pre-flight:** `chrome.has_linkedin_session(profile_dir)` (file-based — checks the `li_at` cookie is present; cheap, no browser). If absent → idle `needs_relogin` (no lease, no halt). *Honest limit (§4.3): this catches a DELETED cookie, not a server-side-STALE one; a stale-but-present cookie still leases → may hit a wall → trips the halt (the safe over-halt direction).*
   - **Halt pre-check:** if `account:linkedin.halted_until > now()` → idle.
2. **Lease:** `queue.lease_linkedin(conn, self.worker_id, public_ip=self.public_ip, owner_ip=self.owner_ip)` — one-IP reject, account mutex, cap/gap, approval, canary, the atomic halt guard, AND the new `applied_set` dedup guard (§4.7), all enforced inside it.
3. **Apply:** `self.apply_fn(job)` — the **same contract as A** (`{"run_status","est_cost_usd"}`); `run_job` is URL-agnostic and already applies to LinkedIn via the `li_at` profile (no LinkedIn flag needed — confirmed).
4. **Route (a re-implemented status-passthrough — NOT literal reuse; A's writes to `apply_queue`):** `applied → write_linkedin_result(status='applied')`; a wall (`captcha`/`login_issue`/`auth_required`/rate-limit) → **`park_linkedin_challenge` which, in the SAME transaction, sets `halted_until=now()+cooldown` AND raises the `auth_challenge`** (§4.3); `failed:no_result_line`/`timeout`/`worker_error` → `write_linkedin_result(status='crash_unconfirmed')` (never phantom-applied); else → `failed`.

### 3.2 LinkedIn queue helpers (`fleet/queue.py`) — all NEW (the apply/compute variants are table-hardcoded)
- `park_linkedin_challenge(conn, worker_id, url, *, halt_seconds, commit=True)` — freeze the held `linkedin_queue` lease out of reclaim (`apply_status='challenge_pending'`, lease far out) AND, **in the same tx**, `INSERT account:linkedin ON CONFLICT DO NOTHING` then `UPDATE … SET halted_until = now() + halt_seconds`. (Ensuring the lazily-created governor row exists is required — a halt write on a missing row silently no-ops.)
- `reclaim_linkedin(conn, *, grace_seconds=30, commit=True)` — the stale-lease sweep, mirroring **`apply/pgqueue.reclaim_stale_leases` (NOT `reclaim_compute`/`reclaim_search`, which blindly re-queue)**. Because `_LEASE_LINKEDIN` bumps `attempts` at claim, a stale LinkedIn lease is always a possible mid-submit → park ALL stale LinkedIn leases as `crash_unconfirmed` (attempts=99, never re-leased). Wired into the watchdog tick (§5.4).
- `push_linkedin_jobs(conn, rows, *, approved_batch=None)` — UPSERT into `linkedin_queue` (NOT `apply_queue`; `push_apply_jobs` hardcodes `lane='ats'`/`apply_queue`), computing `dedup_key = _dedup.dedup_key(company, title)` **identically to the offsite push** (so cross-lane `applied_set` dedup matches).
- `approve_linkedin_jobs(conn, urls, batch)` and `resolve_linkedin_challenge(conn, url, *, requeue)` — `linkedin_queue` variants (`approve_jobs`/`resolve_challenge` are `apply_queue`-hardcoded → they silently no-op on LinkedIn rows).
- `clear_linkedin_halt(conn)` / `kill_linkedin(conn)` — set `account:linkedin.halted_until` NULL / far-future (each `INSERT … ON CONFLICT` first so the row exists).

### 3.3 `linkedin_worker_main` entrypoint
Like `apply_worker_main` but: the Chrome profile is the **`linkedin-seed` `li_at` clone** (`setup_worker_profile` prefers it); `role='linkedin'`; owner box; the same `should_halt` drive loop; acquires the interlock token at startup. Registered `applypilot-fleet-linkedin`.

### 3.4 `linkedin_home_main` driver
Like `apply_home_main` but operating on `linkedin_queue` with its OWN helpers (A's `set_canary`/`_canary_armed`/`approve` write the wrong columns/table): `push` (`push_linkedin_eligible` — the **effective-host LinkedIn select**, §5 fix), `approve [--all-pushed]` (refuses unless the LinkedIn canary armed, via `_linkedin_canary_armed`), `linkedin-canary K` / `lift-linkedin-canary`, `pull`, `challenges` / `resolve-challenge`, `clear-halt`, `kill`, `status`. Registered `applypilot-fleet-linkedin-home`.

## 4. Safety gates

### 4.1 G-IP (BUILT) / 4.2 G-mutex (BUILT)
`lease_linkedin` rejects `public_ip != owner_ip`; `_LEASE_LINKEDIN` locks `account:linkedin FOR UPDATE` (one lease in flight fleet-wide — though released at lease-commit, see §4.3).

### 4.3 G-halt — single-event halt (NEW; atomic, race-closed)
The breaker needs 8 samples, so a single challenge doesn't stop the lane. Fix:
- New `rate_governor.halted_until TIMESTAMPTZ` (idempotent ALTER).
- `_LEASE_LINKEDIN` `next` guard gains `AND (a.halted_until IS NULL OR a.halted_until < now())` under the `account:linkedin FOR UPDATE` lock.
- **Closing the halt-write race (critique BLOCKER):** the mutex is released at lease-commit, NOT held through the apply, so a naïve "set halt after the apply" leaves a window where a second lease slips through after a challenge but before the halt write. Two combined fixes: **(a)** the halt is written by `park_linkedin_challenge` in ONE transaction at park time (as early as the fleet can know); **(b)** the LinkedIn `min_gap_seconds` is set **`≥` the lease TTL** (default **min_gap = 1200s = ttl**). Because `last_applied_at` is stamped at lease-CLAIM and the lease expires at `claim + ttl`, the `last_applied_at + min_gap` guard makes the NEXT lease ineligible until the PRIOR lease has fully ended (timed out or reclaimed) — by which point its apply has finished and written any halt. This is bulletproof regardless of the apply's actual duration or the agent-timeout's reliability, and **independent of worker count** (not just the single-worker runbook). At most one LinkedIn apply per ~20 min — correct for the catastrophe lane. A test drives an apply LONGER than the agent timeout and proves the next lease still blocks until the halt lands.
- On a wall outcome (captcha/rate-limit/`linkedin_challenge`, and `login_issue`/`auth_required` since the session was pre-flight-verified present, biased-to-halt per §5.6), the halt is set (cooldown default **6h**, `APPLYPILOT_LINKEDIN_HALT_COOLDOWN`). `clear-halt`/`kill` adjust it (each ensures the row exists first).
- **Session-expiry ≠ challenge (honest):** the §3.1 file-based pre-flight catches a *deleted* cookie (→ `needs_relogin`, no halt). A server-side-*stale* cookie still leases and may trip the halt — the safe over-halt direction (a needless 6h pause, never a missed challenge). The post-apply path must NOT re-check the session to "downgrade" a wall to `needs_relogin` (that would reopen the dangerous direction).
- **Governor non-clobber:** `roll_window`/`clear_expired_breakers`/`evaluate_breakers` enumerate their columns and do NOT touch `halted_until` (verified) — a regression test asserts `halted_until` survives a `roll_window` + full `watchdog_tick`, and `roll_window` gets a comment noting the deliberate exclusion.

### 4.4 G-canary — separate LinkedIn canary (NEW)
`fleet_config.linkedin_canary_enabled BOOLEAN NOT NULL DEFAULT FALSE`, `linkedin_canary_remaining INTEGER` (default arm **K=1**). **The `fleet_db` fixture's fleet_config reset UPDATE MUST add `linkedin_canary_enabled=FALSE, linkedin_canary_remaining=NULL`** (else cross-test leak — fleet_config is never truncated). `_LEASE_LINKEDIN` reads them in a `cfg` CTE, guards `(NOT linkedin_canary_enabled OR linkedin_canary_remaining > 0)`, and a `reserve` CTE decrements on `EXISTS(next)`. Atomicity is provided by the `account:linkedin` mutex (serializes leases); the `cfg` (fleet_config) row is locked `FOR UPDATE` **FIRST** (before `account:linkedin`) purely for lock-order consistency with A's `_LEASE_APPLY` (which locks fleet_config first) — preventing any cross-lane deadlock. At 0 the guard blocks (auto-pause for review); re-arm to continue.

### 4.5 G-approval (lease gate BUILT; driver NEW) / 4.6 G-cap+gap (BUILT, min_gap raised per §4.3)
`linkedin_queue.approved_batch IS NOT NULL`; `approve_linkedin_jobs` refuses unless the canary is armed. `daily_cap=20` (runbook starts lower) + `min_gap=1200s` (= lease TTL, raised from 300s per §4.3 to close the halt-write window) + reserve-at-claim.

### 4.7 G-double-apply (must be ADDED — NOT inherited)
`applied_set` is shared and `write_linkedin_result` UPSERTs it on `applied`/`crash_unconfirmed`. **But the lease-time dedup guard `AND NOT EXISTS (SELECT 1 FROM applied_set a WHERE a.dedup_key = q.dedup_key)` is in `_LEASE_APPLY` and NOT in `_LEASE_LINKEDIN` today — B MUST add it** (the critique caught this). Plus `push_linkedin_jobs` computes `dedup_key` identically to the offsite push. A cross-lane test: an offsite-applied company|title → the LinkedIn lease skips it.

### 4.8 G-phantom (NEW — re-implemented from A)
`_tick_linkedin`'s passthrough routes `failed:no_result_line`/`timeout`/`worker_error → crash_unconfirmed`; only exact `applied` writes applied; bypasses `captcha.classify`. **It is re-implemented** (calls `write_linkedin_result`/`park_linkedin_challenge`/`account:linkedin` scope — A's helper hardcodes the apply_queue table/scopes). Bias-to-halt: an ambiguous page routes to the WALL path (halt), not the crash path.

## 5. The six additions + the critique blockers (folded in)
1. **Dry-run first** (runbook): a one-off supervised single-URL dry-run (`applypilot apply --url <linkedin> --dry-run`) verifies the `li_at` + Easy-Apply path before arming the fleet canary — sequential with (not concurrent to) the fleet lane, so it honors the interlock. No new fleet dry-run code.
2. **Session pre-flight** → §4.3 (honest limit documented).
3. **Supervised-vs-fleet interlock — MANDATORY (critique BLOCKER, upgraded from "optional"):** a single shared token both drivers check — `pg_try_advisory_lock(hashtext('linkedin_driver'))` (or a `fleet_config.linkedin_driver_owner` flag). The fleet `_tick_linkedin` refuses to lease unless it holds it; the supervised `launcher.py` worker_loop must probe the same fleet-PG flag and set `exclude_li` (skip its LinkedIn lane) if the fleet owns it. A human-remembered precondition is NOT sufficient for the irreversible lane.
4. **Watchdog `reclaim_linkedin`** → §3.2/§5.4 (crash_unconfirmed-only).
5. **`kill` panic button** → §3.2/§3.4.
6. **Bias-to-halt classifier** → §4.8.

## 5.4 Watchdog wiring
`watchdog_tick` gains `reclaim_linkedin(conn)` alongside `reclaim_compute`/`reclaim_search`/`reclaim_stale_leases` (watchdog.py:46) + a `reclaimed_linkedin` summary key. No other watchdog change.

## 6. Error handling
Crash mid-apply → `reclaim_linkedin` → `crash_unconfirmed` (never re-leased). Wall → `park_linkedin_challenge` (freeze + halt in one tx) + `auth_challenge`. Dead cookie → `needs_relogin` idle (no lease/halt). The interlock not held → refuse to run. DB blips → reconnect. Home commands idempotent.

## 7. Testing (subagent-driven TDD, `fleet_db` Postgres)
- **Worker contract:** applied→applied+applied_set; failed:no_result_line→crash_unconfirmed (NOT applied); wall→parked+auth_challenge+halted_until set in one tx.
- **G-halt race (the catastrophe proof):** drive an apply LONGER than min_gap with a wall outcome → assert a concurrent/next lease is blocked (by the min_gap+halt), `halted_until` set; `clear-halt`/`kill` work; a halt write when the `account:linkedin` row doesn't yet exist still freezes the lane (the INSERT-first); a dead-session pre-flight does NOT set halted_until; `halted_until` survives `roll_window`+`watchdog_tick`.
- **G-canary:** K=1 → exactly 1 lease then None; re-arm; disabled→no decrement; the fixture resets the columns (no leak).
- **G-IP/mutex/approval:** one-IP reject; unapproved not leasable; approve refuses unless canary armed.
- **G-dedup (added):** a `dedup_key` in `applied_set` (incl. offsite-applied + backfilled) → the LinkedIn lease skips it.
- **reclaim_linkedin:** a stale lease → `crash_unconfirmed` (NOT re-queued); the watchdog calls it.
- **Interlock:** the fleet worker refuses to lease without the token.
- **Regression baseline:** `test_fleet_v3_governor_queue.py`, `test_fleet_v3_worker.py`, the A apply-lane tests, the watchdog tests — green (the `_LEASE_LINKEDIN`/watchdog changes don't touch the other lanes). Full suite green.

## 8. Owner-run (the LinkedIn canary runbook) + residual risk
`docs/fleet-linkedin-lane-runbook.md`: (0) PRECONDITIONS: the supervised LinkedIn lane is OFF (the interlock enforces it, but the operator confirms); the watchdog runs; a fresh `li_at` seed profile; owner box. (1) dry-run one LinkedIn job (supervised `--url --dry-run`). (2) `linkedin-home pull → push` (unapproved). (3) `linkedin-canary 1` then `approve --all-pushed`. (4) start `applypilot-fleet-linkedin` (owner box, acquires the interlock); it applies to ONE LinkedIn job then the canary blocks. (5) `pull` + verify: submitted cleanly? right resume? NO challenge (`halted_until` still NULL)? (6) if clean, re-arm `linkedin-canary 1` (another single) or raise K slowly; raise `daily_cap` only after many clean days. Anything off → `kill`.
**Residuals:** the halt is a 6h cooldown (LinkedIn may stay suspicious longer — owner judges, `kill` extends); a stale (not deleted) cookie can cause a needless 6h over-halt (safe direction); `approved_batch` is a presence-stamp (single-owner box).

## 9. Decided questions
- Scope = LinkedIn, owner-IP only, single worker. **Decided.**
- Single-event halt = atomic `halted_until` written at park-time in one tx + `min_gap ≥ lease TTL (1200s)` to close the lease-commit window bulletproof, + the Python pre-check belt. **Decided (owner + critique).**
- Canary = separate `fleet_config.linkedin_canary_*` (K=1), atomic via the account mutex, fleet_config locked first for lock-order. **Decided (owner + critique).**
- Supervised-vs-fleet interlock = **MANDATORY** shared token, not a runbook line. **Decided (critique).**
- `reclaim_linkedin` = crash_unconfirmed-only; `push_linkedin` = effective-host select + new `push_linkedin_jobs`; LinkedIn variants of approve/resolve/park; the `applied_set` dedup guard ADDED to `_LEASE_LINKEDIN`. **Decided (critique).**
- The six additions — all IN. **Decided (owner).**
- Next: Sub-project C (unified visual console) after B is proven. **Noted.**
