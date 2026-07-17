# Canonical Decision Policy Runbook

This runbook promotes the reviewed FitMap, knowledge-graph, pairwise-preference, and
reviewed-outcome pipeline into fleet authority. ATS and LinkedIn are independent
lanes. Never substitute one lane's policy, queue counts, canary, or readiness for
the other.

## Safety invariants

- Keep ATS paused throughout backfill, scoring, replay, and validation.
- A legacy recommendation/import is evidence only; it cannot authorize an apply.
- Only a current, unexpired `action='apply'`, `qualification_verdict='qualified'`
  decision from the configured active lane policy may enter or leave a queue.
- Replay metrics are immutable. A changed input requires a new policy version.
- Promotion does not clear an operator pause or arm a canary.
- Promote ATS and LinkedIn separately. A passing ATS replay says nothing about
  LinkedIn readiness, and vice versa.
- Policy state, queue capacity, and admission proofs are lane-specific. Canary
  execution is temporarily serialized because fleet configuration still has one
  shared worker/version slot. Do not arm both lanes concurrently until the
  lane-specific worker-pin migration is installed and verified.

## 1. Confirm fail-closed state

Run from the Python runtime checkout:

```powershell
applypilot canonical status --dsn $env:BRAIN_STATUS_PG_DSN
```

`BRAIN_STATUS_PG_DSN` must use the read-only status principal.
`BRAIN_CONTROLLER_PG_DSN` must use the dedicated lifecycle controller principal;
do not reuse the schema migrator or a worker login for routine policy operations.
Schema v2 requires the pre-provisioned fixed `NOLOGIN` capability roles
`brain_status_reader` and `brain_policy_controller`; provision each DSN as a separate login that belongs
only to its matching capability role.
Each lifecycle login must have no other inherited roles, elevated role
attributes, direct function grants, public-schema creation authority, or table
mutation privileges. Runtime authorization and schema verification reject a
login that exceeds its single capability even when the capability role itself
is correctly configured.

The operator must grant `brain_schema_migrator` the exact fleet privileges used
by the security-definer lifecycle functions. `SELECT` needs grant option because
the migration installs the narrower status-reader grants:

```sql
GRANT SELECT ON TABLE
  public.fleet_config,
  public.fleet_decision_policies,
  public.workers,
  public.worker_heartbeat,
  public.fleet_worker_principals,
  public.fleet_desired_state,
  public.apply_queue,
  public.linkedin_queue,
  public.rate_governor,
  public.apply_result_events,
  public.apply_attempts,
  public.applied_set,
  public.fleet_worker_blocklist
TO brain_schema_migrator WITH GRANT OPTION;

GRANT UPDATE ON TABLE
  public.fleet_config,
  public.fleet_decision_policies,
  public.workers,
  public.worker_heartbeat,
  public.fleet_worker_principals,
  public.fleet_desired_state,
  public.apply_queue,
  public.linkedin_queue,
  public.rate_governor
TO brain_schema_migrator;

GRANT INSERT ON TABLE public.fleet_decision_policies
TO brain_schema_migrator;
```

These grants belong only to the non-login migrator/function-owner role. The
controller login receives no direct table privileges and can execute only the
three audited lifecycle wrapper functions. Schema verification fails when any
required function-owner grant is absent.

Confirm the intended lane remains paused or has zero canary capacity. Investigate
`fleet_error`, `missing_projection`, `mismatched_projection`, and queued rows with
missing or mismatched provenance before continuing.

## 2. Backfill reviewed evidence

Legacy backfill and outcome-review commands prepare the rebuildable SQLite cache
only. They cannot validate, promote, retire, stage, or lease a policy. Import the
sealed cache into Postgres with the reviewed SQLite-to-Postgres importer and pass
its parity gates before continuing.

Prepare legacy research artifacts in the local cache when needed:

```powershell
applypilot canonical cache-backfill <artifact-directory>
```

Outcome artifacts do not self-authorize. Review a candidate explicitly before it
can become accepted outcome training evidence:

```powershell
applypilot canonical cache-outcome-review <message-id> --resolution trusted
```

Use `corrected` with `--job-url` and `--stage` when attribution or stage is wrong;
use `ignored` for non-outcome mail.

## 3. Score one immutable draft per lane

Run from the TypeScript scoring checkout, using a new version for every changed
input snapshot:

```powershell
npm run applypilot:canonical:score -- --policy-version=<ats-version> --lane=ats --source=brain --knowledge-graph=<kg.json> --artifact-manifest=<manifest.json>
npm run applypilot:canonical:score -- --policy-version=<linkedin-version> --lane=linkedin --source=brain --knowledge-graph=<kg.json> --artifact-manifest=<manifest.json>
```

