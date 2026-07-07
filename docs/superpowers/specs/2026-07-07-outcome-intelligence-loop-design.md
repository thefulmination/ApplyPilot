# Outcome Intelligence Loop — design

- **Date:** 2026-07-07
- **Status:** Approved in brainstorm; pending written-spec review
- **Owner:** Jonathan
- **Topic:** Upgrade ApplyPilot's email outcome monitor into a trustworthy operator loop for review, alerts, and learning

---

## 1. Motivation

ApplyPilot already scans email outcomes on a 6-hour cadence, stores them in
the home brain's `email_events`, mirrors them into fleet Postgres
`inbox_outcomes`, and uses trusted confirmation emails to reconcile
`crash_unconfirmed` applies. That baseline is now live and healthy.

The next problem is not "make it exist." The problem is that the loop still
under-serves four practical needs:

1. Important emails are not surfaced aggressively enough for operator action.
2. Misattributed or low-confidence outcomes are hard to correct cleanly.
3. The data is not yet organized into an operator-facing outcomes console.
4. Learning from outcomes is still weak because acknowledgements dominate and
   trusted/corrected evidence is not first-class.

This design turns the current scan into an outcome-intelligence loop with one
backbone and four operator-facing capabilities:

- evidence review and correction
- an outcomes console
- high-signal alerts
- trusted-only learning outputs

## 2. Verified current state

Ground truth from the live runtime on 2026-07-07:

- The authoritative runtime repo is
  `C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot`, not `New project 9`.
- The home brain at `C:\Users\JStal\.applypilot\applypilot.db` contains
  `email_events`, with fresh `scanned_at` rows on 2026-07-07.
- The scheduled task `ApplyPilotFleet-OutcomeScan` is present, last ran
  successfully on 2026-07-07, and is scheduled every 6 hours.
- The log `.fleet-logs\outcome-scan.log` shows a successful latest run with
  `reconcile-email exit=0`.
- The configured mail backend is `ImapMailSource`, backed by
  `gmail_app_password.json`, and a live fetch canary succeeds.
- Live counts at design time: `883` outcome rows, heavily dominated by
  `acknowledged`, with some `rejected`, sparse `screen`/`interview`, and no
  current `offer` rows.

These facts matter because they shift the work from transport/reliability into
review, presentation, and trusted analytics.

## 3. Goals

### Goals

- Make every important outcome email visible, actionable, and auditable.
- Let the operator correct bad matches and bad stage/outcome classification
  without manual database surgery.
- Separate raw evidence from trusted evidence so downstream consumers know what
  they may safely use.
- Add an operator-facing console for review, timelines, action queues, and
  analytics.
- Add high-signal alerting for interview/offer/screen style events.
- Produce trusted learning outputs that can inform scoring and discovery review.

### Non-goals

- No automatic rewriting of scoring logic directly from raw outcomes.
- No automatic pausing/blacklisting of employers or lanes from tiny samples.
- No SMS/push dependency in the first rollout.
- No migration of this feature into the TypeScript `New project 9` repo.
- No general email-client ambitions; this remains a job-outcomes subsystem.

## 4. Design choice

Three approaches were considered:

1. Backbone first: build trusted evidence and review first, then dashboard,
   alerts, and learning.
2. Dashboard first: expose current data in UI and fix what looks wrong.
3. Automation first: add alerts and lane recommendation logic immediately.

This design chooses **backbone first**.

Reasoning:

- Alerts are only useful if the evidence is trustworthy enough to avoid noise.
- Learning outputs are only useful if they can exclude untrusted evidence.
- The current system already has enough scan cadence and storage; it lacks
  review and downstream structure.

## 5. Architecture overview

The upgraded loop keeps `email_events` as the raw evidence source and adds a
review/correction layer above it.

```
mail source (IMAP/Gmail API fallback)
    -> scan + classify + match
    -> email_events (raw evidence)
    -> evidence trust policy
    -> email_event_reviews / derived trust state
    -> operator surfaces
       - inbox review
       - application timeline
       - action queue
       - analytics
    -> alerts
    -> trusted learning exports
```

The central rule is:

**Only trusted or explicitly corrected evidence may drive alerts, analytics,
or learning.**

## 6. Evidence model

### 6.1 Raw evidence

Keep `email_events` as the immutable-ish evidence layer written by the scanner.
It remains the source of:

- message metadata
- extracted stage/outcome/reason/title/company
- match method and match score
- scan timestamp
- existing `match_status` and `match_reason`

`email_events` is still the single writer target of the scan flow. The review
layer does not overwrite its semantic fields directly.

### 6.2 Review layer

Add a new table `email_event_reviews` with append-only correction records.
Suggested columns:

- `id`
- `message_id`
- `review_action`
- `reviewed_by`
- `reviewed_at`
- `corrected_job_url`
- `corrected_stage`
- `corrected_outcome`
- `corrected_confidence`
- `resolution`
- `note`

`review_action` should cover:

- `confirm`
- `reassign_job`
- `change_stage`
- `change_outcome`
- `ignore`
- `suppress_pattern`
- `mark_actioned`

`resolution` should support:

- `trusted`
- `needs_review`
- `ignored`
- `corrected`

### 6.3 Derived effective view

Add a pure assembler that resolves each email event into an effective state by
combining the raw event with its latest review record.

This effective state is what all downstream readers consume. It should expose:

- effective job match
- effective stage
- effective outcome
- effective trust state
- whether the event is unresolved
- whether the event still needs operator action

## 7. Trust policy

The system needs a deterministic trust policy so the operator does not have to
micromanage every row.

### 7.1 Auto-trusted cases

Auto-trust when:

- the match is exact and job-specific, such as `board_slug` or
  `linkedin_job_id`, and there is no temporal or ambiguity failure
- the event already passes the existing integrity guards and has no manual
  correction against it
- the stage is a straightforward acknowledgement from a precise ATS source and
  attribution is unambiguous

### 7.2 Auto-review cases

Route to review when:

- `match_status='needs_review'`
- the company/title attribution is ambiguous
- the classifier yields a potentially important event with low confidence
- the sender or subject shape repeatedly produces operator corrections
- the event is unmatched but appears to be recruiting-related

### 7.3 Always-surface cases

Even when trust is low, always surface events that look like:

- offer
- interview invite
- recruiter screen request
- assessment request or deadline
- a new reply on a previously silent application thread

These should appear in the action queue and may emit warning-level alerts.

## 8. Operator surfaces

The operator UI belongs in the Python runtime repo because that is where the
live state, scheduling, and fleet mirror already live.

Add an outcomes console with four working views.

### 8.1 Inbox review

Purpose: triage uncertain evidence.

Show:

- unmatched events
- `needs_review` events
- low-confidence but high-value events
- corrected events
- ignored or suppressed patterns when explicitly requested

Actions:

- confirm match
- reassign to another job
- change stage/outcome
- ignore false positive
- suppress recurring sender/pattern

### 8.2 Application timeline

Purpose: show one practical record per application.

Show:

- applied date
- latest trusted stage
- latest trusted outcome
- silence age
- most recent event time
- linked evidence timeline

Derived metrics:

- time to first response
- time to rejection
- time to screen/interview/offer
- currently silent vs active vs terminal

### 8.3 Action queue

Purpose: surface what requires owner attention right now.

Include:

- trusted interview/offer/screen events not yet actioned
- uncertain but high-value events
- auth-gated or challenge-related follow-up outcomes if linked
- previously silent applications that now have fresh recruiter activity

Actions:

- mark actioned
- open evidence
- confirm/reject suggested interpretation

### 8.4 Analytics

Purpose: give trustworthy descriptive feedback, not black-box automation.

Show:

- response rates
- stage progression counts
- time-based conversion metrics
- silent-job worklists
- lane slices by coarse dimensions

The UI should clearly separate trusted/corrected evidence from excluded
untrusted evidence.

## 9. Alerts

Alerts must stay high signal. Acknowledgements should not page the operator by
default.

### 9.1 Alert classes

- `critical`: offer, interview invite, recruiter screen, assessment deadline,
  or meaningful recruiter reply on a silent thread
- `warning`: uncertain but possibly important events that still need review
- `digest`: periodic summary of acknowledgements, rejections, and unresolved
  review items

### 9.2 Delivery order

Use the cheapest already-available delivery surfaces first:

1. console banner / local status surface
2. email digest
3. phone-facing summary artifact

Push or SMS can be added later if the first three prove insufficient.

### 9.3 Alert gating

