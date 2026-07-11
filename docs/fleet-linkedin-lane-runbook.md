# Fleet LinkedIn Lane B — Operator Runbook

## Overview

This runbook covers the **LinkedIn apply lane** (`lane='ats'` in `linkedin_queue`)
of the residential fleet.  LinkedIn is the single catastrophe-class destination: a
ban is unrecoverable, so the lane carries additional safety layers beyond the offsite
ATS lane (Lane A):

- **One-IP hard reject** — the worker's registered egress IP must equal the broker-
  trusted `owner_ip`.  Any mismatch causes `lease_linkedin` to return `None`
  immediately, before even touching the database.
- **Single-account mutex** — the `account:linkedin` governor row is locked
  `FOR UPDATE` at lease time, serializing ALL LinkedIn leases so two concurrent
  sessions can never run.
- **Separate LinkedIn canary** — `fleet_config.linkedin_canary_enabled /
  linkedin_canary_remaining` is a DISTINCT counter from Lane A's
  `canary_enabled / canary_remaining`.  Arming one never arms the other.
- **Single-event halt** — a captcha/login wall from any tick calls
  `park_linkedin_challenge`, which sets `rate_governor.halted_until` for
  `account:linkedin` in one atomic transaction.  The default cooldown is 6 hours
  (`APPLYPILOT_LINKEDIN_HALT_COOLDOWN`, default `21600`).  Min-gap = lease TTL
  (1200 s) closes the commit-race window.
- **Mandatory advisory interlock** — `applypilot-fleet-linkedin` acquires
  `pg_try_advisory_lock(hashtext('applypilot:linkedin_driver'))` at startup and
  exits immediately if the lock is unavailable.  The supervised apply path holds
  the same lock when active, so the fleet driver and the supervised path can never
  run concurrently.

**Building this lane is NOT the same as running it.**  Nothing applies to LinkedIn
until you, the owner, execute the canary sequence described below on the home box.

---

## Preconditions (must ALL be true before proceeding)

### P1 — Supervised LinkedIn lane is OFF

The legacy supervised apply path must not be active.  The advisory interlock
(`hashtext('applypilot:linkedin_driver')`) enforces this at startup: if the
supervised path holds the lock, `applypilot-fleet-linkedin` will refuse to start.
Verify:

```bash
# Check whether the supervised apply is running (it holds the lock while active).
# If fleet_linkedin_active returns true from the Codex bridge or directly:
psql $FLEET_PG_DSN -c "SELECT pg_try_advisory_lock(hashtext('applypilot:linkedin_driver')) AS available;"
# available = true  → the lock is free; the supervised path is NOT running. OK to proceed.
# available = false → the supervised path holds the lock. STOP — terminate it first.
```

The `FLEET_PG_DSN` env-var must be set before any command in this runbook:

```bash
export FLEET_PG_DSN="postgresql://postgres@127.0.0.1:<port>/postgres"
```

### P2 — Watchdog is running

`applypilot-fleet-watchdog` must be running on the home box.  It reclaims stale
leases (crashed workers), evaluates circuit-breakers, and preserves the halt flag
across watchdog roll-window events.

```bash
applypilot-fleet-watchdog --dsn $FLEET_PG_DSN
```

### P3 — Fresh `li_at` cookie in the linkedin-seed Chrome profile

The worker drives a Playwright session using the `li_at` session cookie from the
linkedin-seed Chrome profile.  A stale or expired cookie will trigger a login wall
on the first tick, set `halted_until` for 6 hours, and park the job.  Before the
canary run:

1. Open the linkedin-seed Chrome profile on the home box.
2. Log in to LinkedIn manually.
3. Confirm the session is valid (a search or the feed loads without a login prompt).

### P4 — Home/owner box only

LinkedIn applies **must only run on the owner/home box** — the one whose egress IP
is registered as `owner_ip`.  The one-IP hard reject in `lease_linkedin` enforces
this: any residential fleet worker with a different IP simply gets `idle` and never
sees a LinkedIn row.  Do not run `applypilot-fleet-linkedin` on any cloud or
residential helper machine.

---

## Ordered Steps

### Step 1 — Dry-run / status check

Before touching anything, confirm the current LinkedIn queue state:

```bash
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN status
```

Key fields to check:
- `linkedin_canary_enabled` — should be `false` before arming.
- `halted_until` — must be `null`.  If it is set (from a prior wall), run
  `clear-halt` only after manually verifying the LinkedIn session is healthy.
