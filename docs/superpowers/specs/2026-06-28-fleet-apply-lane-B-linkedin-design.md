# Fleet Apply Lane B — LinkedIn — Design Spec

**Date:** 2026-06-28
**Status:** design, pending review (will be adversarially critiqued before sign-off)
**Repo:** `New project/ApplyPilot` (Python tool)
**Depends on:** Sub-project A (offsite apply lane — BUILT: the `apply_fn` status-passthrough contract, the canary/approval pattern, `should_halt` drive loop, the shared `applied_set` + home backfill), the fleet v3 foundation, and the watchdog lane. See [`2026-06-27-fleet-apply-lane-A-offsite-design.md`](2026-06-27-fleet-apply-lane-A-offsite-design.md).

## 1. Goal & success criteria

Let the fleet submit **LinkedIn Easy-Apply** applications — the catastrophe-class lane, where a LinkedIn ban is the single irreversible failure — behind the hardest safety enforcement available. Most safety primitives exist (the one-IP gate, the single-account mutex, the rolling-24h cap, the approval gate, the `li_at` Chrome profile); B adds the **worker tick**, the **single-event halt** (atomic), a **separate LinkedIn canary**, and the home driver — and brings A's never-phantom-apply routing + the shared double-apply dedup to LinkedIn.

**Done when:** (a) `_tick_linkedin` (role `linkedin`) leases via `lease_linkedin`, applies via the same `apply_fn` contract, and routes off `run_job`'s verdict (applied / park / crash) — proven by tests including `failed:no_result_line → crash_unconfirmed`; (b) a single challenge atomically sets `account:linkedin.halted_until = now()+6h` and the lease physically cannot proceed until it passes (proven by test) + a Python pre-check fast-idles the worker; (c) the separate LinkedIn canary (default K=1) caps LinkedIn applies at K then blocks the lease until re-armed (proven by test); (d) `reclaim_linkedin` is wired into the watchdog; (e) the home driver (`push`/`approve`/`linkedin-canary`/`pull`/`challenges`/`resolve-challenge`/`clear-halt`/`kill`/`status`) is built; (f) a runbook documents the dry-run-first procedure + the supervised-conflict precondition.

**Non-goals (Sub-project B):** the unified visual console (Sub-project C); any change to the offsite lane (A); resume tailoring (apply-as-is); a second LinkedIn account.

## 2. Scope boundary — LinkedIn, owner-IP only

LinkedIn applies run **only from the owner's IP / home box** — enforced server-side by `lease_linkedin` (`public_ip != owner_ip → None`). The fleet *coordinates* LinkedIn (queue, governor, canary, halt, approval) but the worker is pinned to the owner IP and uses the cloned `li_at` Chrome profile. The fleet **never scrapes** on the LinkedIn account — discovery scrapes from other IPs; the LinkedIn worker only *applies*. `applied_set` is shared with the offsite lane, so a company|title applied on LinkedIn is deduped against offsite and vice versa.

## 3. Architecture

### 3.1 `_tick_linkedin` + `ROLE_LINKEDIN` (`fleet/worker.py`)
A new tick mirroring `_tick_apply`, dispatched on `role='linkedin'`. Per tick, in order:
1. **Session pre-flight + halt pre-check (Python belt):** if `chrome.has_linkedin_session(...)` is False → idle with a `needs_relogin` signal (do NOT lease, do NOT trip the halt — a dead cookie is not a LinkedIn challenge, §4.3); if the account is currently halted (`halted_until > now()`) → idle. Both are fast-idle belts; the lease SQL is the load-bearing enforcement.
2. **Lease:** `queue.lease_linkedin(conn, self.worker_id, public_ip=self.public_ip, owner_ip=self.owner_ip)` — the one-IP reject, the account mutex, the cap/gap, the approval gate, the canary, and the atomic halt all enforced inside it (§4).
3. **Apply:** `self.apply_fn(job)` — the **same contract as A** (`{"run_status","est_cost_usd"}`).
4. **Route off the verdict (status-passthrough, reused from A — never re-classify):** `applied → write_linkedin_result(status='applied')`; a wall (`captcha`/`login_issue`/`auth_required`/rate-limit) → `park_linkedin_challenge` + raise `auth_challenge` **+ trip the halt** (set `halted_until`); `failed:no_result_line`/`timeout`/`worker_error` → `write_linkedin_result(status='crash_unconfirmed')` (never phantom-applied); else → `failed`.

