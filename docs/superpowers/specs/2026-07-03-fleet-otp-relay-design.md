# Fleet-wide OTP / Email-Verification Relay — Design

**Date:** 2026-07-03
**Status:** Approved by owner (brainstorming session)
**Scope:** Finish the deferred centralized email-verification-code relay so a remote apply worker (the Mac, and any future offsite machine) can clear an email-verification wall using a code read from a **single** Gmail-connected home box — Gmail credentials never leave that home box.

## Goal

When a remote worker's apply agent hits an "enter the code we emailed you" step, it obtains that one-time code through the fleet Postgres instead of reading Gmail itself. The home box (which already has `gmail.readonly` access to the account the codes arrive at, `jonathanstallone15@gmail.com`) is the only machine that ever touches Gmail. The relay is strictly additive: if no code is available in time, the worker degrades to exactly today's behavior (the job parks/fails gracefully and is never lost).

## Why this is needed (current state, verified)

- Email verification today is **local, per machine**. A machine can auto-enter codes only if it has `~/.applypilot/gmail_credentials.json` and the flag on. The home box has this; remote machines do not.
- The centralized relay was **scaffolded but never built**: the `otp_request` table exists (`fleet/schema_v3.sql:238-246`) and `broker.request_otp()` (`fleet/broker.py:340-368`) writes a bookkeeping row and returns `{"status":"stub"}` with a comment deferring the real read to a "production build." **Nothing** reads `otp_request` or writes `matched_email_ts`/`consumed_at`; no Gmail-reading responder exists.
- The Mac's live test apply failed `failed:email_verification_required` (`prompt.py:591`) because `APPLYPILOT_ENABLE_GMAIL_MCP=0` and it has no Gmail creds.

## Decisions made (owner-confirmed)

