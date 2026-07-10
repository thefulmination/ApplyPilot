# Apply cost reduction phase 2 design

**Approved direction:** adapter-first hybrid routing. The owner authorized whatever work is
necessary to minimize cost per high-quality, positively verified application.

## Goal

Reduce ApplyPilot's all-in cash cost per positively verified application below `$1.00` without
increasing duplicate submissions, fabricated answers, false `applied` states, or unbounded human
recovery work. Continue toward `$0.50` by moving stable ATS traffic off the general browser agent.

This phase keeps local Playwright and Chrome as the primary browser stack. It does not migrate the
fleet to Selenium, Puppeteer, a hosted browser, an anti-detection browser, or a vision agent. The
31-option research found that those choices can change browser infrastructure, but they do not
remove ApplyPilot's dominant model-turn, expired-posting, host-policy, and ambiguous-result costs.

## Evidence and baseline

Fresh read-only production evidence on 2026-07-09:

- `909` positively applied rows from `6,373` terminal attempts.
- `$1,855.2859` recorded fleet cost.
- `$2.0410` all-in cost per positively applied row.
- Ashby: `358` applied at `$0.7751` fleet cost per apply.
- Greenhouse: `239` applied at `$1.6495` fleet cost per apply.
- Workday: `18` applied at `$14.6340` fleet cost per apply.
- Other hosts: `267` applied at `$3.1865` fleet cost per apply.
- Agent/browser runtime failures: `$310.2143`.
- Preflight/policy failures: `$289.3490`.
- Explicit email/auth failures: `$42.1457`, a second-order bucket.

Historical modeling against the same queue shows:

- Removing Workday unattended spend alone would have lowered the historical figure to about
  `$1.7860`, while losing only the small Workday success slice.
- Eliminating paid preflight and browser-runtime failures, together with the Workday gate, would
  have lowered it to about `$1.2428`.
- Applying a 75% cost reduction to Ashby and Greenhouse after those gates would have lowered it to
  about `$0.8051`.

These are planning sensitivities, not claims about future production results. Promotion decisions
use new route-tagged canaries and the full all-in denominator.

Research inputs are stored under `applypilot-webdriver-cost-research/`; the consolidated report is
`applypilot-webdriver-cost-research/report.md`.

## Alternatives considered

### 1. Adapter-first hybrid router - selected

Use read-only liveness and host-policy gates before Chrome, deterministic Playwright adapters for
stable ATS forms, an independent verifier after submit, a low-cost agent only for incomplete forms,
and the current premium agent as the final fallback.

This approach reuses the existing Python, Playwright, Chrome profile, answer-plan, queue, and test
infrastructure. It directly attacks the measured waste buckets and preserves a controlled escape
path for unfamiliar forms.

### 2. Agent/model optimization only - rejected as the primary strategy

Codex and lower-context Playwright control can reduce marginal model cash cost. Historical success
was comparable to Claude in aggregate, but Codex also produced a much higher
`crash_unconfirmed` share during misconfigured and timeout-heavy runs. Model switching does not
stop expired jobs, Workday failures, or ambiguous retries. It remains a guarded experiment after
route telemetry and submit verification are deployed.

### 3. Driver or hosted-browser migration - rejected as the primary strategy

Selenium, WebDriver BiDi, Puppeteer, raw CDP, Browserbase, Browserless, Steel, Cloudflare, Apify,
and Bright Data can provide compatibility, capacity, or observability. They add migration or
provider cost without supplying ATS semantics, answer provenance, or positive confirmation. A
hosted browser may be canaried later for a reproducible local-browser reliability problem only.

## Architecture

The apply path becomes a fail-closed route ladder:

1. **Lease and dedup gate**
   - Keep the current queue lease, approval, `applied_set`, and effective-target dedup rules.
   - Never create a second application attempt for a dedup key with a verified or ambiguous prior
     submit.

2. **Read-only preflight**
   - Run the existing `apply.liveness.probe_url()` before `apply_fn` in the fleet-v3 `WorkerLoop`.
   - `dead` closes the lease at zero model cost with route `preflight` and a durable reason.
   - `live`, `uncertain`, blocked HTTP, server errors, and probe exceptions continue to routing.
   - The probe remains GET-only and never logs in, fills, posts, or bypasses access controls.

3. **Host policy**
   - Workday is not eligible for unattended general-agent application unless a tenant is explicitly
     trusted.
   - Ashby, Greenhouse, and proven Workable forms are eligible for adapter routing.
   - Lever remains deterministic fill-only while hCaptcha owns the submit boundary.
   - Low-success long-tail hosts receive small canary caps or supervision rather than unrestricted
     premium-agent spend.

4. **Deterministic adapter**
   - Use the existing `AnswerPlan` boundary: discover current fields, map evidence-backed answers,
     refuse unmapped required fields, fill, checkpoint, submit once, and verify.
   - Productionize Greenhouse first because guarded discovery, planning, filling, and submit code
     already exists.
   - Build Ashby next with the same protocol and fail-closed rules.
   - Browser-assisted HTTP is allowed only inside a certified ATS adapter with an allowlisted
     request contract. It is not a generic network-replay route.

