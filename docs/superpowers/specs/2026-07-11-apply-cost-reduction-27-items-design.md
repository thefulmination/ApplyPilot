# Apply Cost Reduction: 27-Item Design

**Date:** 2026-07-11

## Objective

Implement the complete 27-item cost-reduction program in strict dependency order. The final system must reduce paid model use without weakening submission correctness, duplicate protection, eligibility truthfulness, authentication safety, or confirmation evidence.

## Delivery Rule

Items are implemented and verified one at a time. An item is complete only when its focused tests pass and its acceptance evidence is recorded. Later items may depend on earlier interfaces, but no item may be marked complete from documentation or indirect evidence alone.

The existing dirty worktree is authoritative. Changes must preserve unrelated user work and avoid broad refactors.

## Safety Invariants

1. Only a strict terminal result parser may convert agent output into a status.
2. `applied` requires positive confirmation from a completion URL, DOM marker, acknowledgement email, or an adapter-specific equivalent.
3. A submit click without confirmation is terminal `no_confirmation` and is never automatically retried.
4. A zero application-tool run is provably pre-submit; a run with application-touching tools is not automatically retryable.
5. Liveness, location, duplicate, host policy, browser readiness, and authentication gates run before paid inference.
6. Models never own retry safety, eligibility termination, or confirmation truth.
7. Credentials are not stored in Postgres, logs, prompts, or the tenant registry.
8. Local inference remains disabled by default and cannot become authoritative without a shadow canary.
9. Every live rollout is bounded by tenant, count, spend, and confirmation gates.

## Architecture

The application path becomes a layered state machine:

1. **Preflight:** resolve target ATS, verify liveness, eligibility, duplication, tenant policy, and browser health.
2. **Session:** select a persistent tenant browser profile or park for supervised authentication.
3. **Adapter:** execute deterministic ATS steps and field mappings.
4. **Answer:** retrieve an approved answer, call local Qwen when needed, verify deterministically, then allow one paid fallback.
5. **Submit:** review, click once, and collect positive confirmation evidence.
6. **Outcome:** write a strict terminal status plus phase, action, cost, and evidence metadata.
7. **Exception:** park any unsupported or ambiguous state with enough context for human resolution.

## Data Model

The design extends existing additive metadata rather than replacing queue contracts.

- `apply_result_events.result_metadata` stores structured phase, current step, submit state, confirmation evidence, and per-phase cost.
- Existing columns `last_tool`, `application_tool_calls`, `route`, `failure_class`, `host_policy`, and `final_result_source` remain canonical indexed/reporting fields.
- `ats_tenants` remains the policy registry: `excluded`, `supervised`, or `trusted`, with bounded daily caps and halt state.
- Browser credentials and cookies stay in host-local encrypted browser profiles. The database stores only profile identifiers and readiness state.
- Approved answer retrieval continues through the existing answer corpus; generated text is accepted only after `verify_answer` passes.

## Ordered Implementation

### Phase A: Correct Outcomes and Evidence

1. **Strict terminal-result parsing.** Parse the last standalone terminal line, reject narrative mentions, and resolve conflicting lines conservatively.
2. **Historical outcome re-audit.** Recompute suspect applied records from linked logs and inbox evidence; produce a dry-run correction report before any mutation.
3. **Execution evidence.** Persist last action, Workday step, submit-click state, confirmation evidence, and per-phase costs.

### Phase B: Prevent Doomed Agent Launches

4. **Fresh liveness gate.** Require just-in-time ATS liveness before leasing, using Workday CXS where parseable.
5. **Pre-agent eligibility gate.** Resolve location and deterministic eligibility rules before browser/model launch.
6. **Browser readiness preflight.** Verify Chrome, CDP, and Playwright MCP before starting an agent.
7. **Zero-tool safe requeue.** Requeue missing-result runs only when recorded application-tool count is zero.
8. **Infrastructure loop breaker.** Classify pre-page initialization failures, restart the affected component once, and prevent model retry loops.

### Phase C: Workday Session and Authentication

9. **Tenant session manager.** Select a tenant-specific persistent browser profile and expose explicit ready/supervised/expired states.
10. **Email verification and OTP integration.** Use the existing inbox relay for supported codes and links; park unsupported authentication.

### Phase D: Deterministic Workday Adapter

