# Design Spec: Canonical Decision Authority V2

**Status:** Owner approved the architecture in chat on 2026-07-10; written-spec review required before implementation.
**Date:** 2026-07-10
**Scope:** Replace ApplyPilot's title-audit-first apply authority with one versioned, evidence-backed decision produced from the SQLite brain, FitMap and knowledge-graph evidence, pointwise and pairwise preferences, and reviewed application outcomes.

## 1. Problem statement

ApplyPilot currently has several scoring systems but no single durable decision authority. The fleet push path prefers `audit_score`, then `fit_score`, and uses `research_fit_score` only when both are absent. The audit layer can substantially raise scores from title matches, while the richer TypeScript FitMap, label-learning, fit-philosophy, and pairwise paths are optional or advisory. Queue rows retain a numeric score but not the decision record, policy version, evidence, uncertainty, or source that authorized the application.

This design supersedes guiding principle 1 and the advisory-only rollout in `2026-06-25-unified-brain-pipeline-design.md`. The SQLite brain remains the source of truth, but a promoted canonical decision becomes the only source permitted to authorize a new application.

## 2. Owner-approved decisions

1. Pause the ATS lane during the rebuild. LinkedIn and compute remain independently controlled. The live ATS pause source is `operator_canonical_v2_rebuild`.
2. Keep broad discovery. Discovery may use titles and search terms to find candidates, but discovery signals cannot raise qualification.
3. Separate qualification, preference, and outcome estimates. A desirable title cannot compensate for a missing must-have qualification.
4. Use one versioned canonical decision record per job and scoring run. Every queue row must reference the exact record that authorized it.
5. Fail closed. Missing, stale, ineligible, or non-active-policy decisions cannot be approved or leased.
6. Preserve the existing JSON research artifacts during migration, but backfill them into the SQLite brain and stop treating them as an independent authority.
7. Use reviewed email outcomes only. Unrelated recommendation emails and unreviewed attribution are excluded from training and policy promotion.
8. Validate through historical replay before any live canary. A canary is defined by policy decisions/applications, not machine count.

## 3. Alternatives considered

### A. Keep the current scorer and only raise the threshold

This reduces volume but leaves title-based score inflation, missing provenance, and disconnected personalized data intact. It is a temporary safety measure, not a model fix.

### B. Make `research_fit_score` first in the existing `COALESCE`

This is easy to deploy but still collapses qualification and preference into one opaque number, has no durable policy/evidence record, and cannot reliably reproduce historical decisions. It also promotes an advisory score before outcome attribution is repaired.

### C. Canonical versioned decision authority (selected)

Create an explicit decision contract, make the TypeScript research pipeline produce it, store it in the brain, project only eligible active-policy decisions into the fleet, and preserve the decision ID in every queue row. This costs more implementation work but removes the architectural ambiguity that caused the current failure.

## 4. System boundaries

### SQLite brain (Python repository)

The brain owns schema, active-policy state, canonical decisions, reviewed outcomes, promotion state, and the denormalized current decision on each job. Python remains the only schema owner.

### Research and scoring (TypeScript repository)

The TypeScript pipeline reads jobs, labels, FitMap observations, KG artifacts, pairwise comparisons, and reviewed outcomes from the brain. It computes component scores and writes complete immutable decision records through `brainDb.ts`. It never edits fleet queues directly.

### Fleet Postgres (Python repository)

Fleet sync copies only promoted canonical decisions into `apply_queue` or `linkedin_queue`. Queue rows preserve decision identity and component provenance. Lease SQL validates active policy, action, freshness, and lane thresholds.

### Fleet workers

Workers execute leases. They do not score, reinterpret, or repair a decision. Central context distribution remains useful for research compute, but worker software/version consistency is independent from model policy consistency.

## 5. Canonical data model

### 5.1 `decision_policy_versions`

One row per policy build:

- `policy_version` TEXT primary key
- `lane` TEXT constrained to `ats` or `linkedin`
- `status` TEXT constrained to `draft`, `validated`, `canary`, `active`, `retired`
- `qualification_model`, `preference_model`, `outcome_model`
- `kg_version`, `label_snapshot`, `pairwise_snapshot`, `outcome_snapshot`
- `config_json`, `metrics_json`
- `created_at`, `validated_at`, `activated_at`, `retired_at`

Only one policy may be `active` per lane. Promotion from `draft` requires a recorded replay result. Activation and retirement are explicit operator actions.

### 5.2 `job_decisions`

Immutable row per job and policy evaluation:

- `decision_id` TEXT primary key
- `job_url` TEXT foreign key to `jobs.url`
- `policy_version` TEXT foreign key to `decision_policy_versions.policy_version`
- `lane` TEXT
- `qualification_score`, `preference_score`, `outcome_score`, `final_score` REAL
- `qualification_verdict` constrained to `qualified`, `unqualified`, `uncertain`
- `action` constrained to `apply`, `review`, `reject`
- `confidence` REAL and `uncertainty_json`
- `blockers_json`, `requirements_json`, `evidence_node_ids_json`
- `title_signals_json`, `explanation`, `input_hash`
- `created_at`, `expires_at`

Uniqueness is `(job_url, policy_version, input_hash)`. Re-scoring changed inputs creates a new record; records are never overwritten.

### 5.3 Current job projection

Add to `jobs`:

- `canonical_decision_id`
- `canonical_policy_version`
- `canonical_action`
- `canonical_score`
- `canonical_decided_at`

These columns are a cache for operational reads. The referenced `job_decisions` row remains authoritative.

### 5.4 Reviewed outcome projection

Create `reviewed_outcomes` keyed by effective email-event identity and job URL:

- raw event identity and attribution evidence
- `review_status`: `accepted`, `rejected`, `needs_review`
- normalized stage and weight
- reviewer, reason, and timestamps

No event enters model fitting until `review_status='accepted'`. Recommendation/newsletter mail is rejected before company/title matching. Automated assessments receive less weight than human screen invitations.

### 5.5 Fleet queue provenance

Add to both queue tables:

- `decision_id`, `policy_version`, `decision_action`
- `qualification_verdict`, `qualification_score`, `qualification_floor`
- `preference_score`, `outcome_score`
- `decision_confidence`, `decision_created_at`, `decision_expires_at`

Queue score remains available for ordering but is copied from `job_decisions.final_score`; it is not independently computed. Lease enforcement requires `qualification_verdict='qualified'` and `qualification_score >= qualification_floor`.

## 6. Scoring policy

### 6.1 Qualification gate

Qualification is computed from atomic requirements and FitMap/KG evidence:

- Must-have requirements are classified as met, transferable, missing, or uncertain.
- A hard blocker or an unfixable missing must-have forces `unqualified` regardless of title or preference.
- Transferable evidence must cite KG evidence nodes.
- Unsupported claims increase uncertainty and route to review.
- The existing audit title patterns may add negative safety flags or retrieval metadata, but may not increase qualification or final score.

### 6.2 Preference reranker

Preference applies only to qualified jobs. It combines pointwise labels and the pairwise comparison corpus using a regularized Bradley-Terry style model over stable role features. Sparse role families shrink toward neutral rather than inheriting a blanket title boost. The model records its training snapshot and support count; low-support predictions route to review when they materially affect the action.

### 6.3 Outcome calibration

Outcome learning uses accepted reviewed outcomes with time-aware censoring:

- Offer and interview are strongest positives.
- Human screen/call invitations are positive.
- Automated assessments are weaker positives.
- Rejections are weak negatives because employer-side factors are only partially observed.
- No response is not negative until the configured 30-45 day maturity window.
- Training and replay split by application time and group by job identity to prevent leakage.

Outcome score calibrates ranking among qualified jobs. It cannot rescue an unqualified job.

### 6.4 Final action

- `reject`: qualification is unqualified or a hard blocker exists.
- `review`: qualification is uncertain, evidence is missing, policy/model support is low, or scores fall in a configured uncertainty band.
- `apply`: qualification is qualified, no blocker exists, the policy is active/canary-authorized, and the combined preference/outcome ranking clears the lane budget threshold.

