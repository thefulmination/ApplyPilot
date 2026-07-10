# Email Authentication Defense-in-Depth Design

Date: 2026-07-10
Status: Approved for specification by the owner on 2026-07-10

## Objective

Make job-application email authentication reliable across local and remote
workers without weakening the existing security boundary. ApplyPilot may read
recent application-verification messages, use one high-confidence code or magic
link once, and retry the application once. It must not automate passwords, SSO,
SMS, authenticator applications, account recovery, CAPTCHA, or identity checks.

## Current Evidence

The existing implementation already provides:

- read-only Gmail access through IMAP app-password credentials, with a legacy
  Gmail API fallback;
- a Postgres OTP relay for remote workers;
- request-time and provider-domain filtering in the relay;
- single-use code consumption and short-lived code storage;
- a persistent home responder with a current heartbeat;
- DeadMan mail-source and responder checks; and
- focused unit and integration-style coverage.

The 2026-07-10 audit also identified remaining reliability gaps:

1. An auth-required retry can abandon a prearmed request and create a second
   request. The responder can answer the first row while the worker polls the
   second.
2. Local-mode polling can select a message that predates the active challenge
   or belongs to a different ATS provider.
3. A message ID is reserved only inside one responder scan. A later scan can
   reuse the same email for another request.
4. A second responder process can race the first because scan ownership is not
   protected by a database singleton lock.
5. DeadMan does not treat a missing responder heartbeat as critical when a
   request is pending, and it does not distinguish a live responder from a
   responder that is failing to answer old pending requests.
6. The Gmail API fallback reads one page, while the IMAP path and current busy
   inbox budget can require more than one Gmail API page.

## Chosen Approach

Use defense-in-depth hardening inside the existing `mail_source` ->
`inbox_auth` -> `otp_relay` -> apply-worker boundaries. Do not introduce a new
service or place Gmail credentials on remote workers.

The rejected minimal approach fixes only duplicate requests and stale local
messages, but leaves message reuse, responder races, and monitoring blind spots.
Provider-wide job serialization was also rejected for now because it reduces
fleet throughput and is unnecessary when ambiguous cases can fail closed.

## Reliability Invariants

1. One application attempt owns one OTP request ID from prearm through final
   poll and optional assisted retry.
2. A request remains valid for the agent runtime plus the full post-run auth
   wait. Its TTL is at least `agent_timeout + inbox_auth_timeout`.
3. An email is eligible only when its received time is on or after
   `requested_at - skew_seconds`; the default skew remains 60 seconds.
4. An email must belong to the request's ATS provider group through sender
   domain or magic-link domain evidence.
5. One Gmail `message_id` can answer at most one request across all responder
   cycles and processes.
6. One responder owns mailbox matching at a time.
7. Ambiguous concurrent challenges fail closed. No code is guessed or assigned
   when provider and timing evidence cannot identify one request.
8. A worker performs at most one assisted retry with a code or magic link.
9. Codes are never logged and are nulled atomically when consumed or expired.
10. A pending request with no healthy responder, or an overdue request with a
    fresh responder heartbeat, produces an operator-visible alert.

## Component Changes

### Request Lifecycle

`launcher._prearm_inbox_auth_request` creates the request once. The apply worker
retains that ID and polls the same row after the first browser run. If the first
short post-run poll finds no code and the run explicitly reports an email-auth
wall, the worker continues polling that request for the unused portion of the
configured auth window. The combined post-run polls must not exceed
`APPLYPILOT_INBOX_AUTH_TIMEOUT`. It does not call the path that inserts another
request.

`request_code` reuses an active unexpired request for the same worker and
application URL when a caller repeats the operation after a recoverable local
error. The prearm TTL covers the agent timeout plus the complete inbox-auth
timeout. The answered TTL may extend availability but may never revive an
already expired request.

### Shared Candidate Eligibility

Move request-relative filtering into shared `inbox_auth` helpers used by both
local polling and the relay. The helper accepts:

- `not_before` challenge time;
- provider/application domain;
- clock-skew tolerance; and
- already-consumed message IDs.

It rejects unparseable dates, stale messages, unrelated provider domains,
Google or account-security prompts, and candidates without high confidence.
The local watcher passes the challenge creation time and provider domain, so it
cannot select a previous or cross-provider code from the broader scan window.

When multiple pending requests and candidates share a provider, construct the
request/candidate eligibility graph from provider and time constraints.
Iteratively assign only a candidate and request that are each other's sole
remaining eligible partner. If an ambiguous connected component remains, leave
that component unanswered for manual handoff rather than risk cross-entering a
code.

### Persistent Message Idempotency

Add nullable `matched_message_id TEXT` to `otp_request`. Create partial unique
index `idx_otp_matched_message_unique` for non-null values. The responder loads
previously matched IDs and writes the chosen message ID in the same guarded
update that writes the short-lived code. A uniqueness conflict is treated as
"already used" and does not expose or log the code.

The message ID is non-secret audit metadata. Raw bodies and permanent codes
remain prohibited.

### Responder Singleton

`answer_pending` acquires the Postgres session advisory lock keyed by
`hashtext('applypilot:otp_responder')` for the matching operation. If another
responder owns the lock, the cycle exits without scanning Gmail. The lock is
released in `finally`, including mail-source and database failures. This
protects against scheduled-task, Startup-shortcut, supervisor, or manual
process overlap.

