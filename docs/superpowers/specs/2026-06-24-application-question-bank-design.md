# Application Question Bank Design

Date: 2026-06-24

## Context

ApplyPilot currently gives the apply agent general screening guidance, but it does not persist application questions or approved answers in a reusable database. When a form asks duplicate or recurring questions, the agent must infer the answer again from the profile and prompt. That is fragile for ordinary questions and unacceptable for legal, background, signature, or attestation questions.

The user provided an IDEA Public Schools application page as an example. That page contains repeated question labels, long legal-affidavit prompts, relationship dropdown options, date/name fields, and already-filled values. The example should be treated as representative input, not as a one-off hardcoded source.

## Goals

- Deduplicate repeated application questions into canonical question records.
- Create answer records for every canonical question, including unanswered pending records.
- Let the user answer a question once and reuse the approved answer on future applications.
- Preserve every source occurrence so ApplyPilot can audit where a question came from.
- Categorize sensitive questions so ApplyPilot never auto-submits risky answers without explicit user approval.
- Support import from pasted application text dumps now, with room for browser-captured questions later.
- Provide CLI commands to import, list, answer, and export questions.

## Non-Goals

- Do not auto-answer legal/background/signature questions without user approval.
- Do not hardcode answers from a single example application.
- Do not build a full review UI in this first pass.
- Do not delete source evidence. Deduplication should create canonical records while preserving per-application instances.
- Do not change the apply agent to rely on unapproved answers.

## Core Principle

Deduplicate questions, not responsibility.

If the same question appears twice on one application or appears across many employers, ApplyPilot should store one canonical question. But each canonical question still needs an answer state:

- approved answer if the user has explicitly answered it.
- suggested answer if it can be safely inferred from the profile.
- pending/manual answer if the question is sensitive, ambiguous, or legally meaningful.

## Data Model

### `application_questions`

One row per canonical deduplicated question.

Columns:

- `id`
- `question_hash`
- `question_text`
- `normalized_text`
- `category`
- `risk_level`
- `answer_type`
- `first_seen_at`
- `last_seen_at`
- `created_at`
- `updated_at`

Recommended `category` values:

- `work_authorization`
- `driver_license`
- `legal_background`
- `employment_history`
- `education_affidavit`
- `relationship_disclosure`
- `eeo`
- `signature`
- `date`
- `name`
- `salary`
- `availability`
- `narrative`
- `other`

Recommended `risk_level` values:

- `safe`
- `sensitive`
- `legal_attestation`
- `manual_only`

Recommended `answer_type` values:

- `yes_no`
- `text`
- `date`
- `select`
- `multi_select`
- `signature`
- `unknown`

### `application_question_instances`

One row per observed occurrence of a canonical question.

Columns:

- `id`
- `question_id`
- `job_url`
- `application_url`
- `company`
- `job_title`
- `source`
- `source_file`
- `raw_text`
- `required`
- `options_json`
- `seen_at`
- `created_at`

This keeps the system auditable: even if 20 repeated labels map to one canonical question, ApplyPilot can still show where each instance came from.

### `application_question_answers`

One current answer record per canonical question, with room for history later.

Columns:

- `id`
- `question_id`
- `answer_text`
- `answer_status`
- `answer_source`
- `confidence`
- `auto_submit_allowed`
- `notes`
- `approved_at`
- `approved_by`
- `created_at`
- `updated_at`

Recommended `answer_status` values:

- `pending`
- `suggested`
- `approved`
- `manual_only`
- `retired`

Recommended `answer_source` values:

- `profile`
- `user`
- `llm_draft`
- `manual`
- `unknown`

## Normalization and Deduplication

The importer should normalize question text by:

- removing duplicate required markers such as `*`.
- normalizing whitespace and non-breaking spaces.
- removing repeated decorative symbols.
- lowercasing for hash computation.
- preserving the original display text in `question_text`.

It should not dedupe different legal questions just because they share words. Long legal prompts should hash from the complete normalized text.

The importer should skip obvious non-question page chrome when possible:

- company logo labels.
- step counters.
- location footer text.
- already-filled user values such as a typed name.

Dropdown choices should be stored as `options_json` on the relevant instance when detected.

## Risk Classification