| Decision | Choice |
|---|---|
| Code transport home→worker | **Through the fleet Postgres** (the existing encrypted Tailscale channel). Home writes the code into the request's row with a short expiry; worker reads it; code is nulled the instant it's consumed. Rejected: a new HTTP broker service (doesn't run today; extra network surface on the crash-prone home box). |
| Gmail credentials | **Home box only.** No Gmail token on any remote machine. |
| Fallback | If no code arrives within the timeout, degrade to current behavior (park/fail gracefully — never lose the job). Relay is additive. |
| Scope | Email codes + magic links only (what Gmail can supply). No SMS/authenticator. |
| Home box's own path | Unchanged — the home box keeps reading Gmail locally. |

## Architecture

```
Remote worker (Mac, offsite)             Home box (has Gmail token)
┌───────────────────────────┐           ┌────────────────────────────┐
│ apply agent hits email-    │           │ applypilot-fleet-otp-home   │
│ verification wall          │           │  (responder loop)           │
│   └─ launcher              │  Tailscale│   1. poll otp_request for   │
│      _poll_inbox_auth_hint │◄─────────►│      PENDING rows           │
│        (relay mode):       │  fleet PG │   2. scan OWN Gmail for a    │
│        1. file otp_request │           │      matching code          │
│        2. poll row for code│           │   3. write code+expiry into  │
│        3. consume (null it)│           │      the row (never logged)  │
│      → returns code= hint  │           │   4. purge expired codes     │
│      → agent retries form  │           │ build_gmail_service (local) │
└───────────────────────────┘           └────────────────────────────┘
        no Gmail token here                    Gmail token stays HERE
```

Both sides speak only to Postgres. The worker's browser/agent flow and the home box's local apply flow are unchanged.

## Components

### 1. `src/applypilot/fleet/otp_relay.py` (new) — shared PG relay logic

Pure functions over a fleet PG connection (no Gmail, no browser). Testable against the `fleet_db` fixture.

- `request_code(conn, *, worker_id, job_url, application_url, ttl_seconds=300) -> int`
  INSERTs an `otp_request` (`worker_id`, `url=application_url`, `sender_hint=<apply domain>`, `expires_at=now()+ttl`) and returns the request id.
- `poll_for_code(conn, request_id, *, timeout_seconds=300, poll_seconds=5) -> RelayCode | None`
  Loops until timeout: if the row has a non-null, unexpired, unconsumed `code`, **atomically consume and capture the pre-null value** in one statement via a CTE that reads then nulls it — e.g. `WITH picked AS (SELECT id, code, code_kind FROM otp_request WHERE id=%s AND consumed_at IS NULL AND code IS NOT NULL AND expires_at > now() FOR UPDATE) UPDATE otp_request o SET consumed_at=now(), code=NULL FROM picked WHERE o.id=picked.id RETURNING picked.code, picked.code_kind` — and return `RelayCode(value, kind)`. `FOR UPDATE` + `consumed_at IS NULL` guarantees exactly one consumer; a second poll returns `None`. Returns `None` on timeout.
- `answer_pending(conn, gmail_service, *, window_minutes=15, max_messages=25) -> int` (home side)
  Selects pending rows (`code IS NULL AND consumed_at IS NULL AND expires_at > now()`), and for each uses `inbox_auth.scan_gmail_for_auth_codes(service=gmail_service, minutes=window_minutes, max_messages=max_messages)` to find candidates. Matches a candidate to the request when: (a) sender/domain aligns with `sender_hint`, (b) the email's `received_at` is **after** `requested_at` (never a stale code), (c) highest confidence wins, and (d) the same email `message_id` is not assigned to another request in this cycle. Writes `code`, `code_kind`, `matched_email_ts`, `answered_at`, and refreshes `expires_at=now()+answered_ttl` (e.g. 120s). Returns count answered. **Never logs the code.**
- `purge_expired(conn) -> int` (home side) — nulls `code` on expired/consumed rows so no code lingers; keeps the audit row (worker_id, timestamps) for observability.
- `RelayCode` dataclass: `value: str`, `kind: str` (`"email_code" | "magic_link"`).

### 2. `src/applypilot/fleet/otp_responder_main.py` (new) — home entrypoint

Console script `applypilot-fleet-otp-home` → `main()`. A loop on the home box (runs alongside watchdog/doctor): each cycle builds the Gmail service once (`gmail_outcomes.build_gmail_service`), calls `otp_relay.answer_pending` and `otp_relay.purge_expired`, sleeps a short interval. Reads `FLEET_PG_DSN`. It only scans Gmail when at least one request is pending, so it is cheap when idle. Registers its own `worker_heartbeat` row (`role="otp_responder"`) for fleet visibility.

### 3. Launcher wiring — `src/applypilot/apply/launcher.py`

`_poll_inbox_auth_hint(job)` gains a mode branch, chosen by a new env var `APPLYPILOT_INBOX_AUTH_MODE` (`"local"` default, `"relay"` on remote workers):
- **relay:** open a fleet PG connection (`FLEET_PG_DSN`), `otp_relay.request_code(...)`, `otp_relay.poll_for_code(...)`, and format the **identical** hint string that `_format_inbox_auth_hint` produces (`code=…\nsender=…` or `magic_link=…`). Everything downstream (retry-the-form) is unchanged.
- **local:** the existing `build_gmail_service` + `watch_gmail_for_auth_code` path, byte-unchanged.

The Mac's `fleet-worker.env` sets `APPLYPILOT_INBOX_AUTH=1` and `APPLYPILOT_INBOX_AUTH_MODE=relay`. The home box leaves the default `local`.

### 4. Schema — `src/applypilot/fleet/schema_v3.sql`

Add to `otp_request` (idempotent `ALTER TABLE … ADD COLUMN IF NOT EXISTS`):
- `code TEXT` — the one-time value; null except during the seconds between answer and consume.
- `code_kind TEXT` — `'email_code'` | `'magic_link'`.
- `expires_at TIMESTAMPTZ` — request TTL; the responder and poller both honor it.
- `answered_at TIMESTAMPTZ` — when the home responder wrote the code.

Plus a partial index for the responder's pending scan: `WHERE code IS NULL AND consumed_at IS NULL`. The `fleet_worker` role already has DML on `otp_request` via the default-privileges grant (see the Mac-worker spec's `pg_roles`).

