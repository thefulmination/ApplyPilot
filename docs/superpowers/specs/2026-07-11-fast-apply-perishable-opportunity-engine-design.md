# Fast Apply Perishable Opportunity Engine — Design

**Approved direction:** 2026-07-11

**Canonical authority:** `%LOCALAPPDATA%\ApplyPilot\applypilot.db`

**Primary objective:** minimize elapsed time from first discovery to confirmed submission while preserving minimum qualification, duplicate-prevention, and submission-integrity constraints.

## 1. Problem

ApplyPilot currently treats a job as a durable queue item even though the opportunity decays while discovery, scoring, URL resolution, approval, tailoring, and worker scheduling proceed. A high-scoring job that cannot be submitted promptly has little execution value. Conversely, a strong job that is open now but likely to close soon should move ahead of an equally strong, stable job.

Availability checks are necessary, but availability is not the objective. The objective is a timely confirmed application. Availability evidence exists to prevent the submission clock from being spent on a dead endpoint.

The current system already contains most required signals, but they are split across:

- Python SQLite job and liveness state;
- Postgres discovery observations and execution queues;
- TypeScript availability reports and ATS adapters;
- LinkedIn, Indeed, and company-site resolvers;
- fit, qualification, preference, and research scores;
- apply-result, cost, duration, and recruiter-outcome ledgers.

This design creates a speed-first control plane over those components. It does not introduce another independent availability scanner or another competing scorer.

## 2. Owner Decisions

1. **Speed is the primary execution objective.** High-priority jobs target confirmed submission within 30 minutes of discovery.
2. **Qualification and safety remain hard constraints.** Speed never bypasses duplicate prevention, company exclusions, minimum qualification, legal/profile truthfulness, or positive submit confirmation.
3. **Customization is subordinate to the application window.** When tailoring or cover-letter generation threatens the submission SLA, ApplyPilot uses the approved base resume and reusable answers.
4. **Human intent is durable; permission to spend is perishable.** Approval survives evidence expiry, but browser or paid-agent execution requires a fresh execution certificate.
5. **The SQLite brain remains canonical.** TypeScript research and availability tools write evidence to the brain; they do not independently authorize production queue insertion.
6. **A stale certificate means `needs_refresh`, not `dead`.** Only high-confidence terminal evidence marks a posting closed.
7. **The first implementation is rule-based and auditable.** Learned survival and response-yield models remain shadow-only until temporal calibration is trustworthy.

## 3. Success Criteria

For newly discovered jobs that satisfy standing authorization and minimum qualification:

- discovery to minimum qualification: p50 under 2 minutes, p90 under 5 minutes;
- discovery to execution-ready: p50 under 5 minutes, p90 under 10 minutes;
- execution-ready to browser start: p50 under 5 minutes;
- discovery to confirmed submission: target under 30 minutes when the destination is reachable and worker capacity is available;
- zero paid-agent launches for jobs carrying fresh high-confidence dead evidence;
- no reduction in duplicate-prevention or confirmed-submit integrity;
- every SLA miss attributed to one measured stage or blocking reason.

The 30-minute target is an operating target, not a promise that every employer form can complete within 30 minutes. Auth challenges, employer outages, required assessments, and unavailable worker capacity remain explicit exceptions.

## 4. Core Model

### 4.1 Durable approval

Approval answers:

> Is ApplyPilot authorized to apply to this role if it remains qualified and executable?

Existing batch approval remains valid unless revoked, the job incarnation changes materially, or a standing policy changes. Approval alone cannot be leased by an application worker.

### 4.2 Perishable execution certificate

An execution certificate answers:

> May ApplyPilot spend on this exact posting incarnation now, and through which cost stage?

Each certificate contains:

- canonical job URL and posting incarnation ID;
- evidence observation IDs;
- issue and expiry timestamps;
- current availability verdict and evidence strength;
- policy version;
- maximum authorized spend stage;
- expected submission minutes;
- reason for issuance.

Certificate expiry returns the job to `needs_refresh`. It does not remove approval or erase ranking information.

### 4.3 Posting incarnation

A posting incarnation is identified from the strongest available combination of:

- ATS board/site token and stable posting ID;
- employer requisition ID;
- resolved application URL;
- normalized company, title, and location;
- content fingerprint.

A posting that reappears after confirmed closure with a changed stable ID or material fingerprint becomes a new incarnation. It must be requalified and receives a new certificate.

### 4.4 Standing authorization envelope

Standing authorization is a versioned owner policy, not an inferred model decision. It defines the combinations of lane, minimum authoritative score, location eligibility, company exclusions, application channel, and document fallback that may enter the fast lane without a new per-job click.

