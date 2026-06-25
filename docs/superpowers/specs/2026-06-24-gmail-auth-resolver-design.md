# Gmail Auth Resolver Design

Date: 2026-06-24

## Context

ApplyPilot can already detect when an application run hits login, account, email verification, or two-factor gates and return `AUTH_REQUIRED`. The current Gmail integration can build a read-only Gmail service and scan messages, and the apply prompt can receive an inbox auth hint. The missing hardening is a resolver that chooses the right email for the right job, limits retries, and makes the behavior observable.

The goal is to help with common ATS email-code and magic-link verification without turning ApplyPilot into a general inbox automation agent.

## Goals

- Resolve common email verification gates for job applications with minimal user babysitting.
- Keep Gmail access read-only.
- Only use high-confidence verification codes or magic links.
- Match auth emails to the active job or ATS provider when possible.
- Retry each challenge at most once with an inbox hint.
- Persist every challenge and inbox event for auditability.
- Fall back to manual handoff when confidence is low, the hint is rejected, or the site requires passwords, SSO, SMS, authenticator apps, CAPTCHA, or security review.

## Non-Goals

- Do not automate Google, Microsoft, or employer SSO login.
- Do not read arbitrary Gmail content outside recent verification/outcome queries.
- Do not handle SMS, authenticator apps, password reset flows, biometric checks, identity verification, or CAPTCHA solving through Gmail.
- Do not submit applications when the form question or auth condition is legally or personally ambiguous.

## Recommended Approach

Use a bounded assisted-auth resolver:

1. The apply worker runs normally.
2. If the result is `AUTH_REQUIRED`, `email_verification_required`, or an equivalent auth code, the worker creates or reuses an active `auth_challenge`.
3. The resolver polls Gmail for recent messages in a short window, defaulting to 15 minutes.
4. Candidate messages are parsed for verification codes and magic links.
5. Candidates are scored with provider, employer, language, recency, and candidate-type signals.
6. A high-confidence winner becomes an inbox auth hint.
7. The same job is retried once with the hint in the apply-agent prompt.
8. Success resolves the challenge. Failure records the error and leaves the job available for manual handoff.

This keeps the high-value automation path fast while avoiding risky behavior when several applications or unrelated codes arrive at once.

## Components

### `inbox_auth` Resolver

The resolver should expose a function that accepts job context and Gmail service:

- `job_url`
- `application_url`
- `company`
- `job_title`
- `provider`
- `requested_at`
- scan window and max message settings

It returns either:

- a selected `AuthEmailMatch` with confidence details, or
- a structured no-match result with reasons.

### Domain-Aware Matcher

The matcher should prefer messages with evidence tied to the current job:

- ATS sender domain matches provider, such as Greenhouse, Lever, Ashby, Workday, SmartRecruiters, or Workable.
- Sender or subject mentions the employer.
- Body mentions the employer, job title, or application context.
- Message is newer than challenge creation time, or close to it with a small tolerance.
- Candidate has verification language like `verify your email`, `one-time code`, `continue your application`, or `magic link`.

It should reject or downgrade:

- generic account security emails unrelated to the active job.
- messages from password reset flows.
- banking, Google, Microsoft, Apple, or unrelated service verification emails.
- candidates with multiple different high-confidence codes unless provider/employer matching breaks the tie.

### Persistence

Use the existing database concepts:

- `auth_challenges`: one row per job/application/provider challenge.
- `inbox_events`: one row per detected auth email.

Add or verify fields that support:

- challenge status: `pending`, `watching`, `resolved`, `expired`, `failed`, `manual_required`.
- attempt count.
- selected inbox event id.
- last resolver reason or error.
- timestamps for requested, watched, resolved, and expired states.

### Apply Prompt Handoff

When a high-confidence match is found, pass a small hint into the apply prompt:

- code or magic link
- sender
- subject
- received time
- confidence reasons

The agent instructions should be strict:

- enter the code exactly when the page asks for it.
- open the magic link only when that is the requested flow.
- if rejected once, stop with `RESULT:AUTH_REQUIRED`.
- never use the hint for SSO, password reset, account recovery, CAPTCHA, SMS, or authenticator prompts.

### CLI and Visibility

Keep Gmail auth opt-in:

- `apply --inbox-auth`
- `apply --no-inbox-auth`

Add visibility commands or status output:

- unresolved auth challenges.
- recent inbox events.
- jobs requiring manual auth handoff.
- last resolver reason for skipped or ambiguous candidates.

## Data Flow

1. Apply worker acquires a job.
2. Browser agent attempts the application.
3. Agent returns `AUTH_REQUIRED` or a related code.
4. Worker checks `APPLYPILOT_INBOX_AUTH`.
5. Worker creates or reuses an `auth_challenge`.
6. Gmail resolver scans recent messages.
7. Resolver records relevant `inbox_events`.
8. Resolver selects one candidate only if confidence is high.
9. Worker retries the job with `inbox_auth_hint`.
10. Worker records final outcome.

## Safety Rules

- Gmail API scope must remain read-only.
- Default scan window should stay short, 15 minutes.
- One assisted retry per challenge.
- Never guess or create passwords.
- Never automate SSO.
- Never use Gmail codes for unrelated domains.
- Never claim `APPLIED` unless the application page shows a confirmed success state.
- Persist enough evidence to debug why a code was or was not used.

## Configuration

Recommended environment variables:

- `APPLYPILOT_INBOX_AUTH=1`
- `APPLYPILOT_INBOX_AUTH_TIMEOUT=300`
- `APPLYPILOT_INBOX_AUTH_POLL_SECONDS=5`
- `APPLYPILOT_INBOX_AUTH_MINUTES=15`
- `APPLYPILOT_INBOX_AUTH_MAX_MESSAGES=25`
- `APPLYPILOT_INBOX_AUTH_MAX_ERRORS=3`
- `APPLYPILOT_INBOX_AUTH_CHALLENGE_TYPE=email_code`

Optional future settings:

- `APPLYPILOT_INBOX_AUTH_MAX_RETRIES=1`
- `APPLYPILOT_INBOX_AUTH_MIN_CONFIDENCE=high`
- `APPLYPILOT_INBOX_AUTH_REQUIRE_DOMAIN_MATCH=1`

## Error Handling

If Gmail credentials are missing, the resolver should log the setup path and return manual handoff without crashing the whole apply run.

If Gmail polling fails repeatedly, mark the challenge `failed` with the error and continue to the next job.

If there are no candidates, mark the challenge `expired` or `manual_required` depending on timeout.

If multiple plausible candidates exist, do not pick one unless domain and recency strongly disambiguate. Otherwise mark manual.

If the apply agent rejects the hint, mark the challenge `failed` or `manual_required` and do not retry again automatically.

## Testing Plan

Unit tests:

- extracts high-confidence email codes.
- extracts high-confidence magic links.
- rejects password reset and unrelated verification messages.
- forwards scan window and max message parameters.
- domain matcher prefers ATS/provider sender over unrelated sender.
- matcher rejects ambiguous same-window multi-code cases.
- retry cap prevents repeated assisted attempts.

Integration-style tests with fake Gmail service:

- `AUTH_REQUIRED` creates a challenge, finds a matching email, retries once with hint, and resolves the challenge.
- no matching email leaves the job manual and does not crash worker loop.
- rejected hint does not trigger repeated retries.

CLI tests:

- `--inbox-auth` sets the env flag.
- `--no-inbox-auth` disables it.
- status command lists unresolved auth challenges.

## Open Decisions

- Whether unresolved auth challenges should block the job indefinitely or return to manual queue immediately.
- Whether domain matching should be required by default or strongly preferred by score.
- Whether application-question review should share the same manual-handoff surface as auth challenges.

Recommendation: unresolved auth challenges should become manual handoff rows immediately, and domain matching should be strongly preferred rather than mandatory because some ATS emails use generic sender domains.