### 3.2 `park_linkedin_challenge` + `reclaim_linkedin` (`fleet/queue.py`)
- `park_linkedin_challenge(conn, worker_id, url, *, commit=True)` — the `linkedin_queue` variant of `park_challenge` (freeze the held lease out of reclaim: `apply_status='challenge_pending'`, push `lease_expires_at` far out).
- `reclaim_linkedin(conn, *, grace_seconds=30, commit=True)` — the `linkedin_queue` stale-lease sweep (safe pre-launch re-queue vs `crash_unconfirmed`), mirroring `reclaim_compute`/`reclaim_search`. **Wired into the watchdog tick** (addition #4).

### 3.3 `linkedin_worker_main` entrypoint
A new `fleet/linkedin_worker_main.py`, like `apply_worker_main` but: **the Chrome profile is cloned from the `linkedin-seed` (`li_at`)** (not the offsite fresh profile — `setup_worker_profile` already prefers the seed clone); `role='linkedin'`; runs on the owner box (the one-IP gate enforces it); the same `should_halt` drive loop. Registered `applypilot-fleet-linkedin`.

### 3.4 `linkedin_home_main` driver
A new `fleet/linkedin_home_main.py`: `push` (select the LinkedIn-lane jobs the offsite push *excludes* — `application_url LIKE '%linkedin.com%'` — from the brain into `linkedin_queue`, UNAPPROVED), `approve [--all-pushed]` (stamp a batch token; **refuses unless the LinkedIn canary is armed**), `linkedin-canary K` / `lift-linkedin-canary`, `pull`, `challenges` / `resolve-challenge <url> [--skip]`, `clear-halt` (set `halted_until=NULL` after you've verified it's safe), `kill` (the panic button — set `halted_until` far future, instantly freezing the lane), `status` (queue depth + linkedin_canary_remaining + halted_until + account:linkedin cap/spend + open challenges). Registered `applypilot-fleet-linkedin-home`.

## 4. Safety gates

### 4.1 G-IP — one-IP hard gate (BUILT)
`lease_linkedin` returns None if `public_ip != owner_ip` (server-side, from the `workers` registered IP via the broker, never client-supplied). LinkedIn can only ever run from the owner's home IP.

### 4.2 G-mutex — single-account mutex (BUILT)
`_LEASE_LINKEDIN` locks the `account:linkedin` `rate_governor` row `FOR UPDATE` — at most one LinkedIn lease in flight fleet-wide, ever. This also makes the LinkedIn canary decrement automatically atomic (§4.4).

### 4.3 G-halt — single-event halt (NEW — the key gate; atomic + belt)
The statistical breaker needs `min_samples=8` before tripping, so a single challenge does NOT stop the lane today. Fix — **Strategy 1 (atomic) + the Python pre-check belt:**
- New column `rate_governor.halted_until TIMESTAMPTZ` (idempotent `ALTER … ADD COLUMN IF NOT EXISTS`).
- `_LEASE_LINKEDIN` gains `AND (a.halted_until IS NULL OR a.halted_until < now())` in the `next` guard — under the `account:linkedin FOR UPDATE` lock, so the halt is enforced **atomically in the lease**.
- On a wall/challenge outcome (`captcha`/rate-limit/`linkedin_challenge`, and — because the session was verified live pre-lease by the §3.1.1 pre-flight — `login_issue`/`auth_required` too, biased-to-halt per addition #6), `write_linkedin_result` (or `_tick_linkedin`) sets `account:linkedin.halted_until = now() + cooldown` (default **6h**, `APPLYPILOT_LINKEDIN_HALT_COOLDOWN`).
- The worker's Python pre-check (§3.1.1) reads `halted_until` and fast-idles — a belt, not the guarantee.
- `clear-halt` (set NULL) resumes early after the owner verifies; `kill` (set far-future) freezes instantly.
- **(addition #2) Session-expiry is NOT a challenge:** a dead `li_at` (caught by the §3.1.1 pre-flight) routes to `needs_relogin` idle — it does NOT lease and does NOT trip the 6h halt. Only an in-apply wall on a *verified-live* session trips the halt (so the halt means "LinkedIn is suspicious," not "re-login me").

### 4.4 G-canary — separate LinkedIn canary (NEW)
A dedicated LinkedIn canary, independent of the offsite canary, so LinkedIn stays cautious even when offsite runs at volume.
- New `fleet_config.linkedin_canary_enabled BOOLEAN NOT NULL DEFAULT FALSE`, `linkedin_canary_remaining INTEGER` (default arm **K=1**). The `fleet_db` fixture resets both.
- `_LEASE_LINKEDIN` reads them (a `cfg` CTE), guards `next` on `(NOT linkedin_canary_enabled OR linkedin_canary_remaining > 0)`, and a `reserve` CTE decrements `linkedin_canary_remaining` on `EXISTS(next)`. **Atomicity is free:** the `account:linkedin FOR UPDATE` mutex already serializes every LinkedIn lease, so no second lock is needed — but the `cfg` (fleet_config) row is locked FIRST for lock-order consistency with A's `_LEASE_APPLY` (avoids any cross-lane deadlock). At 0, the guard blocks the lease (the lane "auto-pauses" for review); the owner re-arms `linkedin-canary K` after reviewing.

### 4.5 G-approval (lease gate BUILT; driver NEW)
`linkedin_queue.approved_batch IS NOT NULL` required by `_LEASE_LINKEDIN`. The home `approve` (refuses unless the LinkedIn canary is armed) is the only path; `push` stages unapproved.

### 4.6 G-cap+gap (BUILT)
`account:linkedin` `daily_cap=20` (start LOWER — runbook ramps it) + `min_gap_seconds=300` + reserve-at-claim, all in the lease SQL.

### 4.7 G-double-apply (INHERITED)
`applied_set` is shared; `write_linkedin_result` UPSERTs it on `applied`/`crash_unconfirmed`; A's `backfill_applied_set` already seeds it from home history. So a LinkedIn job whose company|title was applied anywhere is never re-applied.

### 4.8 G-phantom (NEW — mirror A)
`_tick_linkedin` reuses A's status-passthrough: `failed:no_result_line`/`timeout`/`worker_error → crash_unconfirmed` (never `applied`); only an exact `applied` writes applied; bypasses `captcha.classify`. (Addition #6: the classifier biases toward *halt* on ambiguity — an unsure page is treated as a challenge.)

## 5. The six additions (folded in)
1. **Dry-run first** (runbook): the first verification is a one-off supervised single-URL dry-run (`applypilot apply --url <linkedin-job> --dry-run` — fills the form, never submits) to prove the `li_at` session + Easy-Apply path, BEFORE arming the fleet canary. Reuses the proven dry-run; sequential (not concurrent) with the fleet lane, so it respects #3. No new fleet dry-run code.
2. **Session pre-flight** → §4.3 (don't burn the halt on an expired cookie).
3. **Supervised-vs-fleet conflict** (runbook hard precondition): NEVER run the supervised LinkedIn lane (`launcher.py`) while the fleet LinkedIn worker runs — two uncoordinated drivers on one account/IP would break the single-in-flight + halt guarantees (the `account:linkedin` mutex covers only the fleet PG, not the supervised SQLite path). Optional small interlock (a file/PG flag one checks).
4. **Watchdog `reclaim_linkedin`** → §3.2 (the watchdog reclaims stale LinkedIn leases).
5. **`kill` panic button** → §3.4 (instant freeze).
6. **Bias-to-halt classifier** → §4.8 (ambiguous page = challenge).

## 6. Error handling
A crash mid-LinkedIn-apply → `reclaim_linkedin` → `crash_unconfirmed` (never re-leased). A wall → `park_linkedin_challenge` (frozen lease) + `auth_challenge` + the 6h halt. A dead session → `needs_relogin` idle (no lease, no halt). DB blips → reconnect. The home driver commands are idempotent.

## 7. Testing (subagent-driven TDD, against the `fleet_db` disposable Postgres)
- **Worker contract:** `_tick_linkedin` with a stub `apply_fn`: `applied → linkedin_queue applied + applied_set`; `failed:no_result_line → crash_unconfirmed` (NOT applied); a wall → parked + `auth_challenge` + `halted_until` set.
- **G-halt (atomic):** with `halted_until > now()`, `lease_linkedin` returns None; after it passes, leasable; a wall outcome sets `halted_until`; `clear-halt`/`kill` work; a dead-session pre-flight does NOT set `halted_until`.
- **G-canary:** `linkedin_canary_enabled, remaining=1` → exactly 1 LinkedIn lease then None (guard blocks); re-arm re-enables; disabled → no decrement; the fixture resets the columns.
- **G-IP / G-mutex:** `public_ip != owner_ip` → None; the mutex serializes (a concurrency test, though the mutex is pre-existing).
- **G-approval:** unapproved `linkedin_queue` row not leasable; approve refuses unless canary armed.
- **reclaim_linkedin:** stale lease → safe re-queue vs crash_unconfirmed; the watchdog tick calls it.
- **Home driver:** each subcommand's DB effect (incl. `kill`/`clear-halt`/`push`/`approve`/`canary`).
- **Regression baseline:** `tests/test_fleet_v3_governor_queue.py`, `tests/test_fleet_v3_worker.py`, the A apply-lane tests, the watchdog tests — all stay green (the `_LEASE_LINKEDIN` change must not touch the other lease constants). Full fleet suite green.

## 8. Owner-run (the LinkedIn canary runbook) + residual risk
`docs/fleet-linkedin-lane-runbook.md`: (0) **PRECONDITIONS:** the supervised LinkedIn lane is OFF; the watchdog is running; `applypilot linkedin-login` has a fresh `li_at` seed profile; the worker runs on the owner box. (1) **Dry-run** one LinkedIn job via the supervised `--url --dry-run` to verify the session + form path. (2) `linkedin-home pull → push` (stage unapproved). (3) `linkedin-home linkedin-canary 1` then `approve --all-pushed`. (4) start `applypilot-fleet-linkedin` on the owner box; it applies to exactly ONE LinkedIn job then the canary guard blocks. (5) `linkedin-home pull` + verify: submitted cleanly? right resume? NO challenge fired (`halted_until` still NULL)? (6) if clean, `linkedin-canary 1` again (another single) or raise K slowly; raise `daily_cap` only after many clean days. If anything looks off, `kill`. **Residuals:** the halt is a 6h cooldown (LinkedIn may stay suspicious longer — the owner judges); a wall after a verified session is treated as a challenge (may over-halt on a rare mid-flight expiry — safe direction); `approved_batch` is a presence-stamp (single-owner box).

## 9. Decided questions
- Scope = LinkedIn, owner-IP only; coordinated by the fleet but pinned to the home box. **Decided.**
- Single-event halt = **Strategy 1 (atomic `halted_until` lease guard)** + the Python pre-check belt; session-expiry ≠ challenge. **Decided (owner).**
- Canary = **separate LinkedIn canary** (`fleet_config.linkedin_canary_*`, default K=1), atomic via the account mutex. **Decided (owner).**
- The six additions (dry-run-first, session pre-flight, supervised-conflict precondition, watchdog reclaim_linkedin, kill switch, bias-to-halt) — all IN. **Decided (owner).**
- `apply_fn` contract + status-passthrough reused from A. **Decided.**
- Next: Sub-project C (unified visual console) after B is proven. **Noted.**
