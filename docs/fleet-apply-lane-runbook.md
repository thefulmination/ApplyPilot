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

For a human-approved cohort, pass an explicit batch token so every staged row
is auditable and eligible for canary leasing:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" push --score-floor 7 --approved-batch "review-YYYYMMDD-01"
```

Do not use `--approved-batch` for merely `ready` review items; the token must
correspond to an actual human approval/export batch.

When a push reports zero staged rows, request the breakdown without changing
the normal approval gate:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" push --score-floor 5.8 --include-research --diagnostic
```

The diagnostic output separates candidate rows, `dedup_skipped`, and rows
actually touched, plus `weak_dedup_skipped` for applied-set entries with no
company, normalized role, or applied URL metadata. A zero `touched` count with
all candidates dedup-skipped is an intentional duplicate guard, not a push
failure; weak entries should be reviewed before any ledger change is considered.

List weak applied-set entries with their latest queue evidence:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" dedup-review --limit 100
```

This is read-only and does not remove or weaken dedup guards.

Verify the queue depth:
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN status
```

For a compact, read-only resume check, run:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" readiness
```

The command reports `ready`, explicit `blockers`, and `next_action` together
with queue depth, crash count, liveness-task freshness, OTP health, open
challenges, desired workers, and canary state. It never changes queue rows,
worker desired state, pause state, or canary settings. Treat any blocker as a
fail-closed reason to keep the apply fleet paused.

The daily unguard task is fail-closed and does not clear an operator pause by
itself. Clearing the operator lock requires an explicit, interactive action:

```powershell
.\fleet-daily-unguard.ps1 -Dsn "$FLEET_PG_DSN" -Force
```

Read `desired_control` before diagnosing a worker outage. It reports the
machine's configured worker count, agent/model, generation, and the last writer.
`apply_heartbeat_summary` reports the number of apply heartbeats and stale
heartbeats. `desired_workers=0` means the actuator is intentionally idle even if
the queue still contains rows; do not start a worker manually until the safety
pause and canary gates have been reviewed.

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

Inspect approved rows before resuming a canary. This is read-only and reports
per-row dedup/challenge blockers plus prior-attempt, requeue, and tool-call
blockers:
```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe canary-readiness --limit 25
```

Only rows with zero prior attempts, no prior requeue, and no prior browser tool
calls are canary-ready. A row that was previously leased must be reviewed or
resolved; it must not be submitted again merely because it is currently queued.

Quarantine unsafe approved rows after a dry run:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-unsafe-approved --limit 1000
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-unsafe-approved --limit 1000 --execute
```

Execution moves only rows with prior browser/requeue evidence to
`blocked/manual_review_required`, records an audit entry, and leaves the
original outcome unresolved. It does not retry or mark the application as
successful.

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

The pull summary includes `pending_remaining` and `batch_limited` when the
500-row sync batch is full. A result such as `failed: 500` is a processed
status count, not a pull error; use `pending_remaining` to see how many
terminal rows still need later cycles.

Review the K submissions in the brain or via:
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN status
```

Check for open auth challenges (captcha / login walls):
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN challenges
```

For a backlog review, list only unresolved challenges older than one day. This
is read-only and sorts the oldest rows first:
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN challenges --stale-days 1 --limit 25
```

To work the highest-value subset first, filter by wall type and sort by score:
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN challenges \
  --kind login_gate --priority --limit 25
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN challenges \
  --kind visible_captcha --priority --limit 25
```

Clear stale ATS challenge records whose queue rows are already terminal (this
does not requeue jobs or touch LinkedIn; dry-run first):
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN cleanup-terminal-challenges --older-days 1
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN cleanup-terminal-challenges --older-days 1 --execute
```

Retire stale frozen ATS challenge holds only after reviewing the dry run. This
marks the held rows `challenge_skipped`, resolves the challenge records as
`skipped`, and clears their leases; it never retries or marks a job applied:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" retire-stale-parked-challenges --older-days 1 --limit 1000
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" retire-stale-parked-challenges --older-days 1 --limit 1000 --execute
```

Clear stale lease metadata left on rows that are already terminally blocked:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" clear-blocked-leases --limit 1000
```

This is metadata repair only; it does not change the blocked outcome.

Repair a stale frozen ATS lease that has no owner-inbox record (also
dry-run-first; this creates metadata only and does not requeue the job):
```bash
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN repair-orphan-challenges --older-days 1
applypilot-fleet-apply-home --dsn $FLEET_PG_DSN repair-orphan-challenges --older-days 1 --execute
```

If a terminal blocked row still says `challenge_pending`, first verify the
latest challenge outcome. Resolved challenges with outcome `skipped` can be
normalized without reopening or retrying the job:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" normalize-resolved-challenge-blocks --limit 1000
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" normalize-resolved-challenge-blocks --limit 1000 --execute
```

This command only changes `apply_status/apply_error` to `challenge_skipped`,
keeps the queue row `blocked`, clears no safety guard, and never requeues a job.

