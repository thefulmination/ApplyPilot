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

## 1. Confirm fail-closed state

Run from the Python runtime checkout:

```powershell
applypilot canonical status --dsn $env:FLEET_PG_DSN
```

Confirm the intended lane remains paused or has zero canary capacity. Investigate
`fleet_error`, `missing_projection`, `mismatched_projection`, and queued rows with
missing or mismatched provenance before continuing.

## 2. Backfill reviewed evidence

Backfill research artifacts into the authoritative brain database:

```powershell
applypilot canonical backfill <artifact-directory>
```

Outcome artifacts do not self-authorize. Review a candidate explicitly before it
can become accepted outcome training evidence:

```powershell
applypilot canonical outcome-review <message-id> --resolution trusted
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

## 5. Validate, then promote explicitly

Run from the Python runtime checkout:

```powershell
applypilot canonical validate <ats-version>
applypilot canonical promote <ats-version> --lane ats --dsn $env:FLEET_PG_DSN

applypilot canonical validate <linkedin-version>
applypilot canonical promote <linkedin-version> --lane linkedin --dsn $env:FLEET_PG_DSN
```

Promotion stages Postgres first, activates the SQLite policy, and then marks the
fleet policy active. A Postgres staging failure leaves SQLite validated. A final
Postgres failure leaves the fleet non-active and fail-closed; rerunning promotion
recovers the already-active SQLite policy.

## 6. Pin and verify the fleet

Publish the runtime branch to every remote actually tracked by the workers. Update
each checkout, then set `fleet_config.pinned_worker_version` to that exact version.
Do not infer compatibility from a branch name: compare the pin with fresh
`worker_heartbeat.sw_version` values and verify queue movement only after the pin
matches.

Run status again and verify, per lane:

```powershell
applypilot canonical status --dsn $env:FLEET_PG_DSN
```

- configured fleet policy equals the promoted lane policy
- no queued row lacks complete canonical provenance
- no queued row references another policy
- no lease can be acquired while its lane pause/canary blocks work
- operator pauses remain unchanged

Only then arm a small lane-specific canary. Do not reopen ATS as a side effect of
promoting LinkedIn, or LinkedIn as a side effect of promoting ATS.

## Rollback

Retire only the affected lane policy:

```powershell
applypilot canonical retire <policy-version> --dsn $env:FLEET_PG_DSN
```

Retirement pauses that lane and invalidates its queued rows before changing the
SQLite policy. If Postgres retirement fails, SQLite remains active so the two
stores do not silently disagree. Diagnose the database failure and retry; do not
manually clear the lane pause or reuse invalidated queue rows.