The envelope is disabled until explicitly activated by the owner. A job outside it continues through the existing manual or batch approval path. Every certificate records whether authority came from a specific approval batch or a named standing-policy version.

## 5. Speed-First Pipeline

The pipeline becomes a concurrent directed graph rather than a long serial chain.

```text
discovered
  ├─> identity + duplicate check
  ├─> URL resolution
  ├─> availability evidence
  └─> minimum viable qualification
             |
             v
      standing authorization
             |
             v
    execution certificate issued
             |
             v
      just-in-time fast queue
             |
             v
  final pre-Chrome evidence check
             |
             v
      browser / paid agent
             |
             v
     confirmed submission
```

Deep diagnosis, research scoring, bespoke tailoring, and cover-letter generation may run concurrently, but they cannot hold the fast lane beyond its time budget.

### 5.1 Minimum viable qualification

The fast lane requires only the deterministic and already-authoritative evidence needed to enforce:

- role lane and company exclusions;
- minimum effective score;
- location and eligibility constraints;
- duplicate and prior-application checks;
- minimum description quality;
- standing owner authorization.

Research scores remain advisory unless already promoted through the existing owner-controlled path.

### 5.2 Document decision deadline

For each job, the scheduler calculates a document deadline:

```text
document_deadline = target_submission_at
                    - expected_browser_minutes
                    - safety_margin
```

If a validated tailored resume is available by the deadline, use it. Otherwise use the approved base resume. A cover letter is generated only when required by the form or when it can complete inside the same deadline.

### 5.3 Small execution buffer

The fleet maintains only enough certified work for approximately one to two worker-hours. Jobs outside that buffer remain ranked in the brain rather than aging in a large approved queue.

The buffer replenisher selects the next jobs using current evidence and current worker capacity. Replenishment runs continuously; it is not a large periodic batch.

## 6. Scheduling Policy

### 6.1 Submission ETA

Each candidate receives an estimated submission time:

```text
ETA = expected queue wait
    + unresolved URL work
    + remaining document work
    + host throttle wait
    + expected browser/application duration
```

ETA estimates use existing apply duration, host, resolver, challenge, worker, and agent telemetry. Cold-start estimates use conservative host-class defaults.

### 6.2 Decay-adjusted priority

The initial auditable priority uses transparent components:

```text
opportunity_value_now = qualification_value
                      × availability_probability_now
                      × response_value_proxy

decay_loss = opportunity_value_now
           - opportunity_value_at_predicted_submission

priority = (opportunity_value_now + urgency_weight × decay_loss)
         / max(expected_worker_minutes, 1)
```

For version 1:

- `qualification_value` is derived from the authoritative score tier plus standing preference policy;
- `availability_probability` comes from a deterministic evidence ladder;
- `response_value_proxy` defaults to `1.0`; a bounded source/host prior may be shadow-reported only after a trusted outcome cohort meets its sample floor, and it cannot override qualification gates;
- `urgency_weight` is configured and shadow-audited before it affects leasing.

A high-value job likely to close during the next execution interval receives an urgency premium. Age alone never produces an automatic rejection.

## 7. Evidence Ladder

Evidence is interpreted by source strength and recency.

### 7.1 Strong live evidence

- membership in a complete, recent authoritative ATS board snapshot;
- recent successful one-job ATS API response;
- recent logged-in LinkedIn resolver result confirming Easy Apply or a resolved external target;
- successful final application-target preflight with an active form signal.

### 7.2 Weak live evidence

- recent repeat sighting in an aggregator search;
- generic HTTP 200 with job-content signals;
- recent discovery without a true posting timestamp.

Weak live evidence receives a short certificate. Absence from a ranked or truncated aggregator result is never dead evidence.

### 7.3 Strong dead evidence

- authoritative ATS snapshot absence when coverage is proven complete;
- ATS job endpoint 404 or 410 with board identity confirmed;
- expired `validThrough` from current structured data;
- explicit closed/expired employer text;
- logged-in resolver confirmation that the posting is unavailable;
- apply-time `RESULT:EXPIRED`;
- trusted `position_filled` outcome mapped to the exact posting.

### 7.4 Uncertain evidence

- 401, 403, 429, 5xx, CAPTCHA, auth wall, SPA shell, or navigation timeout;
- incomplete or truncated board snapshot absence;
- conflicting URL or posting identity;
- old live evidence without a recent observation.

Uncertain jobs are refreshed only when their expected value justifies the check. They are not automatically marked dead.

### 7.5 Initial certificate lifetimes

Version 1 uses conservative, configurable defaults:

| Evidence | Initial certificate lifetime |
|---|---:|
| Complete authoritative ATS snapshot membership | 24 hours or until the next required snapshot, whichever is earlier |
| Successful one-job ATS API response | 12 hours |
| Logged-in LinkedIn Easy Apply or exact external-target confirmation | 6 hours |
| Recent repeat aggregator sighting | 2 hours |
| Generic HTTP/DOM active-page evidence | 30 minutes |
| Uncertain evidence | no execution certificate |

