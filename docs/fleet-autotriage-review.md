# Fleet Autotriage Review

Autotriage never retries a `crash_unconfirmed` job unless durable evidence shows
`application_tool_calls = 0`. Jobs without that evidence are recorded as
`manual_review_required` and remain parked.

The home status command exposes the evidence split under `crash_diagnosis`:
`zero_tool_calls` is the only potentially safe requeue class, `tool_touched`
must remain parked, and `missing_evidence` / `no_result_event` require manual
review or better instrumentation. Watchdog lease reclaims now create a durable
result event with `source='watchdog'` and `execution_evidence='missing'` so new
crashes do not disappear from the audit trail.

`crash_diagnosis` also reports `older_7d`, `age_1d_to_7d`, and
`oldest_crash_at` so operators can work the oldest unresolved rows first.

New leases also create a `source='worker_lease'` marker. The repair report groups
a crash whose latest marker is `worker_lease` under `lease_started_no_terminal`.
That is better diagnosis, not permission to requeue: the marker proves only that
the worker received the job, not that the application form was untouched.

For the full read-only repair summary, including outcome-email matches and queue
gates, run:

```powershell
.\.conda-env\Scripts\applypilot-fleet-repair-report.exe `
  --dsn "$FLEET_PG_DSN" --sample-limit 5 --overbroad-limit 10
```

The matcher caches immutable job-field tokens because the report compares many
stored emails against the same crash rows; this keeps the report usable on the
full live backlog without changing its match rules.

Outcome reconciliation also recognizes an exact normalized job/application URL
in an email as per-job evidence. Existing Greenhouse and LinkedIn identifier
matching retains precedence; fuzzy company/domain matches remain review-only.

Run a bounded audit-only pass from the ApplyPilot checkout:

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
.\run-fleet-autotriage.ps1 -Once -CrashOnly -Limit 250 -WindowMinutes 60 -DisableLlm
```

Review the parked jobs and their evidence in Postgres:

```sql
SELECT q.url, q.company, q.title, q.worker_id, q.apply_error,
       q.updated_at, a.reason, a.created_at AS reviewed_at,
       e.application_tool_calls, e.job_log_path, e.transcript_digest
FROM apply_queue q
JOIN LATERAL (
  SELECT reason, created_at
  FROM autotriage_actions
  WHERE url = q.url AND action_status = 'manual_review_required'
  ORDER BY created_at DESC
  LIMIT 1
) a ON TRUE
LEFT JOIN LATERAL (
  SELECT application_tool_calls, job_log_path, transcript_digest
  FROM apply_result_events
  WHERE queue_name = 'apply_queue' AND url = q.url
  ORDER BY created_at DESC, id DESC
  LIMIT 1
) e ON TRUE
WHERE q.status = 'crash_unconfirmed'
ORDER BY q.updated_at ASC;
```

The same review is available without SQL, oldest first:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" crash-review --limit 25
```

To focus on the older backlog while keeping the highest-scoring jobs first:
```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" crash-review --older-days 7 --priority --limit 25
```

To stamp current public-posting liveness without changing crash state or retrying:
```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" crash-liveness --older-days 7 --refresh-days 7 --limit 25 --execute
```

The home task registration also installs a bounded evidence-only refresh every
30 minutes. It runs the equivalent of:

```powershell
.\run-fleet-crash-liveness.ps1 -Once -Limit 25 -OlderDays 7 -RefreshDays 7
```

To install or refresh the scheduled task, run this from an elevated PowerShell
on the home machine. Because the fleet is intentionally at zero desired workers,
the explicit `-AllowZero` is required and does not unpause or start workers:

```powershell
.\register-fleet-tasks.ps1 -Machine home -AllowZero
```

The liveness task can also be installed or refreshed independently from a
normal interactive PowerShell session:

```powershell
.\register-fleet-crash-liveness.ps1
```

The task is named `ApplyPilotFleet-CrashLiveness`, runs every 30 minutes, and
only stamps liveness fields. It cannot retry, resolve, or apply a job.

Each row includes the queued job metadata, latest `application_tool_calls`
value, transcript/log references, and latest autotriage action.

Useful evidence filters:

```powershell
# Rows with no durable result event (instrumentation backlog)
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" crash-review --evidence no_result_event --limit 25
# Rows that may be safe to requeue only after all other guards pass
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" crash-review --evidence zero_tool_calls --limit 25