- `open_challenges` — any open auth challenges from prior sessions must be resolved
  before the next canary run (`resolve-challenge` or `resolve-challenge --skip`).

### Step 2 — Pull (seed applied_set)

Backfill `applied_set` from the home brain so the fleet never re-applies to a job
the home box already submitted in a prior session:

```bash
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN pull
```

### Step 3 — Refresh LinkedIn posting availability

Run the logged-in LinkedIn resolver before pushing. It records whether the page is
still reachable and still has an apply path, without submitting anything:

```bash
applypilot linkedin-resolve-apply-urls --limit 200
```

Rows classified as `unavailable` are stamped `liveness_status='dead'`. The fleet
push/approval/lease path accepts only rows with a recent positive resolver status
(`easy_apply` or `resolved_offsite`; default max age 3 days).

### Step 4 — Push eligible LinkedIn jobs

Push high-score LinkedIn rows from the brain into `linkedin_queue`:

```bash
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN push --score-floor 7
```

The push now applies three extra eligibility guards beyond score:

- **Resolver freshness (`--max-resolved-age-days`, default 3).** LinkedIn is a
  network-probe BLOCKED host, so `verify-live` still skips it by policy. Instead,
  the logged-in resolver is the pre-approval availability check.
- **Discovery recency (`--max-age-days`, default 21).** Pass `--max-age-days 0`
  only when intentionally backfilling old, freshly resolver-checked rows.
- **Company + title required.** Postings with neither are unapplyable and collapse onto a
  single `(company,title)` dedup_key, so they are excluded.

The push also prints a `note:` line counting apply-shaped LinkedIn jobs held out **only**
because they are unscored — run the scorer to fold that backlog into the candidate pool.

Verify the queue depth:
```bash
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN status
```

### Step 4 — Arm the LinkedIn canary (set K)

K is the maximum number of LinkedIn applications the fleet may submit before
auto-pausing for review.  Choose a small K for the first canary run (1 is
recommended for the initial test; 2–5 for subsequent runs once you have confirmed
the session and automation work correctly).

```bash
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN linkedin-canary 1
# fleet_config: linkedin_canary_enabled=TRUE, linkedin_canary_remaining=1
```

The `approve` command **refuses** unless the LinkedIn canary is armed (the same
gate as Lane A).  This enforces the arm-then-approve ordering in code — it cannot
be inverted by accident.

### Step 5 — Approve --all-pushed

Stamp a fresh batch token on the current queued rows.  Only rows with a non-NULL
`approved_batch` are eligible for leasing.

```bash
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN approve --all-pushed
```

Record the batch token printed by this command.  It identifies which rows belong to
this canary run if you need to audit or revoke the batch:

```sql
-- Revoke unapplied approvals from a batch:
UPDATE linkedin_queue SET approved_batch=NULL
WHERE approved_batch='<token>' AND status='queued';
```

### Step 6 — Start the LinkedIn fleet worker (acquires the advisory interlock)

Launch the LinkedIn fleet driver on the home box:

```bash
applypilot-fleet-linkedin --dsn $FLEET_PG_DSN --worker-id home-linkedin-0
```

At startup the process calls `pg_try_advisory_lock(hashtext('applypilot:linkedin_driver'))`.
If the supervised path is active, the lock fails and the process exits immediately.

The worker leases one row at a time.  On a confirmed apply it:
1. Writes `status='applied'` to `linkedin_queue` (lease-owner guarded — no phantom applies).
2. Upserts `applied_set` so the posting can never be applied to again.
3. Decrements `linkedin_canary_remaining` (atomically, in the lease CTE).
4. When `linkedin_canary_remaining` reaches 0 the CTE blocks further leases.

### Step 7 — Fleet applies exactly K, then auto-pauses

After K successful applications the canary remaining hits 0 and no further leases
are granted.  The worker will return `idle` on subsequent ticks.  You may leave the
worker running (it will idle-loop) or stop it with `kill` (see Step 9b).

### Step 8 — Review results

```bash
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN status
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN challenges
```

Check:
- `queue.applied` count equals K.
- `halted_until` is still `null` (no wall was hit).
- `open_challenges` is 0.

If `halted_until` is NOT null, a wall was hit during the run.  Follow the halt
procedure in Step 9a before re-arming.