Strong dead evidence closes the current posting incarnation until a later observation proves reappearance or identity correction. These defaults are policy constants, not learned values, and can be shortened during canary rollout.

## 8. Append-Only Evidence Ledger

The canonical brain gains `job_availability_observations`:

```text
id
job_url
posting_incarnation_id
observed_at
observer
verdict                 live | dead | uncertain
reason
evidence_strength       strong | weak
source_board
target_host
original_url
final_url
board_snapshot_id
coverage_complete
http_status
posted_at_observed
valid_through_observed
latency_ms
estimated_cost_usd
policy_version
selection_probability
raw_evidence_json
```

The existing mutable job columns remain as a compatibility projection, but their semantics become explicit:

- `liveness_checked_at`: most recent completed check;
- `last_confirmed_live_at`: most recent strong or policy-accepted live observation;
- `last_confirmed_dead_at`: most recent strong dead observation;
- `liveness_status`: current projection;
- `liveness_reason`: reason for the current projection;
- `liveness_valid_until`: projection expiry.

The misleading legacy behavior in which `last_verified_live` can be stamped for dead or uncertain observations must not be used as training truth. It is migrated into compatibility-only status and replaced by the explicit timestamps above.

## 9. Execution Queue Contract

Postgres execution queues carry a projection sufficient for an atomic lease decision:

```text
posting_incarnation_id
approval_decided_at
availability_verdict
availability_evidence_strength
availability_checked_at
availability_valid_until
execution_certificate_id
execution_certificate_expires_at
expected_submission_minutes
priority_score
routing_policy_version
```

Lease requires:

- durable approval or matching standing authorization;
- certificate for the current posting incarnation;
- certificate not expired;
- current score at or above the configured threshold;
- all existing duplicate, governor, company, lane, and spend gates.

Expired certificates are atomically moved to `needs_refresh`; they are not leased and do not consume an application attempt.

## 10. Just-in-Time Verification

The final verification occurs immediately before Chrome or a paid agent starts.

- A still-valid strong certificate proceeds without another redundant page check.
- A weak certificate older than its host policy permits receives the cheapest applicable refresh.
- A strong dead result parks the job and records the saved launch.
- An uncertain result may proceed only when policy explicitly permits it and expected value exceeds the cost threshold.
- Targeted fleet runs must not bypass this step merely because a `target_url` was supplied.

Verification must occur before Chrome startup so a skipped job does not consume browser initialization or paid-agent tokens.

## 11. Bulk Market-Tape Integration

This is the next subproject after the fast-apply control plane, but the interfaces are defined now.

- `discovered_postings` recurrence becomes positive `last_seen` evidence instead of being discarded on an existing SQLite URL.
- Corporate ATS discovery emits lean, complete board snapshots separately from expensive content enrichment.
- Complete Greenhouse, Lever, Ashby, SmartRecruiters, and Workable snapshots reconcile all known posting IDs for that board in one operation.
- Truncated or filtered snapshots provide presence evidence only.
- Conditional HTTP requests are feature-detected to reduce bytes; unconditional refresh remains the fallback.
- LinkedIn and Indeed source rows inherit time-bounded evidence from exact, high-confidence resolved ATS targets.

## 12. Cross-Repository Ownership

### Python runtime and brain repository

Owns:

- canonical observation and certificate schema;
- availability projection;
- scheduling and execution-buffer replenishment;
- fleet sync and lease enforcement;
- resolver integration;
- pre-Chrome verification;
- SLA and cost telemetry.

### TypeScript review and scoring repository

Owns:

- existing deterministic availability adapters and reports;
- score and qualification explanations;
- review/operator presentation;
- writing normalized observations to the canonical brain through the approved brain seam.

TypeScript tools may not create approved production queue rows directly unless an explicit manual-override command records the override actor, reason, expiration, and policy version.

## 13. Operator Explanation

Every job receives a concise execution explanation:

```text
Discovered: 4 minutes ago from Indeed
Original posting date: 1 day ago
Last positive evidence: seen in discovery 4 minutes ago (weak)
Resolved destination: Greenhouse board acme / posting 123
Authoritative board evidence: live 2 minutes ago (strong)
Qualification: 8.4, authorized fast lane
Expected submission: 14 minutes
Certificate expires: 22 minutes
Document route: base resume; tailoring would miss deadline
Priority reason: high qualification and high near-term decay risk
```

The operator surface shows both the current status and the evidence trail. It does not hide uncertainty behind a scalar score.

## 14. Failure Handling