The first implementation can use deterministic keyword rules.

`legal_attestation`:

- felony
- convicted
- nolo contendere
- deferred adjudication
- child abuse
- investigation
- terminated
- non-renewed
- discharged
- license revoked
- license suspended
- Do Not Hire Registry
- truthful and accurate
- affidavit
- consent for release of records

`sensitive`:

- gender
- Hispanic or Latino
- race
- disability
- veteran
- relationship to employee or board member

`safe`:

- work authorization
- sponsorship
- name
- date
- phone
- email
- LinkedIn

Even when a question is classified as `safe`, the answer should only become reusable after it is either explicitly approved or sourced from a known profile field.

## Answer Behavior

When importing questions, ApplyPilot should create an answer row for each canonical question.

Default answer status:

- `suggested` for profile-backed safe questions.
- `pending` for questions that need user input.
- `manual_only` for legal attestations until the user explicitly approves a reusable answer.

Examples:

- "Are you legally able to work in the U.S. without visa sponsorship?" can be suggested from `profile.work_authorization.legally_authorized_to_work`.
- "Will you now, or in the future, require sponsorship?" can be suggested from `profile.work_authorization.require_sponsorship`.
- EEO questions should default to the profile's EEO defaults if present, usually "Decline to self-identify" or equivalent.
- Criminal/background/affidavit questions must stay pending or manual-only unless the user directly supplies the answer.

## CLI Commands

### Import

```powershell
.\.venv\Scripts\python.exe -m applypilot import-questions "path\to\pasted-text.txt" --company "Company" --job-title "Role" --job-url "https://example.com/job"
```

Expected output:

- raw lines scanned.
- question-like lines detected.
- canonical questions created.
- existing canonical questions reused.
- duplicate instances skipped or recorded.
- pending answers created.

### List

```powershell
.\.venv\Scripts\python.exe -m applypilot list-questions --pending
.\.venv\Scripts\python.exe -m applypilot list-questions --risk legal_attestation
.\.venv\Scripts\python.exe -m applypilot list-questions --company "IDEA Public Schools"
```

### Answer

```powershell
.\.venv\Scripts\python.exe -m applypilot answer-question 12 --answer "No" --approve
.\.venv\Scripts\python.exe -m applypilot answer-question 18 --manual-only --notes "Review per application"
```

### Export

```powershell
.\.venv\Scripts\python.exe -m applypilot export-questions
```

Exports should include CSV and JSONL files in `.applypilot/application_question_exports/`.

## Apply-Agent Integration

Initial implementation should not auto-submit using this bank. It should build the database and let the user answer the queue first.

Later integration should:

1. Match visible form questions to canonical questions.
2. Use approved answers only.
3. Use suggested answers only if `auto_submit_allowed` is true.
4. Stop with manual handoff for pending, manual-only, or unmatched legal questions.
5. Record which answer was used for each application.

## Testing Plan

Unit tests:

- normalizes duplicated question strings.
- imports repeated question lines into one canonical question.
- creates separate instances for separate source occurrences.
- creates pending answer records for every canonical question.
- classifies legal/background questions as `legal_attestation`.
- suggests work authorization answers from profile defaults.
- skips obvious page chrome and filled values.

CLI tests:

- `import-questions` creates canonical rows and reports duplicate counts.
- `list-questions --pending` shows unanswered questions.
- `answer-question --approve` updates the answer row.
- `export-questions` writes CSV and JSONL outputs.

Database tests:

- schema is created idempotently.
- `question_hash` is unique.
- instances can reference canonical questions.
- answers can reference canonical questions.

## Error Handling

- Missing import file should fail with a clear path message.
- Malformed or empty imports should create no rows and report zero questions.
- Duplicate imports should be idempotent for canonical questions.
- Database writes should be transactional so a failed import does not partially create orphan records.
- Unknown categories should fall back to `other` and `pending`.

## Implementation Boundary

First pass:

- schema.
- importer.
- deterministic dedupe and classification.
- CLI import/list/answer/export.
- tests.

Second pass:

- apply-agent prompt integration.
- browser question capture.
- answer usage tracking during real applications.
- optional review UI.

This order lets the user start answering a durable question queue immediately without risking incorrect auto-submission.