### Step 9a — Handle a halt (wall hit)

If a captcha or login wall fires, `park_linkedin_challenge` sets `halted_until` for
the default 6-hour cooldown (`APPLYPILOT_LINKEDIN_HALT_COOLDOWN`, default `21600`).
The driver cannot lease any new rows while the halt is active.

1. Open the linkedin-seed Chrome profile and manually verify the LinkedIn session.
2. If the session is healthy (no captcha), clear the halt:

```bash
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN clear-halt
```

3. Review the open challenge:

```bash
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN challenges
```

4. Resolve it (retry from the owner box):

```bash
# Requeue the parked job for a retry:
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN resolve-challenge <url>

# Or give up on this posting (marks it 'blocked'):
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN resolve-challenge <url> --skip
```

Do NOT clear the halt and re-arm the canary until you have confirmed the LinkedIn
session is healthy.  A stale cookie can trigger repeated walls.

### Step 9b — Kill (emergency stop)

If anything looks off — unexpected destinations, a ban warning, or an authentication
anomaly — stop all LinkedIn activity immediately:

```bash
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN kill
# Sets halted_until = now() + 100 years. No further leases until manually cleared.
```

Also terminate the `applypilot-fleet-linkedin` process.

### Step 10 — Expand the canary (next batch)

If the K submissions look clean (right companies, no doubles, no challenges):

```bash
# Arm a larger canary and approve the next batch:
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN linkedin-canary <N>   # N > K
applypilot-fleet-linkedin-home --dsn $FLEET_PG_DSN approve --all-pushed
# Restart the worker if it exited, or let it resume if still running.
```

---

## Residuals (known limitations / operational notes)

### R1 — Halt cooldown is conservative by design

The 6-hour halt (`APPLYPILOT_LINKEDIN_HALT_COOLDOWN`, default 21600 s) is
deliberately conservative.  A stale cookie can trigger a login wall on a perfectly
healthy IP; the halt ensures the operator reviews the session before the next
attempt.  Clearing the halt early (`clear-halt`) is safe if you have manually
confirmed the LinkedIn session is valid.

### R2 — Stale cookie over-halts

If the `li_at` cookie expires between runs, the first tick in the next canary run
will hit a login wall and set the halt.  This is expected behaviour — it is
conservative.  Refresh the cookie (log in manually in the linkedin-seed profile)
before clearing the halt.

### R3 — approved_batch is a presence-stamp

The `approved_batch` column is a string token (UTC timestamp + UUID fragment), not
a foreign key.  There is no batch-level cancel command.  To revoke unapplied
approvals from a batch, set `approved_batch = NULL` directly (see Step 5 note).

### R4 — Advisory-lock interlock is session-scoped

`pg_try_advisory_lock` is a session-level lock — it is released automatically when
the Postgres connection closes (e.g. the `applypilot-fleet-linkedin` process exits).
There is no need to manually release it.

### R5 — Building ≠ running

The LinkedIn fleet lane (Tasks 1-9) is now fully built and tested (210 fleet suite
passes, 0 failures).  Nothing applies to LinkedIn until you, the owner, run this
canary sequence by hand on the home box.  The canary is the gate; no LinkedIn
application is submitted without explicit arm + approve + start.

---

## Quick-reference command table

| Goal | Command |
|------|---------|
| Check queue + config state | `linkedin-home status` |
| Seed applied_set + review terminals | `linkedin-home pull` |
| Push eligible jobs to PG | `linkedin-home push [--score-floor N] [--limit N]` |
| Arm LinkedIn canary | `linkedin-home linkedin-canary <K>` |
| Approve current batch | `linkedin-home approve --all-pushed` |
| Start LinkedIn fleet worker | `applypilot-fleet-linkedin --dsn ... --worker-id ...` |
| List open auth challenges | `linkedin-home challenges` |
| Resolve a challenge (retry) | `linkedin-home resolve-challenge <url>` |
| Resolve a challenge (skip/block) | `linkedin-home resolve-challenge <url> --skip` |
| Clear a halt (after session verified) | `linkedin-home clear-halt` |
| Kill (emergency — 100yr halt) | `linkedin-home kill` |
| Expand canary to N | `linkedin-home linkedin-canary <N>` (re-arms, does not clear halt) |
| Lift canary (unlimited) | `linkedin-home lift-linkedin-canary` |
