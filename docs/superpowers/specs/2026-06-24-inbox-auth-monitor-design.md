# Inbox Auth Monitor Design

## Goal

ApplyPilot should handle email-based application verification without turning account authentication into unsafe password automation. When a job application requires an email verification code or magic link, ApplyPilot should monitor the inbox, match the message to the active job, automatically enter high-confidence codes, and record the event in the application tracker.

This design is focused on job-application verification emails from ATS platforms such as Greenhouse, Workday, Lever, Ashby, iCIMS, SmartRecruiters, Oracle/Taleo, and Workable.

## Non-Goals

ApplyPilot must not automate Google passwords, Google passkeys, authenticator-app 2FA, suspicious-login challenges, SMS codes, ID verification, biometric checks, or recovery prompts. Those remain human checkpoints. The first version also does not create a general email client or permanently archive raw email bodies.

## User Policy

ApplyPilot is allowed to automatically enter email verification codes when all of the following are true:

- The code or link comes from a recent email received during an active apply/auth window.
- The sender, subject, body, ATS domain, company, or job context matches the active application with high confidence.
- The message is an application verification email, not a Google account security prompt or unrelated login alert.
- The page is asking for an email verification code or magic link for the job application flow.

If confidence is not high, ApplyPilot should surface the candidate code/link and pause for human confirmation.

## Architecture

Add three bounded pieces to the existing ApplyPilot workflow:

1. `inbox_events`: durable metadata for relevant inbox messages.
2. `auth_challenges`: durable state for application flows waiting on email verification.
3. `inbox_auth`: Gmail read-only scanner plus code/link extraction and challenge matching.

The existing `applications` and `application_events` tables remain the source of truth for job/application state. Inbox rows should link back to job URLs whenever possible.

## Database Changes

Add `inbox_events`:

- `id`
- `message_id`
- `thread_id`
- `sender`
- `sender_domain`
- `subject`
- `received_at`
- `event_type`
- `confidence`
- `matched_job_url`
- `matched_company`
- `matched_method`
- `snippet`
- `created_at`

Add `auth_challenges`:

- `id`
- `job_url`
- `application_url`
- `provider`
- `challenge_type`
- `status`
- `requested_at`
- `expires_at`
- `resolved_at`
- `inbox_event_id`
- `attempt_count`
- `last_error`
- `created_at`
- `updated_at`

Do not permanently store raw email bodies. Do not permanently store extracted auth codes. A short-lived in-memory value can be returned to the apply worker for immediate entry, and metadata can be stored for auditability.

## Gmail Integration

Reuse the existing read-only Gmail OAuth pattern from `gmail_outcomes.py`:

- Scope remains `gmail.readonly`.
- Credentials default to `.applypilot/gmail_credentials.json`.
- Token defaults to `.applypilot/gmail_token.json`.
- First run opens a local OAuth consent browser.

Add a scanner that supports two modes:

- `scan`: classify recent messages and write `inbox_events`.
- `watch`: poll Gmail for a bounded time window while an application is waiting for a code.

Search queries should prioritize recent and likely verification messages, for example:

- `newer_than:1d (verification OR code OR "one-time" OR "confirm your email" OR "magic link")`
- sender/domain filters for known ATS systems where practical.

## Code and Link Extraction

Extraction should be deterministic first:

- Numeric codes: 4 to 8 digits near words such as `code`, `verification`, `one-time`, `OTP`, `confirm`, or `Greenhouse`.
- Magic links: URLs near text such as `verify`, `confirm`, `continue`, or `sign in`.
- Ignore common false positives such as years, phone numbers, currency, job IDs, and tracking pixels.

Each candidate gets a confidence score based on:

- Message recency inside the active challenge window.
- Sender domain known for the ATS.
- Subject/body verification language.
- Company/job title match.
- Application domain match.
- Whether exactly one plausible code/link exists.

Only high-confidence candidates are auto-entered. Medium/low confidence candidates create a manual checkpoint.

## Apply Flow

Add an `--inbox-auth` option to `apply`.

When enabled:

1. The apply agent reaches an email verification step.
2. It reports a structured result or tool event indicating `email_verification_required`.
3. The launcher creates an `auth_challenge` row with a short expiry, default 5 minutes.
4. The Gmail monitor polls for matching messages.
5. If a high-confidence code/link arrives, the launcher passes it back to the active browser flow.
6. The apply agent enters the code or opens the magic link and continues.
7. The challenge is marked `resolved`, `expired`, `manual_required`, or `failed`.
8. `applications` and `application_events` receive an audit event.

If the page asks for password, Google SSO password, passkey, authenticator app, SMS code, suspicious-login confirmation, or recovery information, ApplyPilot must not auto-handle it. It should mark the job `auth_required` and route it to assisted apply.

## CLI

Add:

```powershell
.\run-applypilot.ps1 inbox --scan --days 1
.\run-applypilot.ps1 inbox --watch --auth-codes --minutes 5
.\run-applypilot.ps1 inbox --auth-required
.\run-applypilot.ps1 apply --inbox-auth ...
.\run-applypilot.ps1 assist-apply <url> --watch-inbox
```

`inbox --auth-required` should show jobs/challenges waiting on human login or email confirmation.

## Error Handling

Handle these cases explicitly:

- Gmail credentials missing: explain setup path and keep apply running without inbox auth.
- Gmail token expired: refresh if possible; otherwise pause with actionable instructions.
- No email found before expiry: mark challenge `expired` and job `auth_required`.
- Multiple plausible codes: pause for confirmation.
- Message is Google/security alert: never auto-enter; mark `manual_required`.
- Code entry fails: retry once if a newer matching email arrives, otherwise mark `failed`.
- Magic link opens a different account/security flow: stop and mark `manual_required`.

## Resilience Requirements

The inbox-auth workflow must be resumable and safe across crashes, retries, duplicate emails, and partial apply attempts:

- Gmail message processing must be idempotent by `message_id`; the same email cannot create duplicate inbox events.
- Auth challenges must have explicit statuses: `pending`, `watching`, `resolved`, `expired`, `manual_required`, and `failed`.
- A stale `watching` challenge older than its expiry must be recoverable on the next run by marking it `expired`.
- The Gmail watcher must use bounded retries with backoff for transient Gmail API failures and must not block the entire apply run indefinitely.
- The apply worker must be able to fail closed: if inbox auth cannot start, cannot refresh Gmail OAuth, or cannot find a code in time, the job becomes `auth_required` rather than looping or falsely marking the job applied.
- Magic links and auth codes must be single-use in the workflow: once a challenge is resolved, later matching emails must be recorded as inbox events but must not be auto-applied to that resolved challenge.
- The database writes for inbox events and challenge state changes must commit before the apply worker attempts to use a code/link, so a crash leaves an auditable state.
- The tracker must preserve enough metadata to explain what happened without storing raw email bodies or permanent auth codes.
- Concurrent apply workers must not resolve the same challenge twice. Challenge resolution should update by `id` and current expected status.
- The CLI must expose recovery views for `pending`, `watching`, `expired`, `manual_required`, and `failed` auth challenges.

## Security Boundaries

- Read-only Gmail only.
- No raw email body persistence.
- No permanent auth-code persistence.
- No password storage.
- No Google account security automation.
- No code entry unless tied to an active ApplyPilot challenge.
- Every auto-entered code must leave an audit trail with message metadata, match confidence, job URL, and timestamp.

## Testing

Add unit tests for:

- Verification-code extraction.
- Magic-link extraction.
- False-positive suppression.
- Confidence scoring.
- Challenge creation and expiry.
- Inbox event idempotency by Gmail message ID.
- `apply --inbox-auth` routing on email verification.
- Safety stops for Google SSO, password prompts, passkeys, and 2FA.

Add integration-style tests with mocked Gmail responses:

- Greenhouse code arrives and is auto-entered.
- Multiple candidates cause manual checkpoint.
- No message before timeout marks challenge expired.
- Application tracker receives an event for code resolution.

## Acceptance Criteria

- A Greenhouse email verification code received during an active apply run can be detected and auto-entered without storing the code permanently.
- The job/application tracker shows that an inbox auth event occurred.
- Non-email authentication remains human-only.
- Existing `scan-gmail` outcome tracking keeps working.
- The feature is opt-in via `--inbox-auth`.
