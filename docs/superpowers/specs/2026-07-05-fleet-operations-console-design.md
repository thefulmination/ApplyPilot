# Fleet Operations Console Design

## Purpose

Rebuild the ApplyPilot fleet console into an operator-grade dashboard that answers four questions without requiring SQL, logs, or guesswork:

1. Is the fleet applying right now?
2. If not, why not?
3. Which machines, workers, agents, models, queues, and safety rails are responsible?
4. What is the safest next action?

The current console already exposes useful raw state through `src/applypilot/fleet/console_app.py`, but it requires the operator to interpret counters such as queued, leasable, challenges, worker heartbeats, Doctor fixes, and recent logs. This design adds a diagnosis layer and a clearer UI while preserving the current LAN-only, dependency-free, allow-listed safety model.

## Current System Context

The existing fleet console is a single stdlib HTTP app:

- Backend: `src/applypilot/fleet/console_app.py`
- Frontend: embedded HTML/CSS/JS string in the same module
- Launcher: `run-fleet-console.ps1`
- Bind safety: private IPv4 or loopback only
- Mutation safety: POST `/api/action` requires a token and routes only through `_ACTIONS`
- Existing read endpoints: `/api/status`, `/api/challenges`, `/api/logs`, `/api/diagnostics`, `/api/outcomes`

Fleet data is primarily in Postgres and includes:

- `apply_queue`, `linkedin_queue`, `applied_set`
- `worker_heartbeat`
- `auth_challenge`
- `fleet_config`
- `rate_governor`
- `agent_availability`
- `llm_usage`
- `fleet_knobs`, `fleet_diagnoses`

The existing UI has the right data ingredients, but the top-level experience is too raw. For example, a large queued count can hide zero leaseable jobs because every queued row is already protected by `applied_set`.

## Design Principles

1. Read-only diagnosis first.
   The dashboard should explain state before offering any control.

2. Keep live write controls narrow.
   New views may recommend actions, but new mutations require explicit implementation review and must go through the existing token and allow-list pattern.

3. Make safety rails visible.
   Canary, spend cap, pause flags, LinkedIn separation, dedup guards, and challenge parking must be first-viewport concepts.

4. Prefer evidence over interpretation.
   Every diagnosis should show the counter or table behind it: queue counts, heartbeat age, provider spend, challenge rows, agent blocks, or recent result rows.

5. Make stale data obvious.
   The console should show polling freshness, DB freshness, endpoint failures, and stale worker/machine heartbeats.

6. Preserve the stdlib deployment path.
   The first implementation should stay inside the current Python stdlib console. A React/Vite console can be a later migration if needed.

## Information Architecture

### 1. Fleet State Summary

The first viewport should contain a plain-English state summary:

- `Applying normally`
- `Idle but healthy`
- `Idle: no leaseable ATS jobs`
- `Idle: canary exhausted`
- `Paused by operator`
- `Halted by spend cap`
- `ATS paused by Fleet Doctor`
- `Challenge blocked`
- `Worker/browser degraded`
- `All agents blocked`
- `Dashboard stale`

The summary should also include:

- last generated timestamp
- next expected worker beat window
- last successful apply
- live apply worker count
- live compute worker count
- live discovery worker count
- immediate next recommended action

### 2. Why Not Applying

This is the most important new panel. It should decompose the apparent queue into lease eligibility:

- total ATS queued
- queued and approved
- queued but unapproved
- leaseable now
- dedup-blocked by `applied_set`
- blocked by company/site exclusion
- blocked by host pause/demotion
- blocked by Doctor host skip
- blocked by host/home/global min-gap
- blocked by daily cap
- blocked by canary remaining at zero
- parked in challenge state
- stale leased rows

The panel should explain cases like:

`756 queued ATS jobs, 0 leaseable: all queued approved rows are already protected by applied_set.`

LinkedIn should have its own parallel explanation:

- queued
- approved
- leaseable
- dedup-blocked
- canary enabled
- `linkedin_canary_remaining`
- account halt state
- open LinkedIn challenges

LinkedIn remains read-only in this console.

### 3. Agent Routing And Dynamic Switching

Add a first-class section showing which apply agent and model each worker is using.

Required display:

- worker id
- machine owner
- lane
- configured agent chain, such as `claude -> codex -> deepseek`
- current effective agent
- current model, such as `sonnet`
- last switch time
- last switch reason
- current wall/block status
- blocked-until time by agent
- block reason: `usage_limit_wall` or `predictive_spend`
- rolling spend by provider from `llm_usage.provider`
- apply count and failure count by provider/model

Dynamic switching verdicts:

- `working`: a preferred agent became blocked and another agent produced later work
- `not triggered`: no recent wall or block required a switch
- `blocked`: every configured agent is blocked
- `partial`: blocks exist, but no later successful fallback is visible
- `misconfigured`: worker is single-agent while fleet policy expects fallback
- `unknown`: heartbeat/schema lacks current agent/model telemetry

Implementation note:

The code already has `AgentSwitcher`, `agent_availability`, and `llm_usage.provider`. The dashboard should add minimal telemetry so `worker_heartbeat` can report live agent/model fields instead of forcing the UI to infer them from logs. Proposed additive columns:

- `current_agent TEXT`
- `current_model TEXT`
- `agent_chain TEXT`
- `last_agent_switch_at TIMESTAMPTZ`
- `last_agent_switch_reason TEXT`

This is additive and read-only from the console. Workers own writes to these fields.

### 4. Machine Health Map

Show the fleet by machine, not only by worker id:

- home desktop
- m2 / tarpon
- m4 / GGGTower
- Mac
- watchdog
- Doctor
- discovery
- compute
- apply

Per machine:

- Tailscale known address, when available from local probe data
- last heartbeat
- live/stale/down
- roles running
- worker count by role
- current busy count
- browser health summary
- recent error count
- last successful apply
- last discovery
- last compute result

Tailscale status should be optional and local-only. If unavailable, the console should say `Tailscale status unavailable` instead of blocking dashboard load.

### 5. Browser Backend Health

Browser/backend failures have been a real operational blocker, so they need their own section.

Classify recent worker logs and terminal statuses into:

- browser backend crashed
- browser service unavailable
- Playwright/MCP disconnected
- file chooser crash
- connection refused
- timeout after browser action
- CAPTCHA solver unsupported
- CAPTCHA present
- login gate
- email/OTP verification
- employer application cap
- usage limit
- no result line

Show:

- count by worker
- count by machine
- count by host
- newest example
- recommended next action

This is read-only. It can be derived from `worker_heartbeat.recent_log`, `worker_heartbeat.last_error`, `apply_queue.apply_error`, `auth_challenge`, and recent terminal rows.

### 6. Challenge Workbench

Improve the existing challenges page into a triage workbench:

- group by lane, kind, host
- show count, oldest age, newest age
- show company/title/score when available
- show machine and worker that raised it
- show screenshot link when available
- show whether row is an open `auth_challenge`, a parked queue row, or both
- show safe actions: open job, requeue solved row, skip row, skip host

Kinds:

- visible CAPTCHA
- login gate
- OTP/email verification
- no challenge row but parked
- other/unknown

LinkedIn challenge actions must remain limited to resolving parked challenge rows. No LinkedIn apply, scrape, arm, or resume control should be added.

### 7. Queue Funnel

Create a funnel view for the full pipeline:

- discovered
- staged for ingest
- scored
- approved
- queued
- leaseable
- leased
- applied
- failed
- crash unconfirmed
- challenge parked
- blocked

Show separate ATS and LinkedIn funnels. ATS can include action recommendations. LinkedIn stays read-only.

### 8. Safety Rails

Create a dedicated safety rail strip near the top:

- global paused
- ATS paused
- ATS pause source
- canary enabled
- canary remaining
- LinkedIn canary enabled
- LinkedIn canary remaining
- spend cap
- spend used
- deadman alert
- Doctor auto-fix count
- applied_set/dedup guard health
- OpenAI/company exclusion guard status if exposed by config

The panel should distinguish:

- operator pause
- spend cap halt
- Doctor ATS pause
- canary depletion
- lane-specific LinkedIn canary depletion

### 9. Recommended Next Action

The console should calculate one or more recommendations from read-only diagnostics.

Examples:

- `Reconcile dedup-blocked queued rows before restarting workers`
- `Re-arm LinkedIn canary if you want LinkedIn active`
- `Solve 27 Indeed login gates before retrying Indeed`
- `Restart m4 browser backend; m4 workers report connection refused`
- `Run remediator dry-run for crash_unconfirmed rows`
- `Start discovery on home/m2; discovery heartbeat is stale`
- `No action needed: workers are live and leaseable jobs are available`

Each recommendation should include:

- severity
- affected lane
- reason
- evidence count
- exact runbook command when available
- whether it is read-only, safe mutation, or live operation for the user to run manually

The console should not execute mutating recommendations unless a separate, explicit allow-listed action is implemented and reviewed.

### 10. Recent Applies Timeline

Show successful and terminal attempts with:

- time
- worker
- machine
- lane
- agent
- model
- status
- company
- title
- host
- estimated cost
- duration
- apply channel when available

This should answer: `What actually happened today?`

### 11. Failure Clusters

Extend the Doctor/diagnostics area with a more operator-friendly clustering view:

- reason
- host
- machine
- agent/model
- samples
- first seen
- last seen
- current auto-fix if any
- recommendation if any