- **Resolver timeout or auth wall:** keep approval, expire the certificate, route to `needs_refresh`, and record the blocking stage.
- **Worker shortage:** recompute ETA and priority; do not pretend the job remains inside its SLA.
- **Tailoring timeout:** use the approved base resume when the document deadline passes.
- **Transient ATS/API failure:** retain the prior observation until its certificate expires; never convert a transient failure to dead.
- **Incomplete board snapshot:** accept positive membership only; never infer closure from absence.
- **Reappearance after closure:** create a new incarnation and requalify.
- **Submission ambiguity:** preserve existing `no_confirmation` and crash safeguards; speed never authorizes a second submit.
- **Clock or timestamp parse failure:** mark time evidence uncertain and require a fresh check before paid execution.
- **Queue/brain disagreement:** fail closed for paid execution, reconcile projections, and preserve the durable owner approval.

## 15. Metrics

### Primary latency metrics

- discovery to minimum qualification;
- discovery to resolved application URL;
- discovery to certificate;
- certificate to lease;
- lease to browser start;
- browser start to confirmed submission;
- discovery to confirmed submission, p50/p90/p95.

### Waste metrics

- paid dead-job launches;
- tokens, dollars, and browser-minutes spent on expired jobs;
- jobs expired before first attempt;
- certificate refresh cost per intercepted dead job;
- redundant checks avoided by valid strong certificates.

### Opportunity guardrails

- qualified jobs missing the 30-minute SLA by reason;
- high-value jobs parked by uncertain evidence;
- sampled false-dead rate;
- confirmed applications per hour;
- recruiter-response and positive-outcome rates by speed, source, and score band;
- base-resume versus tailored-resume outcome comparison.

## 16. Testing

### Unit tests

- evidence-strength classification and certificate TTL policy;
- posting-incarnation identity and resurrection;
- ETA and decay-priority calculation;
- document deadline and base-resume fallback;
- projection from append-only observations;
- transient failures never classified dead;
- complete versus incomplete snapshot absence behavior.

### Integration tests

- discovery, qualification, resolution, and availability run concurrently;
- a newly qualified job reaches the execution buffer without waiting for deep scoring;
- expired certificate cannot lease and moves to `needs_refresh`;
- refreshed certificate can lease without losing durable approval;
- strong dead preflight occurs before Chrome startup;
- targeted URL fleet runs cannot bypass preflight;
- TypeScript observation import updates the brain but cannot bypass queue authorization;
- base-resume fallback fires when tailoring misses its deadline;
- duplicate and no-confirmation safeguards remain intact.

### Temporal backtests

- replay historical jobs in observation-time order;
- group by posting incarnation to prevent repeated-sighting leakage;
- compare current score ordering with decay-adjusted ordering;
- measure avoided dead launches, SLA attainment, and sampled opportunity loss;
- retain a small randomized sentinel sample across source and age bands to measure calibration without policy-selection bias.

## 17. Rollout

1. **Instrumentation shadow:** add stage timestamps, observation ledger, ETA, certificate projection, and priority computation without changing leases.
2. **Pre-Chrome enforcement:** ensure every fleet path checks strong dead evidence before browser or paid-agent startup.
3. **Certificate canary:** require unexpired certificates for a bounded lane while preserving existing approval.
4. **Fast document route:** enable deadline-based base-resume fallback for a small authorized cohort.
5. **Small execution buffer:** replace large batch staging for the canary lane with continuous replenishment.
6. **Decay ordering canary:** compare latency, dead-launch waste, and confirmed submissions against current score-only ordering.
7. **Bulk market tape:** add complete ATS snapshots and recurrence ingestion under a separate implementation spec.
8. **Learned models:** shadow source/ATS survival and response-yield models only after observation history and sentinel sampling are adequate.

Each rollout step is independently reversible. No step removes historical jobs or their training evidence.

## 18. Explicit Non-Goals for the First Implementation

- no autonomous change to qualification thresholds;
- no learned model controlling production leases;
- no deletion of dead or stale jobs;
- no assumption that HTTP 200 proves availability;
- no inference of death from absence in ranked aggregator search results;
- no broad refactor of scoring or outcome subsystems;
- no replacement of existing LinkedIn, Indeed, company, or ATS resolvers;
- no direct TypeScript production authorization path.

## 19. Implementation Boundary

The first implementation plan covers Sections 4–10 and the instrumentation/operator portions of Sections 13–17: durable approval plus perishable certificates, stage clocks, ETA and decay priority, the small fast lane, document deadline fallback, and pre-Chrome enforcement.

Bulk ATS market-tape capture and learned survival/yield modeling are follow-on subprojects with separate specs and plans. Their interfaces are defined here so the first implementation does not create another dead end.