# Review only rows whose public posting is still live or whose liveness is unknown.
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" crash-review --liveness live --limit 25
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" crash-review --liveness unchecked --limit 25
```

For an automated audit sweep, include `-CrashOnly` in the PowerShell wrapper:

```powershell
.\run-fleet-autotriage.ps1 -Once -CrashOnly -DisableLlm -Limit 500
```

That mode cannot issue actions for ordinary `failed` or `blocked` rows.
Each audit row stores the loader scope in `evidence->>'triage_scope'`, so
incident review can distinguish scheduled crash-only runs from older broad
passes.

Only after reviewing the worker log or transcript should an operator resolve a
row. A row with any application tool call, an applied-set match, or an outcome
email must remain parked to prevent duplicate submissions.

After the evidence review establishes the outcome, close exactly one row with an
explicit, audited operator decision:

```powershell
# The reason must identify the evidence reviewed. This does not retry the job.
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" resolve-crash <URL> `
  --outcome applied --reason "confirmation email matched application"

# Use failed/blocked only when the evidence supports that terminal outcome.
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" resolve-crash <URL> `
  --outcome failed --reason "operator verified posting was unavailable before submission"
```

`resolve-crash` refuses rows that are not still `crash_unconfirmed`, clears any
stale lease fields, writes an operator-sourced result event, and marks the row
for the next home pull. A dead public posting alone is not enough evidence to
resolve a crash row because the original submission may already have succeeded.

An automatic pre-touch requeue is single-use per job. If that retried job later
crashes again, autotriage records `manual_review_required` instead of looping
the same posting through repeated retries.

Rows with durable `application_tool_calls > 0` are also classified as
`manual_review_required`; an LLM suggestion to retry cannot downgrade that
evidence-backed safety decision to `no_action`.

When a crash row has a fresh, strong `dead` posting result but still has no
submission evidence, it can be removed from the crash counter without claiming
that the application failed. Review the dry run first:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" park-dead-crashes --older-days 7 --refresh-days 7 --limit 25
```

With `--execute`, eligible rows move to
`blocked:submission_uncertain`, receive an operator result event, and remain
explicitly unresolved. Rows with tool calls, applied-set entries, or inbox
outcomes are excluded. The dry-run output includes `guard_counts` so an empty
candidate set is explained by the exclusion guard that protected each row.

Rows protected only by an `applied_set` dedup marker can be explicitly
reclassified as unresolved submission uncertainty after reviewing the guard
counts. The dedup marker is retained and no outcome is inferred:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" park-dead-crashes --include-applied-set --older-days 7 `
  --refresh-days 7 --limit 25
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" park-dead-crashes --include-applied-set --older-days 7 `
  --refresh-days 7 --limit 25 --execute
```

For older crash rows whose only durable signal is the `applied_set` guard,
use the dedicated bulk report. It covers live, uncertain, and unchecked
postings, but still excludes tool-call, applied-event, and inbox evidence:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" park-protected-crashes --older-days 7 --limit 100
```

After reviewing `guard_counts`, add `--execute` to move those rows to
`blocked:submission_uncertain` while retaining `applied_set`.

For rows with browser/tool activity, add `--include-tool-touched` only when
the review confirms there is no applied event or inbox outcome. This still
does not infer the application result or permit a retry.

For rows with no positive execution evidence and no dedup marker, use the
evidence-gap command after reviewing its dry run. It is a classification-only
operation and excludes any row with tool calls, applied events, or inbox
outcomes:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" park-evidence-gap-crashes --older-days 0 --limit 100
```

Inbox-linked rows that the reconciler marks only as probable can be parked in
an explicit review state without flipping them to applied:

```powershell
.\.conda-env\Scripts\applypilot-fleet-apply-home.exe `
  --dsn "$FLEET_PG_DSN" park-email-review-crashes --older-days 0 --limit 100
```

`apply-home status` now includes `crash_liveness_task`, sourced from
`.fleet-logs\crash-liveness.log`, with the last exit code and freshness. A
healthy value means the last evidence-only pass succeeded within 90 minutes.