This should consume existing `fleet_diagnoses`, `fleet_knobs`, recent queue errors, and heartbeat logs.

### 12. Host And Source Quality

Show which boards/hosts are producing value versus blockers:

- applies by host/source
- failures by host/source
- challenges by host/source
- crash rate
- CAPTCHA rate
- login gate rate
- queued remaining
- leaseable remaining

This helps tune search sources and decide which hosts should be skipped, paced, or prioritized.

### 13. Discovery Health

Show:

- discovery workers live/stale
- search tasks total/enabled/due
- discovered postings total
- pending ingest
- last 24h discovered
- last discovery time
- top sources found
- stale discovery warning

The console may keep the existing `Expand searches` action because it already queues PG search work and submits nothing.

### 14. Compute Health

Show:

- compute workers live/stale
- scoring backlog by status
- last scored job
- failed scoring count
- compute throughput last 1h/24h
- model/provider used for scoring when available

This should make it clear whether m4 is actually doing scoring work or simply idle because the scoring queue is empty.

### 15. Operator Audit Log

Add a read-only audit section showing console-driven operator actions:

- pause/resume
- arm/lift canary
- set spend cap
- reclaim
- expand searches
- challenge requeue/skip
- Doctor reverse/dismiss

If a durable audit table already exists, read from it. If not, add a small append-only `fleet_console_audit` table and write one row inside each existing action function. Do not log secrets or DSNs.

### 16. Throughput Forecast

Add a lightweight forecast:

- applies in last 1h
- applies in last 24h
- current live apply workers
- leaseable jobs
- open challenges
- estimated applies/hour if current lane remains unblocked
- estimated time to exhaust leaseable queue

This is advisory only and should label assumptions clearly.

### 17. Daily Goals

Add an optional daily target strip:

- applied today
- target today
- remaining target
- canary remaining
- projected shortfall

If no target config exists, show `No daily target configured` and do not block anything.

### 18. Worker Comparison

Show worker-level comparisons:

- applies
- failures
- challenges raised
- browser crashes
- average duration
- estimated cost
- current agent/model
- last heartbeat

This should make one bad worker or bad machine obvious.

### 19. Data Freshness

Every major panel should show freshness:

- dashboard poll age
- DB query generated at
- worker last beat
- last apply
- last discovery
- last compute
- endpoint error state

If `/api/status` succeeds but `/api/diagnostics` fails, the UI should show diagnostics as stale instead of silently keeping old data.

## Backend Data Model Changes

Prefer read-only derivations from existing tables. Add schema only where current telemetry is missing.

### Additive Worker Heartbeat Fields

Add to `worker_heartbeat`:

- `current_agent TEXT`
- `current_model TEXT`
- `agent_chain TEXT`
- `last_agent_switch_at TIMESTAMPTZ`
- `last_agent_switch_reason TEXT`

Workers update these fields on heartbeat. The console reads them only.

### Optional Console Audit Table

Add only if no existing durable audit table is appropriate:

```sql
CREATE TABLE IF NOT EXISTS fleet_console_audit (
    id BIGSERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    actor TEXT,
    lane TEXT,
    target TEXT,
    message TEXT,
    ok BOOLEAN NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

No secrets, DSNs, tokens, prompts, or raw browser logs go into this table.

## Backend Endpoints

Keep current endpoints and add or extend read-only payloads.

### `/api/status`

Fast, small, frequent polling.

Include:

- top fleet state
- safety rails
- worker summary
- queue summary
- agent/model summary
- freshness
- deadman alert
- next recommendation summary

### `/api/diagnosis`

New read-only endpoint for heavier derived diagnosis.

Include:

- why-not-applying breakdown
- queue eligibility breakdown
- browser health classification
- recommendations
- host/source quality
- throughput forecast
- worker comparison

This endpoint should not run expensive log parsing on every 4 second poll. Poll every 15-30 seconds or refresh manually.

### `/api/agents`

Read-only agent/model route state.

Include:

- per-worker agent/model telemetry
- agent availability rows
- rolling spend by provider/model
- switch verdict
- recent provider/model terminal attempts

This may be folded into `/api/diagnosis` if the payload stays small.

### Existing Endpoints

Keep:

- `/api/challenges`
- `/api/logs`
- `/api/diagnostics`
- `/api/outcomes`
- `/api/action`

Do not add LinkedIn apply controls.

## Frontend Layout

The dashboard should be an operational tool, not a marketing page.

### Header

- product name
- LAN-only warning
- connection state
- generated-at/freshness
- token-expired/deadman banners

### First Viewport

- fleet state summary
- next recommended action
- safety rails strip
- live worker counts by lane
- last successful apply
- leaseable queue count

### Primary Tabs Or Sections

1. Overview
2. Why not applying
3. Agents and models
4. Machines and workers
5. Challenges
6. Queues and funnel
7. Browser health
8. Discovery
9. Compute
10. Doctor and recommendations
11. Outcomes and recent applies
12. Audit

Use dense tables, compact cards, clear status labels, and stable dimensions. Avoid nested cards and decorative dashboard chrome.

## Error Handling

Backend:

- Each endpoint opens short-lived DB connections and rolls back read-only transactions.
- Endpoint failures return structured JSON errors.
- A failed optional diagnostic should not break `/api/status`.
- Text from logs and errors is scrubbed and capped.
- All SQL remains parameterized.

Frontend:

- Show stale state per panel.
- Preserve last good render but mark it stale.
- Show endpoint-specific errors.
- Token expiration shows a fixed banner.
- Deadman alert shows a fixed banner.
- Empty state copy should explain whether data is absent, stale, or unavailable.

## Safety Constraints

The redesign must preserve these constraints:

- No live brain SQLite reads from this console.
- No hidden apply, discover, score, push, approve, or pull actions.
- No LinkedIn write controls.
- No broad raw SQL mutation from request input.
- No action dispatch through `eval`, `getattr`, or unchecked request names.
- No DSNs, tokens, API keys, resume/profile data, or secrets in logs, audit rows, or HTML.
- No weakening of dedup/double-apply, OpenAI/company exclusion, canary, or challenge guards.

## Testing Plan

Add focused tests before implementation changes:

1. Diagnosis tests
   - queued approved rows blocked by `applied_set` produce `no_leasable_dedup_blocked`
   - canary zero produces canary-exhausted diagnosis
   - LinkedIn canary zero is lane-specific and read-only
   - host/Doctor/rate-governor blocks are counted separately

2. Agent/model tests
   - `agent_availability` active blocks appear in `/api/agents`
   - `llm_usage.provider` produces rolling spend by agent
   - worker heartbeat agent/model fields render in status payload
   - all-agents-blocked verdict is detected
   - fallback-success verdict is detected from post-block provider activity

3. Browser health tests
   - known browser crash strings classify correctly
   - CAPTCHA/login/OTP/employer-cap strings classify correctly
   - scrubbed log caps remain enforced

4. Console page smoke tests
   - HTML includes new sections
   - JS fetches new read endpoints
   - token-gated actions still send same-origin credentials
   - LinkedIn has no write action

5. Safety tests
   - `_ACTIONS` allow-list does not grow unexpectedly
   - new diagnosis endpoints are GET/read-only
   - bind safety and token tests remain green
   - audit rows do not contain tokens or DSNs

6. Existing regression suite
   - console tests
   - fleet queue tests
   - worker switching tests
   - agent budget tests
   - challenge triage tests

## Rollout Plan

Phase 1: Read-only diagnosis model.

- Add backend diagnosis helpers.
- Add tests for queue eligibility and recommendations.
- Add no UI actions.

Phase 2: Agent/model telemetry.

- Add additive heartbeat columns.
- Update worker heartbeat writes.
- Add `/api/agents` or equivalent diagnosis payload.
- Add tests for dynamic switching visibility.

Phase 3: UI makeover.

- Rebuild the embedded HTML/CSS/JS into the new operator layout.
- Keep dependency-free stdlib deployment.
- Preserve existing action controls and token behavior.

Phase 4: Audit and polish.

- Add operator audit table if needed.
- Add host/source quality, throughput forecast, daily goals, and worker comparison.
- Add stale panel handling.

Phase 5: Verification.

- Run targeted tests.
- Start console locally.
- Verify desktop and narrow viewport.
- Confirm no live fleet mutation occurred during verification unless explicitly invoked by the user.

## Out Of Scope

- Replacing the stdlib console with React/Vite in this pass.
- Adding new LinkedIn write controls.
- Automatically mutating queued rows to fix dedup-blocked state.
- Automatically restarting remote machines or browser backends.
- Changing apply scoring or selection policy beyond read-only reporting.
- Reading or modifying the live brain SQLite DB.

## Acceptance Criteria

- The first viewport states whether the fleet is applying and why.
- The dashboard explains zero leaseable jobs even when queued count is high.
- Agent/model routing and dynamic switching are visible with evidence.
- Browser/backend health is visible by worker and machine.
- Machine health, queue funnel, challenge workbench, safety rails, recommendations, recent applies, failure clusters, source quality, discovery, compute, audit, forecast, daily goals, worker comparison, and freshness are represented.
- All new diagnosis data is read-only unless routed through the existing mutation allow-list.
- LinkedIn remains read-only except existing challenge resolution primitives.
- Tests cover diagnosis, agent/model telemetry, browser classification, page smoke, and safety guards.
