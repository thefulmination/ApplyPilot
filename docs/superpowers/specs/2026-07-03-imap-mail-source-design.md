# IMAP mail source (permanent, no-OAuth Gmail read) — design

**Approved-in-principle:** 2026-07-03 (owner: "Build it — IMAP + app password"). **Problem:**
the fleet's email-verification/2FA capability (the OTP relay `otp_relay.answer_pending` + the
outcome scan) reads Gmail via an OAuth token that **dies every 7 days** — the app is in Google
"Testing" mode, and publishing to production requires full restricted-scope verification (paid
CASA assessment) that isn't worth it for a personal single-user inbox. Fix: read Gmail over
**IMAP with a Google App Password**, which never expires (until revoked), needs no consent
screen and no verification.

## Principle
Swap ONLY the fetch layer. Every message *parser* — `extract_verification_candidates`
(OTP codes), `classify_email_outcome` (interview/offer/rejection) — is content-based and stays
byte-unchanged. The OTP relay + outcome scan keep working; only "how mail is pulled" changes.

## Components

### 1. `src/applypilot/mail_source.py` (new) — the abstraction
- `@dataclass MailMessage: id: str; thread_id: str; subject: str; sender: str; date: str; body: str`
  (the normalized shape both consumers need; `date` is the RFC-2822 Date header string, as today).
- `class MailSource(Protocol): def fetch(self, *, since_days: int, max_messages: int) -> list[MailMessage]`
- `ImapMailSource` — `imaplib.IMAP4_SSL('imap.gmail.com', 993)`, `login(email, app_password)`,
  `select('INBOX', readonly=True)`, `search(None, 'SINCE <dd-Mon-yyyy>')`, fetch newest N by UID
  (`RFC822`), parse each via `email.message_from_bytes` → `MailMessage`. A shared `_normalize(raw)`
  handles: decoded Subject/From (`email.header.decode_header`), the Date header, and body text
  (walk parts, prefer `text/plain`, fall back to stripped `text/html`; honor charset). The imaplib
  object is INJECTABLE (constructor arg) so tests use a fake — no network.
- `GmailApiMailSource` — wraps the EXISTING `gmail_outcomes.build_gmail_service()` + the current
  `.users().messages().list/.get` loop, returning the same `MailMessage` shape. Preserves today's
  behavior for anyone who hasn't switched.
- `get_mail_source() -> MailSource` — factory: returns `ImapMailSource` iff an app-password config
  is present (§2), else `GmailApiMailSource`. So switching is purely additive/opt-in.

**Query handling (deliberate):** IMAP filters by DATE window only (`SINCE`), newest-first, capped
at `max_messages`. The Gmail-query keyword/category narrowing (`newer_than:Nd (verification OR …)`,
`-category:promotions`) is NOT translated to IMAP — instead the existing Python parsers do the
content filtering they already do (the OTP candidate extractor and the outcome classifier's
non-job gate are the backstops). This sidesteps fragile Gmail→IMAP query translation; the only
cost is fetching a slightly larger superset, bounded by `max_messages`.

### 2. Config (`config.py` + a gitignored secret)
- App-password secret at `~/.applypilot/gmail_app_password.json` (mirrors where `gmail_credentials.json`
  already lives; add `gmail_app_password*` to `.gitignore`): `{"email": "...", "app_password": "..."}`.
  Env overrides `APPLYPILOT_GMAIL_ADDRESS` + `APPLYPILOT_GMAIL_APP_PASSWORD` also accepted (for the
  fleet wrappers). `config.load_gmail_app_password() -> tuple[str, str] | None`. The password is
  NEVER logged and NEVER committed.

### 3. Consumer refactor (fetch → parse, parsers unchanged)
- `inbox_auth.scan_gmail_for_auth_codes(*, service=None, minutes, max_messages)` → gains a
  `messages: list[MailMessage] | None` path: the loop consumes `MailMessage` (subject/sender/body
  already extracted) and calls `extract_verification_candidates` exactly as now. Keep the `service`
  param working (back-compat) by internally converting, OR route both through a small
  `_messages_from_source_or_service(...)`. `watch_gmail_for_auth_code` (its polling caller) switches
  to `get_mail_source()`.
- `otp_relay.answer_pending(conn, gmail_service=None)` → uses `get_mail_source()` when no service is
  passed (the responder no longer needs a live Gmail API `service`).
- `otp_responder_main.py` → drops `build_gmail_service()`; calls `answer_pending(conn)` (source
  resolved internally). `launcher.py:1856` inbox-auth path → same.
- `outcome_scan._gmail_fetch` + `gmail_outcomes.scan_inbox` → fetch via `get_mail_source()`; their
  downstream already works on `{subject, sender, date, body}` dicts, so this is near drop-in.

## Error handling
- IMAP login failure (bad app password / IMAP disabled in Gmail) → raise a clear
  `MailSourceError("IMAP login failed — check the app password and that IMAP is enabled in Gmail
  settings")`, caught by the responder loop (logs + backs off, like today). `gmail_token_alive()` /
  the DeadMan `otp_relay_down` check is EXTENDED to also validate the IMAP path (a bad app password
  or IMAP-off surfaces on the console banner).
- No app-password config AND no OAuth token → `get_mail_source()` returns `GmailApiMailSource`
  which fails as today (documented); the DeadMan flags it.

## Testing
1. `_normalize`: a multipart/alternative RFC822 (plain+html), an encoded-word Subject
   (`=?UTF-8?…?=`), a base64 `text/plain` part, an HTML-only message → correct subject/sender/body.
2. `ImapMailSource.fetch` with a FAKE imaplib (injected): SINCE window, newest-first, max cap,
   returns `MailMessage`s; login failure → `MailSourceError`.
3. `get_mail_source()` picks IMAP when app-password config present, Gmail-API otherwise.
4. `scan_gmail_for_auth_codes` over a `MailMessage` list yields the same `AuthEmailMatch`es as the
   service path did (the parser is unchanged) — a regression test pinning OTP extraction.
5. `outcome_scan` with an injected mail source (no network) still classifies + persists (reuse the
   existing outcome-scan test harness).
6. DeadMan `otp_relay_down` fires when the configured IMAP login fails.

## Success criteria
Owner enables 2FA + IMAP + drops the app password in one JSON file → the OTP relay and outcome
scan read Gmail via IMAP, **never expiring**. No OAuth token, no consent screen, no verification.
Falls back to the OAuth path untouched if no app password is configured.

## Non-goals
No SMTP/sending (read-only). No Gmail-query→IMAP translation (date-window + Python filtering). No
change to the OTP-code or outcome parsers. No change to `otp_relay`'s request/answer/consume DB
protocol — only where `answer_pending` gets its messages.