Do not use JSON fallback for authority. It is analysis-only and requires
`--dry-run`.

## 4. Replay each lane

Use a reviewed, versioned hard-negative corpus. The authoritative replay writes
locked metrics to the draft policy:

```powershell
npm run applypilot:canonical:replay -- --policy-version=<ats-version> --lane=ats --source=brain --hard-negatives=<ats-hard-negatives.json>
npm run applypilot:canonical:replay -- --policy-version=<linkedin-version> --lane=linkedin --source=brain --hard-negatives=<linkedin-hard-negatives.json>
```

Each lane must independently pass all four locked gates:

1. zero hard-negative applies
2. zero title-only promotions
3. grounded support for required qualifications
4. canonical performance exceeds the actual legacy stream

A failed gate requires corrected evidence/configuration and a new policy version.

## 5. Validate, then promote explicitly in Postgres

Run from the Python runtime checkout:

```powershell
applypilot canonical validate <ats-version> --dsn $env:BRAIN_CONTROLLER_PG_DSN
applypilot canonical promote <ats-version> --lane ats --to canary --dsn $env:BRAIN_CONTROLLER_PG_DSN

applypilot canonical validate <linkedin-version> --dsn $env:BRAIN_CONTROLLER_PG_DSN
applypilot canonical promote <linkedin-version> --lane linkedin --to canary --dsn $env:BRAIN_CONTROLLER_PG_DSN
```

Each command advances exactly one locked lifecycle edge through a dedicated
`brain_controller_*` security-definer wrapper. The same Postgres database must contain the brain,
fleet configuration, lane policies, and both queues. SQLite is never opened by
status, validation, promotion, or retirement.

The lifecycle transition stages but does not open the canary. With both lanes
still stopped and all leases at zero, arm one bounded lane explicitly:

```powershell
applypilot canonical canary-arm <ats-version> --lane ats --capacity 20 `
  --expected-ats-pause-source <exact-source-from-status> `
  --dsn $env:BRAIN_CONTROLLER_PG_DSN
```

When status reports SQL `NULL`, use `--expect-null-ats-pause-source` instead of
`--expected-ats-pause-source`. The two options are mutually exclusive.

`canary-arm` requires the exact canary policy, a pinned release version, a fresh
desired validated lane-capable worker heartbeat at that version, a worker public
IP, a currently leaseable reviewed job with every queue/governor/spend gate
satisfied, and zero outstanding lease fields in both lanes. ATS additionally
requires the exact pause source reported by the immediately preceding status read;
the compare-and-set prevents the command from silently clearing a changed operator
or incident hold. It opens only the selected lane. Stop and globally pause
immediately after the sample finishes:

```powershell
applypilot canonical canary-stop --lane ats --dsn $env:BRAIN_CONTROLLER_PG_DSN
```

Review the ATS evidence before repeating the same sequence for LinkedIn with its
own policy, worker, capacity, and result set. Never arm both lanes together.

After the lane-specific canary is reviewed and accepted, while globally paused
and with both lanes stopped, advance only that lane:

```powershell
applypilot canonical promote <ats-version> --lane ats --to active --dsn $env:BRAIN_CONTROLLER_PG_DSN
applypilot canonical promote <linkedin-version> --lane linkedin --to active --dsn $env:BRAIN_CONTROLLER_PG_DSN
```

Do not run `--to active` from `validated`; the command and database both reject a
skipped canary lifecycle.

## 6. Pin and verify the fleet

Publish the runtime branch to every remote actually tracked by the workers. Update
each checkout, then set `fleet_config.pinned_worker_version` to that exact version.
Do not infer compatibility from a branch name: compare the pin with fresh
`worker_heartbeat.sw_version` values and verify queue movement only after the pin
matches.

Run status again and verify, per lane:

```powershell
applypilot canonical status --dsn $env:BRAIN_STATUS_PG_DSN
```

- configured fleet policy equals the promoted lane policy
- no queued row lacks complete canonical provenance
- no queued row references another policy
- no lease can be acquired while its lane pause/canary blocks work
- operator pauses remain unchanged

Do not reopen ATS as a side effect of promoting LinkedIn, or LinkedIn as a side
effect of promoting ATS.

## Rollback

Retire only the affected lane policy:

```powershell
applypilot canonical retire <policy-version> --dsn $env:BRAIN_CONTROLLER_PG_DSN
```

Retirement is one Postgres transaction. It preserves operator pause controls,
retires the matching brain and fleet policy, clears the lane binding, and removes
canonical authority from unleased queued rows. Diagnose a failed transaction and
retry; do not manually clear a lane pause or reuse invalidated queue rows.
