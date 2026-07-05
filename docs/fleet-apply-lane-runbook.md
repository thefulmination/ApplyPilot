# Fleet Apply Lane A — Operator Runbook

## Overview

This runbook covers the **offsite ATS apply lane** (`lane='ats'`) of the residential
fleet.  The lane uses a canary gate and an owner approval step so no application is
submitted without explicit human sign-off, and the canary limits blast radius to
exactly K submissions before auto-pausing for review.

---

## Preconditions (must be true before proceeding)

### P0 — Fleet code is published and pinned

All worker boxes should run a clean git checkout of one published fleet version.  The
normal rollout path is:

```powershell
git status --short --branch
git push origin HEAD
git tag fleet-20260705.1
git push origin fleet-20260705.1
```

Then pin the fleet to the current software identity from the home box:

```powershell
.\.conda-env\python.exe -c "import os; from applypilot.apply import pgqueue; from applypilot.fleet.config import set_pinned_version; from applypilot.fleet.software_version import current_sw_version; conn=pgqueue.connect(os.environ['FLEET_PG_DSN']); set_pinned_version(conn, current_sw_version()); conn.close(); print(current_sw_version())"
```

Worker boxes should converge through `fleet-agent.ps1 -AutoUpdate` while between
jobs.  Do not hand-edit remote files over SSH; fix the repo, push/tag, and let the
agent fast-forward clean clones.

Verify drift:

```powershell
.\fleet-health.ps1
```

If a box is stale, missing the updater, or cannot fast-forward, use Tailscale/SSH
only for bootstrap/repair:

```powershell
.\Invoke-FleetReconcile.ps1              # check-only
.\Invoke-FleetReconcile.ps1 -Only m4     # check one target
.\Invoke-FleetReconcile.ps1 -Only m4 -RunHealth
.\Invoke-FleetReconcile.ps1 -Apply -Branch codex/fleet-applier-hardening
```

### P1 — v1 fleet is OFF

The legacy v1 fleet apply process (`applypilot apply` / the keepalive task) **must
not be running** on the same home box or Railway service while the offsite lane is
active.  Both paths write to the same apply destinations; running them concurrently
risks double-submit and cap over-run.

Verify:
```
# Windows: check that no applypilot apply process is running
Get-Process python | Where-Object { $_.MainWindowTitle -like '*apply*' }
# Railway: confirm the legacy apply service is stopped or removed
```

### P2 — Watchdog is running

`applypilot-fleet-watchdog` (or the equivalent `test_fleet_watchdog` harness) must
be running on the home box.  The watchdog reclaims stale leases (crashed workers)
and runs the circuit-breaker `evaluate_breakers` sweep.  Without it, a crashed
worker leaves a job frozen in `status='leased'` indefinitely, and no adaptive
rate-throttle fires if a host starts blocking.

```
applypilot-fleet-watchdog --dsn $FLEET_PG_DSN
```

---

## Ordered Steps

### Step 1 — Pull (sync brain → PG queue)

Backfill `applied_set` from home history so the fleet never re-applies to a job
the home box already submitted, then push eligible jobs into `apply_queue`:

```bash
# One-time (first run): seed applied_set from local brain
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN pull

# Push eligible jobs (score >= 7, status queued in brain, not in applied_set)
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN push --score-floor 7 --include-research
```

Verify the queue depth:
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN status
```

If raw queue counts look nonzero but nothing leases, run the read-only repair
report before changing state:

```bash
applypilot-fleet-repair-report --dsn $FLEET_PG_DSN
```

Use its recommendations in order.  Confirmed outcome-email matches can be flipped
with a small cap:

```bash
applypilot-fleet-reconcile-email --dsn $FLEET_PG_DSN --no-scan --apply --confirmed-only --max-flips 1
```

Overbroad aggregator dedup keys are dry-run first, then applied only to queued
rows:

```bash
applypilot-fleet-dedup-repair --dsn $FLEET_PG_DSN --dedup-key <key> --json
applypilot-fleet-dedup-repair --dsn $FLEET_PG_DSN --dedup-key <key> --apply --max-rows 25
```

### Step 2 — Arm the canary (set K)

K is the maximum number of applications the fleet may submit before auto-pausing
for review.  Choose a small K for the first canary run (2–5 is typical).

```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN canary <K>
# e.g. canary 2  →  fleet_config: canary_enabled=TRUE, canary_remaining=2
```

The `approve` command **refuses** to run if the canary is not armed first.  This
ordering is enforced in code (`apply_home_main.approve` → `_canary_armed` check)
so the canary-then-approve sequence cannot be inverted by accident.

### Step 3 — Approve --all-pushed

Stamp the current batch of queued rows with an `approved_batch` token.  Only rows
with a non-NULL `approved_batch` are eligible for leasing.

```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN approve --all-pushed
```

The batch token is a UTC timestamp + UUID fragment, e.g. `20260627T143022-a1b2c3d4`.
Record it: you will need it if you need to identify which batch was applied.

### Step 4 — Start the fleet apply worker(s)

Launch one or more residential workers in apply role:

```bash
# On the home/owner box (or a residential machine):
applypilot-fleet-apply --dsn $FLEET_PG_DSN --worker-id home-0 --home-ip <YOUR_EGRESS_IP>
```

The worker calls `queue.lease_apply` which atomically:
1. Locks the `fleet_config` row (`FOR UPDATE`),
2. Checks `paused`, `canary_remaining`, `spend_cap_usd`, governor state, and dedup,
3. Decrements `canary_remaining` and sets `paused = (canary_remaining - 1 <= 0)` in one CTE.

### Step 5 — Fleet applies ≤ K then auto-pauses

The fleet will submit at most K applications.  When `canary_remaining` reaches 0
the CTE sets `fleet_config.paused = TRUE` atomically — no further leases are
granted, even if additional workers are running.

Each submitted job is written to `applied_set` (dedup guard) and the result is
available in `apply_queue.apply_status`.

### Step 6 — Pull results + review

Sync results back to the home brain:
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN pull
```

