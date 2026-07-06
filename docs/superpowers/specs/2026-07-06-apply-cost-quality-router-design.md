# Apply cost-quality router - design

**Approved:** 2026-07-06. Owner goal: highest-quality applications at the cheapest
quality-adjusted cost, not simply the cheapest browser or model.

## Problem

The current apply lane spends too much per verified successful application because every job is
treated too similarly. Stable ATS flows, low-yield hosts, auth-gated pages, and true edge cases all
flow toward the same expensive browser-agent path.

Live and local historical evidence gathered on 2026-07-06:

- Fleet Postgres `apply_queue`: 452 applied, 3,283 terminal attempts, $625.66 total recorded
  `est_cost_usd`, $1.3842 all-in cost per successful apply.
- Applied rows alone average $0.6793, which understates the real cost because failed, blocked, and
  crash-unconfirmed attempts are paid for.
- `llm_usage` for `task='apply_agent'`: Claude apply-agent rows average $0.8599, p90 $2.3609.
- Local SQLite brain: 82,527 jobs, 3,291 touched jobs, 527 applied jobs from 2026-06-23 through
  2026-07-05.
- Historical success rate by ATS bucket: Ashby 33.7%, Greenhouse 28.6%, Workday 1.0%, Lever 10.6%,
  other 8.3%.
- Historical non-applied failures: agent/browser runtime 44.7%, preflight/policy 40.8%, challenge
  related 2.7%, explicit email/auth/verification 1.2%.
- Fleet auth challenges are concentrated in `login_gate` and `manual_auth`, mostly Indeed,
  Hiring Cafe, LinkedIn, and scattered Workday tenants.

The target is to reduce all-in cost per successful, correctly submitted application without
lowering application quality. The first target is below $1.00 per successful apply; the second
target is near $0.50 by moving high-volume stable flows off the full browser agent.

## Design principles

1. Optimize for quality-adjusted cost per verified successful apply.
2. Use historical application outcomes as the primary benchmark.
3. Treat external provider prices only as planning assumptions for future routes.
4. Prefer deterministic ATS-specific logic where the form structure is known.
5. Use expensive agents only for cases where cheaper routes cannot confidently submit.
6. Fail closed: do not fabricate answers, do not blind-submit through captcha/auth walls, and do not
   mark a row applied without positive confirmation.

## Architecture

The apply path becomes a tiered router:

1. **Scoreboard and policy input**
   - Reads local SQLite history and fleet Postgres cost/challenge data.
   - Produces route metrics by host, ATS, worker, agent, model, failure bucket, and cost.
   - Feeds static defaults and later dynamic host policy.

2. **Pre-lease and pre-agent host policy**
   - Decides whether a job is eligible for unattended apply.
   - Allows stable high-yield hosts such as Ashby and Greenhouse.
   - Parks or supervises low-yield or auth-heavy hosts such as Workday tenants until they prove
     clean submits.
   - Prevents workers from spending model dollars on known low-success categories.

3. **Adapter-first execution**
   - For supported hosts, the deterministic adapter builds an answer plan and fills the form.
   - A verified cheap answerer handles free-text questions using resume/profile evidence and past
     approved answers.
   - A complete plan can own submission after canary validation.
   - An incomplete plan falls back to the browser agent.

4. **Agent fallback**
   - Premium Claude/Sonnet remains available for unfamiliar or high-risk forms.
   - Cheaper Codex/DeepSeek-style routes can be canaried, but they must beat the quality-adjusted
     cost baseline and preserve confirmation quality before scaling.

5. **Auth/profile recovery lane**
   - Login-gated jobs route to owner/home browser profiles rather than random workers.
   - Indeed, LinkedIn, and Hiring Cafe get trusted seeded profiles if canaries prove they increase
     success.
   - Workday remains per-tenant; there is no global Workday account strategy.
   - Email/OTP relay supports verification flows, but it is a second-order improvement after
     runtime failures and host policy.

## Component details

### A. Apply cost scoreboard

Add a repeatable reporting command or script that joins:

- Fleet Postgres: `apply_queue`, `llm_usage`, `auth_challenge`, `worker_heartbeat`,
  `fleet_desired_state`.
- Local SQLite: `jobs`, `applications`, `auth_challenges`, `email_events`, `inbox_events`,
  `llm_usage`.

Report sections:

- Cost per successful apply: applied-row cost and all-in terminal cost.
- Success rate by ATS and host.
- Failure bucket by count and cost.
- `no_result_line` and crash distribution by host, agent, model, worker, and tool-call class.
- Auth challenge backlog by host, kind, route, outcome.
- Route comparison once adapters are enabled: `agent`, `adapter_shadow`, `adapter_submit`,
  `auth_profile`, `supervised`.

This report is the acceptance gate for every optimization. A change is not considered successful
unless it improves quality-adjusted cost or reduces a known waste bucket without reducing verified
submits.

### B. Runtime failure classification

Current `failed:no_result_line` and crash-unconfirmed rows are too broad. Before changing retry
policy, the launcher and worker should persist enough evidence to separate root causes.

Add per-run fields or result-event metadata:

- `agent`, `model`, `worker_id`, `machine_owner`.
- `tool_calls_total`, `application_tool_calls`, `last_tool`, `last_url`.
- `chrome_launch_ok`, `cdp_connect_ok`, `mcp_started_ok`.
- `agent_exit_code`, `timeout_seconds`, `reader_timeout`.
- `result_source`: final agent result, transcript scan, timeout, process failure.
- `failure_class`: `usage_or_session_limit`, `agent_auth`, `mcp_start_failure`,
  `browser_launch_failure`, `cdp_lost`, `zero_tool_no_result`, `post_browser_no_result`,
  `post_form_crash_unconfirmed`, `malformed_result`, `timeout`.
- `transcript_tail` or log pointer for inspection.

Policy after classification:

- Zero application-touching tool calls plus quota/auth/agent-start failure requeues safely.
- Browser/CDP/MCP startup failures halt or back off the worker, not the job.
- Post-form crashes remain `crash_unconfirmed`.
- Repeated host-specific `no_result_line` becomes a host policy signal.

### C. Host policy gate

Introduce a host/ATS policy layer before unattended apply work is pushed or leased.

Initial policy:

- `allow`: Ashby, Greenhouse, Workable where history is healthy.
- `canary`: Lever, SmartRecruiters, high-volume other hosts with acceptable historical outcomes.
- `supervised`: Indeed, Hiring Cafe, LinkedIn, Workday tenants with account/login gates.
- `park`: Workday tenants with no clean submits, hosts with repeated auth gates, known low-yield
  hosts such as broad public job boards when they redirect poorly.
- `deny`: user-excluded companies, known blocked hosts, impossible geography, expired/dead
  postings.

The policy must be observable. Each skipped or parked job gets a durable reason such as
`host_policy:workday_supervised`, `host_policy:low_success`, or `host_policy:login_gate`.

Workday rule:

Workday is not globally enabled. Each tenant starts supervised or parked and moves to canary only
after clean submits on that tenant. This avoids treating `*.myworkdayjobs.com` as a single solved
provider when accounts and flows are tenant-specific.

### D. Adapter-first Greenhouse and Ashby

Greenhouse already has a guarded deterministic path:

- `apply.greenhouse_adapter` builds the answer plan from public questions.
- `apply.greenhouse_submit` can dry-run fill, submit only when separately enabled, and requires
  positive confirmation.
- `apply.answerer` provides verifier-gated cheap free-text answers.

Rollout:

1. Shadow Greenhouse: enable form discovery/fill dry-run, log plan readiness, do not submit.
2. Canary Greenhouse: submit only complete plans for a small batch, compare quality and cost.
3. Production Greenhouse: adapter owns ready forms, agent fallback owns incomplete plans.
4. Build Ashby adapter with the same gates because Ashby has the largest high-success volume.
5. Add adapter route metadata to result rows so cost and success can be compared directly.

Quality requirements:

- Never submit if required fields are unmapped.
- Never submit unverified free-text answers.
- Require positive confirmation text or a known success URL/state.
- Capture approved free-text answers back into the answer corpus.
- Preserve agent fallback for unsupported form variants.

### E. Auth/profile/email handling

Auth/profile work is useful, but it is not the largest current cost bucket. It should recover
parked opportunities after host policy and runtime classification reduce waste.

Initial accounts/profiles:

- Indeed: create and seed one trusted candidate profile if a canary shows success improves.
- LinkedIn: keep a separate seeded profile and one-account concurrency guard.
- Hiring Cafe: seed if login gating blocks useful redirects.
- Workday: create accounts only per high-value tenant; no global Workday account exists.

Email/OTP:

- Use a dedicated application inbox.
- Keep apply-time `inbox_events` distinct from outcome email `email_events`.
- OTP relay can resolve verification-code walls, but login gates requiring user decisions remain
  owner-supervised.

Routing:

- Friend or remote workers do not solve login walls themselves.
- Login-gated rows route to owner/home profile or stay parked with a clear reason.
- Once a tenant/profile has proven clean submits, it can move from supervised to trusted canary.

## Data model additions

Prefer append-only result metadata over rewriting historical rows.

Potential additions:

- `apply_result_events.route`: `agent`, `adapter_shadow`, `adapter_submit`, `auth_profile`,
  `supervised`.
- `apply_result_events.failure_class`.
- `apply_result_events.tool_calls_total`, `application_tool_calls`, `last_tool`.
- `apply_result_events.host_policy`.
- A host policy table in fleet Postgres, or a versioned config file mirrored into Postgres:
  `host`, `ats`, `mode`, `reason`, `success_threshold`, `daily_cap`, `updated_at`.
- Adapter metrics: `adapter_name`, `adapter_plan_ready`, `unmapped_required_count`,
  `free_text_count`, `confirmation_method`.

## Testing

Unit tests:

- Failure classifier maps known transcripts/log shapes to stable `failure_class` values.
- Zero-tool usage/session limits requeue; post-browser crashes do not.
- Host policy maps Workday tenants to supervised/parked by default.
- Ashby/Greenhouse adapter plans refuse unmapped required fields.
- Answerer rejects fabricated metrics, companies, placeholders, and banned content.

Integration tests:

- Fake ATS pages for adapter submit and confirmation detection.
- Fake login gate and email-code gate route to the auth lane instead of agent retries.
- Fleet queue push/lease excludes parked hosts and records host-policy reasons.
- Scoreboard fixture computes cost per successful apply and host success rate correctly.

Live canary:

- Start with a small Greenhouse adapter canary.
- Compare against current Greenhouse baseline: historical 28.6% success and fleet $1.208
  all-in cost per applied.
- Promote only if submit confirmation quality holds and cost per applied falls.
- Repeat for Ashby after Greenhouse instrumentation is stable.

## Rollout

1. Build the scoreboard so baseline and regressions are visible.
2. Add runtime failure classification and preserve current behavior except safe requeue/backoff for
   clearly pre-application failures.
3. Enable host policy in report-only mode.
4. Enforce Workday and low-yield host parking/supervision.
5. Enable Greenhouse adapter shadow mode.
6. Run Greenhouse canary submit.
7. Build and shadow Ashby adapter.
8. Add trusted auth/profile canaries for Indeed, LinkedIn, and Hiring Cafe.

## Acceptance criteria

- The scoreboard command reports local history plus fleet costs in one view.
- `no_result_line` and crash rows have a specific `failure_class` for new attempts.
- Workday unattended apply is blocked unless a tenant is explicitly trusted/canary.
- Greenhouse adapter can run shadow mode without changing submit behavior.
- Adapter-owned submits are route-tagged and positive-confirmation gated.
- Login-gated rows are parked or routed to owner/home auth profile with a visible reason.
- First production target: all-in cost per successful apply below $1.00 without reducing verified
  submit quality.
- Second target: move high-volume Ashby/Greenhouse traffic toward $0.50 all-in cost per successful
  apply or better.