For terminal rows carrying stale lease metadata, use the broader terminal cleanup
only after a dry run. It changes only lease columns and preserves every outcome:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" clear-terminal-leases --limit 5000
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" clear-terminal-leases --limit 5000 --execute
```

Rows with no durable result event must not be retried. Park malformed failures
for manual review instead:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-missing-evidence-failures --limit 1000
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-missing-evidence-failures --limit 1000 --execute
```

This changes only `failed:reason`-style rows with no result event to
`blocked/submission_uncertain:evidence_gap`; it never marks them applied or
requeues them. Use `quarantine-invalid-queued` for queued ATS rows with a
LinkedIn source URL or missing company/title identity.

For browser/playwright failures with no result event, use the corresponding
infrastructure evidence quarantine. It is also dry-run first and never retries
the rows:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-missing-infrastructure-evidence --limit 1000
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-missing-infrastructure-evidence --limit 1000 --execute
```

Untouched browser-preflight parks can be retried only through the bounded,
dry-run-first repair command below. It requires a latest result event with zero
browser calls, an elapsed cooldown, a low prior infrastructure-failure count,
and no matching `applied_set` dedup key:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" retry-infrastructure-pending --cooldown-hours 1 --max-failures 1 --limit 100
```

Use `--execute` only in a controlled recovery window after readiness is verified.
Rows with browser tool calls remain protected from automatic retry.

If an event exists but omits `application_tool_calls`, quarantine it separately;
the event is real but still does not prove that submission was untouched:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-incomplete-infrastructure-evidence --limit 1000 --execute
```

Failures whose latest event records browser calls are never retryable by the
repair tool until an operator establishes the outcome:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-tool-touched-infrastructure --limit 1000 --execute
```

Apply the same protection to explicit no-confirmation failures:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-no-confirmation-failures --limit 1000 --execute
```

Stale, expired, and missing-provenance inventory is non-retryable. Normalize it
out of the failed pool while retaining the original reason:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" normalize-terminal-inventory --limit 5000 --execute
```

Deterministic location, adapter, routing, and non-job policy failures can be
normalized separately; suspicious, stuck, authentication, and budget outcomes
remain protected for review:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" normalize-nonretryable-policy-failures --limit 1000 --execute
```

Suspicious pages and stuck flows are protected outcomes, not retry candidates:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-suspicious-stuck-failures --limit 1000 --execute
```

Auth failures whose latest challenge is already resolved can be normalized;
open or missing challenges are not touched:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" normalize-resolved-auth-failures --limit 1000 --execute
```

Budget, application-limit, and rate-limit failures are protected because they
may have stopped after form interaction:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-budget-limit-failures --limit 1000 --execute
```

Exact already-applied, spam-blocked, and missing-reference outcomes can be
normalized as terminal without changing dedup protection:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" normalize-clear-terminal-failures --limit 1000 --execute
```

Dedup-protected `dedup:already_applied` rows can be moved out of `failed`; the
`applied_set` guard is retained:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" normalize-dedup-terminal-failures --limit 2000 --execute
```

Final crash, auth, and tooling outcomes with no open challenge can be made
explicit protected review rows:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" normalize-final-protected-failures --limit 1000 --execute
```

After the targeted passes, park any remaining one-off failures for explicit
operator review:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" quarantine-residual-failures --limit 1000 --execute
```

For weak `applied_set` metadata, use the same dry-run-first pattern:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" backfill-dedup-metadata --limit 500
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" backfill-dedup-metadata --limit 500 --execute
```

Only missing company or applied URL fields are filled when queue evidence has
one unambiguous value. The dedup key is never deleted and protected rows remain
blocked; ambiguous entries are left for manual review.

Each new ATS lease also writes a `source='worker_lease'` lifecycle marker to
`apply_result_events`. If a worker later expires without a terminal result, the
status report shows `lease_started_no_terminal`. This improves diagnosis of new
crashes, but it is not proof that the form was untouched and must not be used to
auto-requeue the job.

Review `age_hours` and `parked` before taking action. A parked row is leased
deliberately so no worker can retry it blindly. Do not use `resolve-challenge`
until the owner has decided whether the challenge was solved (`resolve-challenge
<url>`) or the posting should be skipped (`resolve-challenge <url> --skip`).

Retire stale inventory only after reviewing the dry-run. This command selects
only ATS rows that are unapproved, older than the TTL, have zero attempts, and
have never had a lease timestamp; it cannot touch approved, leased, or attempted
jobs:
```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe retire-stale-unapproved --older-days 7 --limit 1000
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe retire-stale-unapproved --older-days 7 --limit 1000 --execute
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
| View actionable queue count | `apply-home status` → `queue_diagnosis.queued.base_leaseable` |
| List open challenges | `apply-home challenges` |
| Clean terminal ATS challenge records | `apply-home cleanup-terminal-challenges --older-days 1 --execute` |
| Repair orphaned ATS challenge metadata | `apply-home repair-orphan-challenges --older-days 1 --execute` |
| Resolve a challenge (retry) | `apply-home resolve-challenge <url>` |
| Resolve a challenge (skip) | `apply-home resolve-challenge <url> --skip` |
| Expand canary to N | `apply-home canary <N>` (resets paused to FALSE) |
| Lift canary (unlimited) | `apply-home lift-canary` |