11. **Workday state machine.** Model login, resume, personal information, experience, questions, disclosures, self-ID, review, submit, and confirmation as explicit states.
12. **Deterministic field mappings.** Map profile-backed factual fields without an LLM.
13. **Resume correction plan.** Apply stable work-history, education, URL, title, company, and date corrections after Workday parsing.
14. **Controlled dropdown handling.** Select nested Workday options by accessible label/value with read-back verification.
15. **Validation-loop protection.** Identify invalid fields from DOM state, permit one targeted repair, then park.
16. **Submission confirmation.** Combine URL, DOM, and inbox evidence; never infer success from button click or navigation alone.

### Phase E: Reduce Successful-Path Inference Cost

17. **Qwen3 8B local answer provider.** Add a disabled provider with timeout, structured result metadata, verifier enforcement, and paid fallback.
18. **Approved-answer cache.** Reuse semantically compatible verified answers while preserving job-specific grounding.
19. **Per-phase budgets.** Enforce separate turn/time/dollar budgets for preflight, authentication, form fill, answer, recovery, and confirmation.
20. **Tenant-aware router.** Route trusted tenants to deterministic execution, supervised tenants to bounded review, and excluded tenants to exceptions.

### Phase F: Other ATS Paths

21. **Greenhouse confirmation repair.** Improve positive confirmation and prevent adapter-owned ambiguous outcomes.
22. **Ashby deterministic adapter.** Add field discovery, profile mapping, verified answers, submit, and confirmation.
23. **Lever bounded path.** Keep deterministic mapping, add explicit CAPTCHA boundaries, and prevent unbounded fallback.
24. **Unsupported-host framework.** Apply common preflight, budgeting, evidence, and exception rules to every remaining host.

### Phase G: Controlled Rollout

25. **Ten-job Workday shadow.** Execute through review without submission and compare state/action/cost against the current path.
26. **Five-job supervised Workday canary.** Submit only on previously validated tenants with human observation and positive confirmation.
27. **Evidence-gated expansion.** Expand counts/tenants only when false-success, duplicate, unsupported-claim, exception, and all-in cost thresholds pass.

## Error Handling

- Parser ambiguity becomes `crash_review`, never `applied` or retryable.
- Dead or ineligible jobs terminate before browser launch with deterministic reasons.
- Browser preflight failure restarts the browser/MCP boundary once without invoking a model; repeated failure parks infrastructure.
- Authentication uncertainty parks the tenant and halts that tenant for the day.
- Missing local answer, verifier failure, or timeout triggers at most one paid answer fallback.
- Adapter validation failures capture the current state and park after one deterministic repair.
- Submission ambiguity writes `no_confirmation`, records `submit_clicked=true`, and blocks automatic retry.

## Testing Strategy

- Unit tests cover parser grammar, preflight policies, state transitions, field maps, verification, budgets, and routing.
- Postgres integration tests prove atomic lease gates, metadata persistence, exception routing, and duplicate protection.
- SQLite tests prove tenant/account policy and dry-run historical re-audit behavior.
- Browser contract tests use synthetic Workday DOM fixtures for each state and validation failure.
- Live tests are limited to the explicit shadow and supervised canary phases.
- Historical replay is rerun after items 1, 4, 7, 11, 16, 19, 21, 22, and 24 to measure attributable displacement and quality.

## Acceptance Gates

- Items 1-3: no known false applied classification and complete evidence for new events.
- Items 4-8: no paid call for dead, deterministic-ineligible, browser-unready, or zero-tool initialization failure cases.
- Items 9-10: tenant session readiness is explicit, secrets stay out of databases/logs, and unsupported auth parks safely.
- Items 11-16: synthetic Workday fixtures complete deterministically with zero model-owned navigation or confirmation decisions.
- Items 17-20: accepted local answers have zero verifier findings; fallback is bounded to one paid call.
- Items 21-24: every ATS route uses common safety and cost contracts.
- Items 25-27: zero false success, zero duplicate submission, zero unsupported accepted claims, positive confirmation for every recorded apply, and lower all-in cost than the historical comparator.

## Rollback

Every new route and provider is disabled by default. Rollback consists of disabling the specific route/provider flag and returning the tenant or ATS to the exception queue; schema changes are additive and do not require destructive rollback.

## Completion Evidence

The program is complete only when all 27 item-specific tests and acceptance gates pass, the final historical report is regenerated from current ledgers, the live canaries satisfy item 27, and no required route remains enabled solely by assumption or documentation.