5. **Independent verifier**
   - Acting and verification are separate components.
   - Evidence ranks from strongest to weakest:
     1. allowlisted successful submit response with an application or request identifier;
     2. known success URL plus allowlisted success state;
     3. allowlisted confirmation DOM;
     4. matched confirmation email;
     5. screenshot or disabled-button inference, which is supporting evidence only.
   - An adapter may report `applied` only from evidence tiers 1-4.
   - Missing confirmation after `submit_started` becomes `crash_unconfirmed`, never agent fallback.

6. **Low-cost fallback**
   - Incomplete adapter plans may fall through before submit.
   - The first fallback experiment uses the current local Playwright/Chrome investment, not a new
     browser provider.
   - Codex or Playwright CLI may run in shadow/canary with strict tool, time, step, and host caps.
   - Claude plus Playwright MCP remains the premium route for approved high-value forms that cheaper
     routes cannot complete.

7. **Authenticated recovery**
   - Login, OTP, and account-required jobs route to owner-controlled dedicated profiles.
   - Remote workers do not create accounts or solve login walls independently.
   - Indeed and LinkedIn use dedicated profiles and concurrency guards; Workday remains per tenant.

## Component boundaries

### `apply.liveness`

Owns read-only `live | dead | uncertain` classification. It has no queue or submit authority.

### `fleet.worker.WorkerLoop`

Owns the pre-agent preflight integration. It receives an injected `preflight_fn`, closes only strong
dead results, and otherwise preserves current apply behavior. Tests can inject a pure fake.

### `apply.adapter_protocol`

Defines the shared deterministic contract:

- `supports(url)`
- `discover(page, job)`
- `plan(fields, profile, resume, approved_answers)`
- `fill(page, plan)`
- `prepare_submit(page, plan)`
- `submit_once(page, attempt_id)`
- `collect_evidence(page, response)`

Greenhouse and Ashby implementations depend on this protocol. The protocol has no queue access;
the launcher owns route selection and result translation.

### `apply.submission_verifier`

Consumes normalized evidence and returns `verified`, `unverified`, or `contradicted` plus a stable
method and reference. It never clicks or fills the form.

### `fleet.apply_attempts`

Provides the durable submit boundary. Before an irreversible click, the worker persists
`submit_started`. A later process must quarantine an unresolved `submit_started` attempt instead of
retrying.

### `fleet.cost_quality_report`

Adds route, adapter, preflight, verifier, agent, model, worker, and browser-provider comparisons.
Every route promotion uses all terminal costs divided by positively verified applications.

## Attempt state and exactly-once behavior

Add an owner-migrated `apply_attempts` table:

- `attempt_id UUID PRIMARY KEY`
- `queue_name TEXT NOT NULL`
- `url TEXT NOT NULL`
- `dedup_key TEXT`
- `worker_id TEXT NOT NULL`
- `route TEXT NOT NULL`
- `route_version TEXT`
- `state TEXT NOT NULL`
- `submit_started_at TIMESTAMPTZ`
- `finalized_at TIMESTAMPTZ`
- `verification_method TEXT`
- `verification_ref TEXT`
- `evidence JSONB NOT NULL DEFAULT '{}'`
- `created_at TIMESTAMPTZ NOT NULL DEFAULT now()`

States:

- `prepared`: no irreversible action; safe to abandon or retry.
- `submit_started`: durable checkpoint written immediately before the one submit action.
- `submitted_unverified`: action returned but evidence is not yet conclusive.
- `verified`: positive evidence; write queue `applied` and `applied_set`.
- `contradicted`: positive evidence that submit did not occur; a later fresh attempt may be allowed.
- `quarantined`: submit may have occurred; write `crash_unconfirmed` and `applied_set`.
- `failed_pre_submit`: safe failure before submit; normal fallback or retry policy may apply.

A partial unique index prevents more than one unresolved `submit_started` or
`submitted_unverified` attempt per non-null `dedup_key`.

Optional route details remain in `apply_result_events.result_metadata` rather than adding many
nullable columns. Required keys for new attempts are:

- `attempt_id`, `route_version`, `adapter_name`, `adapter_version`
- `preflight_status`, `preflight_reason`
- `plan_ready`, `unmapped_required_count`, `action_count`
- `submit_checkpoint_state`
- `verification_method`, `verification_ref`
- `fallback_from`, `browser_provider`
- `model_cost_usd`, `browser_cost_usd`, `human_minutes`

## Data flow