Review the K submissions in the brain or via:
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN status
```

Check for open auth challenges (captcha / login walls):
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN challenges
```

### Step 7 — Handle auth challenges (if any)

If a worker hit a captcha or login wall, it raises an `auth_challenge` row and
**parks** the job (status remains `'leased'` with a 3650-day expiry — never
re-claimed blind).  The owner resolves it from the trusted home box:

```bash
# Solve the wall yourself on the home box, then requeue for a retry:
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN resolve-challenge <url>

# Or give up on this posting (marks it 'blocked'):
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN resolve-challenge <url> --skip
```

### Step 8a — Expand the canary (next batch)

If the K submissions look clean (right companies, no doubles, no challenges):
```bash
# Arm a larger canary and approve the next batch
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN canary <N>   # N > K
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN approve --all-pushed
# (fleet_config.paused is reset to FALSE by set_canary)
```

### Step 8b — Lift the canary (production mode)

When confident, lift the canary to allow unlimited applies (still bounded by
`spend_cap_usd` and the adaptive governor):

```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN lift-canary
# Then un-pause:
# (lift-canary does NOT auto-unpause; set it explicitly)
# psql $FLEET_PG_DSN -c "UPDATE fleet_config SET paused=FALSE WHERE id=1;"
```

Set a dollar spend cap so the fleet halts if cost overruns:
```bash
# psql $FLEET_PG_DSN -c "UPDATE fleet_config SET spend_cap_usd=20.00 WHERE id=1;"
```

---

## Residuals (known limitations / follow-ups)

### R1 — Aggregator (push cross-check)

`sync.push_apply_eligible` reads the home SQLite brain.  Jobs pushed before the
brain is fully enriched / scored may arrive with lower scores than their final
value.  Re-run `push` after a scoring pass to refresh.  The ON CONFLICT clause
only updates `queued` rows, so already-leased or applied rows are not disturbed.

### R2 — approved_batch presence-stamp

The `approved_batch` column is a presence-stamp only (a string token), not a
foreign key to a batch table.  There is no batch-level cancel command — to revoke
an approval, set `approved_batch = NULL` directly:
```sql
UPDATE apply_queue SET approved_batch=NULL WHERE approved_batch='<token>' AND status='queued';
```

### R3 — Per-destination block risk

The adaptive governor (`rate_governor`) tracks per-host challenge_rate.  If a
host's challenge_rate crosses the pause threshold (default 60%) the governor marks
that host `paused` and `lease_apply` will skip it.  Run `applypilot-fleet-watchdog
--evaluate-breakers` after a batch to let the breaker sweep fire.

The LinkedIn lane (`lane='linkedin'`) is a separate mutex (`account:linkedin`) and
is NOT affected by canary state in `fleet_config`.  Never run both lanes
simultaneously on the same LinkedIn session.

### R4 — spend_cap_usd gate

`spend_cap_usd = 0` means NO cap (unlimited).  Always set a non-zero cap before
lifting the canary in production.  The cap is a HARD lease guard in the CTE
(`SUM(apply_queue.est_cost_usd) < spend_cap_usd`); workers never see it as a soft
warning — they simply stop leasing.

---

## Quick-reference command table

| Goal | Command |
|------|---------|
| Seed applied_set + sync results home | `apply-home pull` |
| Push eligible jobs to PG | `apply-home push [--score-floor N] [--limit N] [--include-research]` |
| Arm canary | `apply-home canary <K>` |
| Approve current batch | `apply-home approve --all-pushed` |
| Start apply worker | `applypilot-fleet-apply --dsn ... --worker-id ... --home-ip ...` |
| View queue depth + config | `apply-home status` |
| List open challenges | `apply-home challenges` |
| Resolve a challenge (retry) | `apply-home resolve-challenge <url>` |
| Resolve a challenge (skip) | `apply-home resolve-challenge <url> --skip` |
| Expand canary to N | `apply-home canary <N>` (resets paused to FALSE) |
| Lift canary (unlimited) | `apply-home lift-canary` |
