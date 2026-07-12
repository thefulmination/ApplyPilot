# Workday Adapter And Answer Cache Design

**Goal:** Reduce per-application LLM spend by completing deterministic Workday field mapping and resolving approved answers before any model call.

## Adapter Path

`build_canonical_resume` remains the factual source for work history and education. `WorkdayAdapterRunner` passes that canonical data into field planning. Dynamic Workday groups are mapped by stable group identity and occurrence order, including company, title, location, school, degree, and field of study. Role descriptions are populated only from explicit resume bullets; unsupported or missing facts remain parked.

The adapter must never infer historical employment facts from the target job, current address, or model output. Existing validation and exception-queue behavior remains fail-closed.

## Answer Cache Path

The approved-answer resolver runs before any LLM resolver. Cache keys are normalized by tenant host, field key when available, and normalized question text. Only answers marked approved or explicitly resolved are returned. Pending, rejected, guessed, and ambiguous answers are cache misses. Cache hits are recorded in execution metadata so avoided model calls and estimated cost can be measured.

## Verification

Unit tests cover indexed Workday experience/education fields, exact source attribution, missing-fact parking, cache hit/miss behavior, host isolation, and usage metrics. The Workday suite, answer-cache suite, lint, compilation, and a fresh non-submitting Workday prepare pass must pass before a new submission canary is considered.