1. Home pushes an approved, host-policy-eligible row.
2. Worker leases it and runs read-only liveness.
3. A dead row closes at zero model cost; all other rows reach the route planner.
4. Route planner selects deterministic adapter or fallback before Chrome-agent work.
5. Adapter discovers fields and produces a complete or incomplete plan.
6. Incomplete pre-submit plans fall through to the selected fallback.
7. Complete plans fill the form and create a durable `prepared` attempt.
8. Immediately before submit, the attempt becomes `submit_started` in Postgres.
9. Exactly one submit action occurs.
10. Verifier normalizes response, URL, DOM, and later email evidence.
11. Verified attempts close `applied`; unresolved attempts close `crash_unconfirmed` and enter
    `applied_set`; contradicted attempts may be retried under policy.
12. Route and cost metadata feed the cost-quality report and circuit breakers.

## Error handling

- Probe failure: continue as `uncertain`; never false-deny for transport or access errors.
- Browser launch/CDP/MCP failure before page action: worker-level backoff; safe requeue only when
  evidence proves the form was untouched.
- Adapter discovery or plan failure before submit: agent fallback is allowed.
- Required field unmapped: no submit; fallback or park.
- Submit response or navigation timeout after `submit_started`: quarantine; no fallback submit.
- Verifier contradiction: finalize `contradicted` with evidence; retry requires a fresh lease and
  policy decision.
- Repeated host failure: host-specific circuit breaker before more model spend.
- Repeated worker browser failure: worker halt/backoff before more leases.
- Email/OTP wait: park with an owner route and deadline; no busy browser-agent loop.

## Cost controls

- Preflight target: zero model cost and less than `$0.01` compute per check.
- Deterministic adapter target: less than `$0.10` marginal cash per attempt before any bounded
  answer call.
- Per-route time, tool-call, and dollar ceilings.
- One concurrent live adapter submit globally during initial canary.
- Per-host daily caps for adapter and fallback routes.
- Stop loss on any duplicate, false `applied`, wrong answer, secret exposure, or cross-job state.
- Pause a route if positive verification is below 85%, no-confirmation exceeds 10%, or its
  upper-confidence all-in cost is not below the current route.

## Testing

### Unit tests

- Fleet-v3 preflight closes only `dead` and never calls `apply_fn` for those rows.
- `live`, `uncertain`, blocked, server-error, and probe-exception outcomes still call `apply_fn`.
- Adapter plans refuse unmapped required fields and unsupported sensitive answers.
- Verifier evidence precedence and contradiction behavior are stable.
- Attempt transitions reject invalid state changes and second unresolved submits.
- Cost aggregation includes zero-cost preflight rows and all fallback costs.

### Integration tests

- Synthetic Greenhouse and Ashby forms cover standard fields, uploads, conditional questions,
  consent, custom controls, validation errors, success response, success URL, and confirmation DOM.
- A process failure after `submit_started` is quarantined and cannot be leased into another submit.
- Adapter fallback occurs before submit only.
- Home migration creates tables; least-privilege workers perform DML and compatibility checks only.

### Live canaries

- Shadow liveness and adapter discovery first.
- Greenhouse: at least 20 complete shadow inventories, then 5 one-concurrent live submits.
- Ashby follows only after Greenhouse has no duplicate or false-positive result.
- Compare route-tagged all-in cost, positive verification, fallback, and ambiguous outcomes against
  the existing Playwright-MCP control.

## Rollout

1. Merge Phase 1 and run the home/owner schema migration.
2. Restart remote workers only after their read-only compatibility check passes.
3. Add fleet-v3 preflight using the existing liveness module; shadow counters, then enforce strong
   dead results.
4. Add attempt state and independent verification without enabling adapter submit.
5. Run Greenhouse shadow inventory and verification fixtures.
6. Enable a five-submit Greenhouse canary with one concurrency.
7. Promote Greenhouse only after quality and cost gates pass.
8. Build and shadow Ashby with the same protocol.
9. Canary the low-cost fallback on pre-submit-only escapes.
10. Add authenticated-profile recovery after primary waste buckets fall.

## Acceptance criteria

- All 31 research records and the consolidated report remain reproducible and validated.
- Fleet-v3 runs liveness before any expensive apply function and records zero-cost dead outcomes.
- Workday cannot reach unattended general-agent execution without explicit tenant trust.
- Every adapter-owned submit has a durable pre-click checkpoint.
- No fallback can submit after an adapter may have submitted.
- `applied` requires positive verifier evidence; screenshots alone are insufficient.
- Greenhouse completes a route-tagged live canary with zero duplicate and false-positive outcomes.
- Ashby has a fail-closed shadow adapter and synthetic fixture coverage.
- Cost reporting shows route-level all-in cost and verified completion.
- First production target: all-in cost per verified apply below `$1.00`.
- Long-term target: approach `$0.50` through Ashby/Greenhouse deterministic coverage, not by hiding
  failed costs or weakening quality gates.

## Explicit non-goals

- Bypassing CAPTCHAs, login controls, rate limits, or site access restrictions.
- Automating the user's default browser profile.
- Treating browser identity or anti-detection behavior as permission.
- Marking applications successful from screenshots or agent assertions alone.
- Retrying any attempt after an unresolved irreversible submit action.
- Replacing Playwright solely to claim lower browser cost.