- trusted high-value events alert immediately
- `needs_review` events may alert only when the stage is potentially important
- repeated updates from the same thread collapse into one active unresolved alert
- acknowledgements and routine rejections roll into digest mode by default

## 10. Learning loop

Only trusted or corrected outcomes feed learning.

### 10.1 Inputs

Use effective outcome states joined against existing job metadata:

- `source_board`
- normalized title family
- seniority
- fit-score band
- company class / ATS family
- location bucket
- salary band where available

### 10.2 Metrics

Track separately:

- `acknowledged`
- `screen`
- `interview`
- `offer`
- `rejected`
- `silent`

Also derive:

- time to first response
- time to terminal decision
- fast rejection
- long silence
- positive-response rate

Acknowledgement rate alone must not be treated as success.

### 10.3 Outputs

Learning should first produce recommendation reports and exports, not direct
policy changes.

Suggested outputs:

- lane response-rate report
- outcome-latency report
- trusted outcomes export for offline analysis
- score-band vs outcome comparison

### 10.4 Deferred automation

Do not let this rollout:

- auto-rewrite scoring logic
- auto-pause lanes
- auto-blacklist employers
- auto-promote changes into discovery/apply policy without explicit review

## 11. CLI and module shape

Keep the new work additive and close to the existing outcomes code.

Suggested modules:

- `src/applypilot/outcome_review.py`
- `src/applypilot/outcome_effective.py`
- `src/applypilot/outcome_alerts.py`
- `src/applypilot/outcome_operator.py`

Suggested CLI entry points:

- `applypilot outcomes-review queue`
- `applypilot outcomes-review resolve`
- `applypilot outcomes-alerts digest`
- `applypilot outcomes-operator --port N`
- `applypilot outcomes-learn export`

The exact command names can be adjusted during planning, but the split should
preserve three boundaries:

- evidence review
- operator surface
- analytics/export

## 12. Error handling and safety

- Review records are additive and reversible; correction must not require manual
  DB edits.
- A malformed or partial event stays visible in review rather than being
  dropped.
- Alerts must degrade safely: if delivery fails, the event still lands in the
  action queue.
- Analytics must be explicit about exclusions: untrusted rows are counted
  separately, never mixed silently into trusted metrics.
- The scan writer remains single-writer over `email_events`; review and console
  code should avoid taking ownership of scan-time writes.

## 13. Rollout plan

### Phase 1: trust backbone

- add `email_event_reviews`
- add effective-state resolver
- add trust-policy rules
- add CLI/report for unresolved review items
- make exports explicit about trusted vs excluded evidence

### Phase 2: operator console and alerts

- add review UI
- add application timeline and action queue
- add critical alerts and digest generation

### Phase 3: learning outputs

- add trusted-only analytics assemblers
- add lane and score-band reports
- add exports for downstream scoring/discovery review

### Phase 4: optional policy hooks

- wire recommendation outputs into explicit human-reviewed scoring/discovery
  decisions

This ordering is intentional: fix trust first, then make the system louder,
then use it to learn.

## 14. Testing

Tests should scale with the risk of downstream automation.

- Unit tests for effective-state resolution from raw + review rows
- Unit tests for trust policy routing
- Unit tests for alert gating and dedup/collapse behavior
- Integration tests for review actions changing effective match/stage/outcome
- Integration tests for analytics excluding untrusted rows
- Operator-surface smoke tests for queue/timeline assembly
- Export tests proving trusted-only learning outputs are deterministic

The most important invariant is:

**A corrected or ignored event must stop affecting alerts and analytics
immediately, without changing the underlying raw evidence row.**

## 15. Success criteria

- Important outcome emails are surfaced within one scan cycle.
- False-positive high-priority alerts stay rare.
- Every bad attribution or bad classification can be corrected through the
  review layer.
- Trusted analytics exclude noisy evidence cleanly.
- Learning outputs contain meaningful stage progression, not just
  acknowledgement counts.
- The operator no longer needs DB surgery or log spelunking to understand what
  happened.

## 16. Open decisions

- Whether the first operator surface should extend the existing fleet console or
  run as a dedicated outcomes console endpoint.
- Whether suppression should be sender-domain based, thread based, or use a
  small pattern registry.
- Whether daily digest should be email-only at first or also write a dedicated
  phone-facing artifact.

These are planning-level decisions, not blockers for the architecture.