## Data flow (happy path)

1. Remote apply agent reaches the email-verification step and signals `AUTH_REQUIRED`.
2. `_poll_inbox_auth_hint` (relay mode) files an `otp_request` for this worker + apply domain, `expires_at = now()+5min`.
3. The home responder's next cycle sees the pending row, scans its Gmail, finds a code from that domain received after the request, and writes `code`/`code_kind`/`matched_email_ts`/`answered_at`.
4. The worker's `poll_for_code` reads the code, atomically consumes it (nulls `code`, sets `consumed_at`), and returns it.
5. The launcher formats the same `code=…` hint; the agent enters it and submits the form.
6. `purge_expired` clears any codes that were never consumed.

## Security

- Gmail token exclusively on the home box (`build_gmail_service` runs only there).
- The code rests in Postgres only between step 3 and step 4 (seconds), over the encrypted Tailscale tunnel, reachable only by the least-privilege `fleet_worker` role; nulled on consume; purged on expiry; **never written to any log** (relay functions never `logger` the value).
- Single-use: the consume `UPDATE … WHERE consumed_at IS NULL` guarantees one worker, one use.
- Codes are low-value, single-use, minutes-expiry email verification codes — not credentials.

## Correctness & failure modes

| Situation | Behavior |
|---|---|
| Two workers, two requests, same ATS | Each request is worker-scoped and consumed once; the responder assigns each `message_id` to at most one request per cycle and matches by nearest `received_at > requested_at`. No cross-worker mixup. |
| Stale/old code in the inbox | Not matched — the responder requires the email to have arrived **after** `requested_at`. |
| No code arrives within timeout | `poll_for_code` returns `None` → launcher returns `None` → today's fallback (park/`email_verification_required`). Job never lost. |
| Home box offline / responder not running | No answer written → worker times out → same graceful fallback. |
| Wrong-sender email | Filtered by `sender_hint` + confidence scoring (reused from `scan_gmail_for_auth_codes`). |

## Testing

PG-backed tests use the `fleet_db` fixture (`tests/conftest.py:97`); Gmail is mocked (a fake service returning `AuthEmailMatch` candidates — no real network).

- `otp_relay` cycle: `request_code` → simulate home write → `poll_for_code` consumes → **second poll returns None** (single-use) → expiry honored.
- `answer_pending` matching: correct code written for a valid candidate; **stale email (received before `requested_at`) NOT matched**; wrong-sender NOT matched; highest-confidence chosen; same `message_id` not double-assigned.
- Cross-worker isolation: two pending requests (different workers/domains) each get their own code; consuming one doesn't touch the other.
- `purge_expired` nulls expired codes, keeps the audit row.
- Launcher wiring: relay-mode `_poll_inbox_auth_hint` returns the exact hint format (relay client mocked); local mode unchanged.

## Deliverables (Python ApplyPilot repo)

1. `src/applypilot/fleet/otp_relay.py` — shared PG relay logic + `RelayCode`.
2. `src/applypilot/fleet/otp_responder_main.py` + `applypilot-fleet-otp-home` console script.
3. Schema additions to `otp_request` (+ index) in `schema_v3.sql`; conftest truncation list updated.
4. `_poll_inbox_auth_hint` relay branch + `APPLYPILOT_INBOX_AUTH_MODE` in `launcher.py`.
5. Mac `fleet-worker.env`: add `APPLYPILOT_INBOX_AUTH=1`, `APPLYPILOT_INBOX_AUTH_MODE=relay` (setup script + runbook note).
6. Tests as above.
7. Runbook note: owner runs `applypilot-fleet-otp-home` on the home box (alongside the other fleet processes); how to verify a relayed code end to end.

## Out of scope

- The unused `broker.request_otp()` HTTP stub — left as-is (superseded by the PG relay; not removed to avoid touching unrelated surface).
- SMS / authenticator-app / visible-captcha challenges (still park to the human inbox as today).
- Changing the home box's local Gmail path.
- Auto-detecting relay vs local without the explicit env var (the env var is deterministic and testable).