### Mail Source Completeness

Both mail backends preserve the same normalized `MailMessage` contract.

- IMAP continues to use the bounded Gmail `X-GM-RAW` verification query and
  raises explicit protocol errors.
- Gmail API fallback follows `nextPageToken` until it reaches the caller's
  message budget or no page remains, using at most 500 messages per API call.
- Local polling uses the same verification query and a busy-inbox-safe default
  budget of 1000 messages, overridable by the existing environment setting.

The parser, confidence rules, and read-only Gmail boundary remain unchanged.

### Monitoring

DeadMan evaluates responder health against actual demand:

- no pending request: a never-started responder does not create noise;
- pending request plus no heartbeat: critical `otp_relay_down`;
- pending request plus stale heartbeat: critical `otp_relay_down`;
- pending request older than the delivery threshold while heartbeat and mail
  source are healthy: critical `otp_delivery_stalled`;
- failed IMAP or Gmail API health probe: critical `otp_relay_down`.

The delivery threshold defaults to 120 seconds and is configurable through
`APPLYPILOT_OTP_STALL_SECONDS` for environments with known delivery latency.

Alerts include counts and ages but never sender contents, subjects, codes, magic
links, credentials, or DSNs.

## Data Flow

1. An auth-gated job is leased and one relay request is prearmed.
2. The browser attempts the application while the home responder scans only if
   pending requests exist.
3. The responder takes the singleton lock, retrieves bounded candidate mail,
   and applies shared time/provider/confidence checks.
4. A uniquely eligible message is persisted with `matched_message_id`, the
   short-lived code, and audit timestamps in one guarded update.
5. The worker polls the original request ID, atomically captures and nulls the
   code, and performs one assisted retry.
6. If no unique candidate arrives before expiry, the job remains
   `auth_required`/manual rather than being falsely marked applied or retried in
   a loop.
7. DeadMan reports unavailable or stalled relay service while demand exists.

## Error Handling

- Mail login, select, search, fetch, or API errors are surfaced to the responder
  loop and monitoring; they are not reported as a successful empty scan.
- A malformed or unparseable message timestamp is ineligible.
- An expired request cannot be revived during a slow mailbox scan.
- A duplicate message ID cannot answer a second request.
- A responder lock miss is a normal skipped cycle, not an error.
- A missing code after the bounded wait returns the existing safe auth-required
  outcome.
- The worker never performs a second assisted retry after a rejected hint.

## Testing Strategy

Regression tests must first reproduce each identified gap and fail against the
pre-change implementation.

### Unit and PG-backed tests

- prearmed auth-required flow continues polling the original request and never
  inserts a second row;
- prearm TTL covers agent runtime plus the full inbox-auth wait;
- local watcher rejects a recent but pre-challenge code;
- local watcher rejects a different ATS provider and selects the valid provider;
- the same message ID cannot answer another request in a later responder cycle;
- expired requests are not revived;
- concurrent responder lock ownership prevents a second scan;
- ambiguous same-provider mappings remain unanswered;
- Gmail API fallback paginates and honors the exact caller budget;
- DeadMan alerts for pending-without-heartbeat and overdue-pending conditions;
- DeadMan remains quiet when there is no demand and no historical heartbeat.

### Broader verification

- run the full inbox-auth, mail-source, relay, responder, launcher, apply-worker,
  DeadMan, startup, and runbook test slices;
- run Gmail outcome tests to prove the shared mail-source changes do not regress
  outcome tracking;
- run `git diff --check` on the isolated change set;
- run a privacy-safe live IMAP canary using the verification query;
- verify the responder process, persistence mechanism, heartbeat freshness,
  pending request count, and alert state; and
- complete a controlled end-to-end OTP cycle using a real application challenge
  or an explicitly approved test message delivered to the monitored inbox:
  request created, responder answers, worker consumes, code is cleared, message
  ID is retained, assisted retry succeeds or reaches the expected controlled
  terminal state, and no DeadMan alert remains.

## Rollout

1. Implement and verify in an isolated worktree based on the current OTP
   hardening commit.
2. Apply the idempotent schema migration before restarting the responder.
3. Restart the home responder and confirm exactly one active matcher.
4. Run the live canary and controlled OTP cycle.
5. Publish the focused branch and open or hand off the pull request without
   including unrelated dirty fleet changes.

Rollback is code-only for behavior. The nullable metadata column and unique
index may remain because older code ignores them.

## Acceptance Criteria

The work is complete only when all of the following are proven in current state:

- one apply attempt uses one relay request from prearm through consumption;
- stale, wrong-provider, ambiguous, and previously used messages are not
  auto-entered;
- one message and one code are delivered at most once;
- local and relay modes apply the same request-relative eligibility rules;
- IMAP and Gmail API fallback both satisfy the bounded scan contract;
- pending demand cannot coexist silently with a missing, stale, or stalled
  responder;
- no password, OAuth secret, app password, raw body, code, or magic link is
  logged or moved to a remote worker outside the short-lived existing hint;
- all focused and broader regression suites pass; and
- the controlled live OTP cycle and post-cycle health checks pass.