The policy configuration stores component weights and thresholds. No hidden environment variable may change authority without producing a new policy version.

## 7. Write and promotion flow

1. Python initializes additive schema.
2. A one-time importer backfills research scores, labels, pairwise labels, KG runs/artifacts, and reviewed outcomes into the brain with count/hash reconciliation.
3. TypeScript brain catalog mode becomes the default for canonical scoring; JSON remains an explicit fallback for analysis only.
4. A canonical scoring command writes a draft policy and immutable decisions.
5. Historical replay writes metrics into the policy row.
6. Operator promotion moves a validated policy to canary or active.
7. Fleet sync selects only `canonical_action='apply'` decisions for the selected lane policy and writes all provenance fields to the queue.
8. Lease SQL refuses rows with missing decision ID, mismatched policy, expired decision, non-apply action, or sub-threshold qualification.

## 8. Historical replay and release gates

Replay all available applications and a representative non-applied candidate set. Report:

- precision at the real application budget and at fixed K
- must-have false-positive rate
- reviewed screen/interview/offer rate by score band
- calibration error and abstention rate
- per-lane and role-family results
- disagreement sets against legacy audit, research score, and prior TypeScript final score
- exact promoted/rejected job examples with evidence

Release gates:

1. Zero unqualified jobs emitted as `apply` in the locked hard-negative set.
2. No title-only promotion across the qualification threshold.
3. Queue insert and lease tests prove fail-closed behavior.
4. Every replayed and canary queue row resolves to one immutable decision and active/canary policy.
5. Positive outcome events used in fitting are all accepted reviewed outcomes.
6. A policy must outperform legacy audit on the locked replay metrics or remain in `validated`/`review` mode.

## 9. Canary and rollback

The first live rollout uses 20-30 ATS decisions. LinkedIn has a separate policy/canary because its workflow and economics differ. During canary:

- only the selected policy version is leasable
- every decision is inspectable before application
- no automatic threshold lowering or title rescue is permitted
- policy metrics and outcomes are attributed by decision ID

Rollback retires the policy, pauses the lane, and invalidates queued rows from that policy. Rollback never re-enables legacy `COALESCE(audit_score, fit_score)` authority. The safe fallback is human review, not the old scorer.

## 10. Error handling and observability

- Brain scoring fails if the DB path guard, schema version, KG artifact, label snapshot, or active resume hash is missing.
- Writes are transactional and idempotent by policy/input hash.
- Fleet sync emits explicit rejection counts by reason.
- Queue status surfaces decision/policy drift, stale decisions, unresolved provenance, and rejected outcome-attribution counts.
- Policy activation and queue invalidation are audited operator events.

## 11. Implementation slices

1. Runtime schema and canonical decision repository.
2. Queue provenance plus fail-closed push/lease behavior.
3. Brain backfill and reconciliation commands.
4. TS brain catalog cutover and canonical decision writer.
5. Qualification authority with title-boost removal.
6. Pairwise preference reranker.
7. Reviewed outcome ingestion and calibrated outcome features.
8. Historical replay, promotion CLI, separate ATS/LinkedIn canaries, and operator runbook.

Each slice is independently tested and committed in its repository. Cross-repository compatibility is versioned through the brain schema and decision contract, not branch naming or deployment timing.

## 12. Explicit non-goals

- Training new DeepSeek foundation-model weights.
- Using Tailscale/SSH as a model-policy distribution mechanism.
- Replacing broad discovery with a narrow title allowlist.
- Allowing workers to make local scoring decisions.
- Treating unreviewed Gmail attribution or immature no-response as training truth.
- Automatically reopening ATS after implementation. Reopening requires replay review and explicit policy promotion.

## 13. Success criteria

The rebuild is complete when a newly discovered job can be traced from source data through atomic requirements, KG evidence, preference/outcome components, one immutable canonical decision, one promoted policy, one provenance-complete queue row, and one outcome timeline; and no application can bypass that chain.
