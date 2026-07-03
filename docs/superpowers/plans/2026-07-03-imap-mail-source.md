# IMAP Mail Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Read Gmail over IMAP with an App Password (never expires) instead of the 7-day-expiring OAuth token, behind a mail-source abstraction so the OTP relay + outcome scan keep working with their parsers unchanged.

**Architecture:** New `mail_source.py` with a `MailMessage` shape + two backends (`ImapMailSource`, `GmailApiMailSource`) chosen by `get_mail_source()`; consumers refactored fetch→parse (parsers untouched). App password in a gitignored JSON. imaplib is stdlib — NO new dependency.

**Spec:** `docs/superpowers/specs/2026-07-03-imap-mail-source-design.md` (approved 2026-07-03).

## Global Constraints
1. Tests: `.\.conda-env\python.exe -m pytest` from repo root. Never `.venv`.
2. SHARED BRANCH: `git add` only files you touched, never `-A`/`-u`. Don't touch `launcher.py`'s fleet/apply paths beyond the one inbox-auth call, `diagnoser.py`, `fleet/doctor.py`, `fleet_sync.py`, `setup-fleet-pg-tailscale*`.
3. HARD RULE: implementers do the work themselves — never spawn subagents.
4. SECRET: the app password is NEVER logged and NEVER committed. Add `gmail_app_password*` to `.gitignore` (Task 1).
5. imaplib/email are stdlib — do NOT add a pip dependency. IMAP host `imap.gmail.com:993` SSL; mailbox selected `readonly=True` (never mutates the inbox).
6. Parsers are FROZEN: `inbox_auth.extract_verification_candidates` and `gmail_outcomes.classify_email_outcome` must not change — only what feeds them.

---

### Task 1: `MailMessage` + IMAP normalizer + `ImapMailSource` + config secret

**Files:** Create `src/applypilot/mail_source.py`; Modify `src/applypilot/config.py` (+ `.gitignore`); Test `tests/test_mail_source_imap.py`.

**Interfaces (Produces):**
```python
@dataclass
class MailMessage:
    id: str; thread_id: str; subject: str; sender: str; date: str; body: str

class MailSourceError(Exception): ...

def _normalize(uid: str, raw: bytes) -> MailMessage   # email.message_from_bytes -> fields
class ImapMailSource:
    def __init__(self, email_addr, app_password, *, imap=None): ...   # imap injectable for tests
    def fetch(self, *, since_days: int, max_messages: int) -> list[MailMessage]: ...
```
- `_normalize`: `email.message_from_bytes(raw)`; Subject/From via `email.header.decode_header` (join decoded parts, decode bytes with the stated charset or utf-8/replace); `date` = the raw `Date` header string; body = walk parts, first `text/plain` (decoded with its charset), else strip tags from `text/html`. `thread_id` = the `Message-ID` header (IMAP has no threads) falling back to `uid`.
- `ImapMailSource.fetch`: connect (or use injected `imap`), `login(email_addr, app_password)` (raise `MailSourceError` on `imaplib.IMAP4.error`), `select('INBOX', readonly=True)`, `search(None, 'SINCE', <dd-Mon-yyyy from now-since_days>)`, take the LAST `max_messages` UIDs (newest), `fetch(uid, '(RFC822)')`, `_normalize` each, `logout()` in a `finally`. Never logs the password.
- `config.load_gmail_app_password() -> tuple[str,str] | None`: env `APPLYPILOT_GMAIL_ADDRESS`+`APPLYPILOT_GMAIL_APP_PASSWORD` if both set, else parse `APP_DIR/gmail_app_password.json` (`{"email","app_password"}`), else None. `.gitignore` gains `gmail_app_password*`.

- [ ] Failing tests (fake imaplib object with canned `login`/`select`/`search`/`fetch`): (1) `_normalize` on a multipart/alternative (plain+html) → plain body; on an encoded-word Subject `=?UTF-8?B?…?=` → decoded; on a base64 text/plain part → decoded; on html-only → tag-stripped text. (2) `fetch` returns newest-N `MailMessage`s within the SINCE window. (3) login failure → `MailSourceError`. (4) `load_gmail_app_password` reads env and the JSON file, None when absent.
- [ ] Run → FAIL; implement; run → PASS.
- [ ] Commit: `git add src/applypilot/mail_source.py src/applypilot/config.py .gitignore tests/test_mail_source_imap.py && git commit -m "feat(mail): ImapMailSource + MailMessage normalizer + app-password config"`

---

### Task 2: `GmailApiMailSource` + `get_mail_source()` factory

**Files:** Modify `src/applypilot/mail_source.py`; Test `tests/test_mail_source_factory.py`.

**Interfaces:**
- `GmailApiMailSource(build_service=None)`: uses `gmail_outcomes.build_gmail_service` (or an injected builder) + the existing `.users().messages().list(userId="me", q=<derived>, maxResults=max_messages).execute()` → `.get(id, format="full")` loop, mapping each Gmail payload to `MailMessage` (reuse `gmail_outcomes._get_text_body` + a header helper for subject/from/date). `since_days` → `q=f"newer_than:{since_days}d"`.
- `get_mail_source() -> MailSource`: `ImapMailSource(*creds)` if `config.load_gmail_app_password()` is not None, else `GmailApiMailSource()`.

- [ ] Failing tests: (1) `get_mail_source` returns `ImapMailSource` when `load_gmail_app_password` is monkeypatched to creds, `GmailApiMailSource` when None. (2) `GmailApiMailSource.fetch` with an injected fake `service` (canned list/get) returns `MailMessage`s with correct subject/sender/body.
- [ ] Run → FAIL; implement; run → PASS.
- [ ] Commit: `git add src/applypilot/mail_source.py tests/test_mail_source_factory.py && git commit -m "feat(mail): GmailApiMailSource + get_mail_source() backend factory"`

---

### Task 3: Route the OTP path through `get_mail_source()`

**Files:** Modify `src/applypilot/inbox_auth.py` (`scan_gmail_for_auth_codes` :455, `watch_gmail_for_auth_code` :517), `src/applypilot/fleet/otp_relay.py` (`answer_pending`), `src/applypilot/fleet/otp_responder_main.py`, `src/applypilot/apply/launcher.py` (the inbox-auth call ~:1856 ONLY); Test `tests/test_inbox_auth_mail_source.py`.

**Interfaces:**
- `scan_gmail_for_auth_codes(*, service=None, messages=None, minutes=10, max_messages=25)`: if `messages` (a `list[MailMessage]`) is given, iterate them directly through `extract_verification_candidates` (UNCHANGED); else keep the existing `service` loop for back-compat. Build `AuthEmailMatch` from `MailMessage` fields (id/thread_id/sender/subject/date/body).
- `watch_gmail_for_auth_code(*, service=None, ...)`: when `service is None`, each poll does `msgs = get_mail_source().fetch(since_days=max(1,(minutes+1439)//1440), max_messages=max_messages)` then `scan_gmail_for_auth_codes(messages=msgs, minutes=minutes, max_messages=max_messages)`. Existing `service` path preserved.
- `otp_relay.answer_pending(conn, gmail_service=None)`: when `gmail_service is None`, fetch via `get_mail_source()` and call `scan_gmail_for_auth_codes(messages=...)`.
- `otp_responder_main.py`: drop `build_gmail_service()`; call `run_once(conn)` → `answer_pending(conn)` (source internal). `launcher.py:1856`: drop the `service=` arg (pass nothing → `watch_gmail_for_auth_code` resolves the source).

- [ ] Failing test: `scan_gmail_for_auth_codes(messages=[MailMessage(...with a 6-digit code...)])` returns an `AuthEmailMatch` with that code — the SAME result the `service` path produced (pin OTP extraction is parser-identical). Plus: `answer_pending` with an injected mail source (monkeypatch `get_mail_source`) answers a seeded `otp_request` row (reuse the otp_relay test harness).
- [ ] Run → FAIL; implement; run → PASS. Also run existing `tests/test_inbox_auth*.py` + `tests/test_otp_relay*.py` (or the otp test files present) — must stay green.
- [ ] Commit the 5 files: `git commit -m "feat(mail): route OTP relay + inbox-auth through get_mail_source()"`

---

### Task 4: Route the outcome scan through `get_mail_source()`

**Files:** Modify `src/applypilot/outcome_scan.py` (`_gmail_fetch` :111) and `src/applypilot/gmail_outcomes.py` (`scan_inbox` :933); Test: extend `tests/test_outcome_scan.py`.

**Interfaces:**
- `_gmail_fetch`: replace the `build_gmail_service` + list/get loop with `get_mail_source().fetch(since_days=days, max_messages=max_messages)` mapped to the existing `{message_id,thread_id,subject,sender,date,body}` dicts (`message_id=m.id`). Keep the `fetch_messages` injection param (tests already use it) untouched — this only changes the DEFAULT fetch.
- `scan_inbox`: same swap for its default fetch; downstream (`classify_email_outcome`, `match_email_to_job`) unchanged.

- [ ] Failing/■ test: `scan_outcomes` (or `_gmail_fetch`) with `get_mail_source` monkeypatched to an in-memory source returns the expected normalized dicts and the classifier still runs (extend the existing outcome-scan harness). Existing `tests/test_outcome_scan.py` stays green.
- [ ] Run → PASS.
- [ ] Commit: `git add src/applypilot/outcome_scan.py src/applypilot/gmail_outcomes.py tests/test_outcome_scan.py && git commit -m "feat(mail): route outcome scan through get_mail_source()"`

---

### Task 5: DeadMan `otp_relay_down` covers the IMAP path

**Files:** Modify `src/applypilot/fleet/deadman.py` (`gmail_token_alive`); Test: extend `tests/test_deadman_check.py`.

**Interfaces:**
- Rename/extend the probe to `mail_source_alive() -> bool | None` (keep `gmail_token_alive` as an alias if referenced): if `config.load_gmail_app_password()` is set → attempt an `ImapMailSource(...).fetch(since_days=1, max_messages=1)` (a real login+select); success True, `MailSourceError`/`imaplib.IMAP4.error` False, other exceptions None. Else fall back to the existing OAuth-token refresh probe. Never raises. `main()` calls the extended probe.
- The `_check_otp_relay` alert semantics are unchanged (it consumes the injected `gmail_token_ok`).

- [ ] Failing test: `mail_source_alive` with `load_gmail_app_password` monkeypatched to creds + an injected `ImapMailSource` whose login raises → returns False; login OK → True. (deadman_check's otp_relay_down behavior is already tested.)
- [ ] Run → PASS (deadman suite green).
- [ ] Commit: `git add src/applypilot/fleet/deadman.py tests/test_deadman_check.py && git commit -m "feat(deadman): otp_relay_down validates the IMAP app-password path"`

---

### Task 6: Verification + runbook

**Files:** Create `docs/imap-gmail-runbook.md`.
- [ ] Full sweep: `.\.conda-env\python.exe -m pytest tests/test_mail_source_imap.py tests/test_mail_source_factory.py tests/test_inbox_auth_mail_source.py tests/test_outcome_scan.py tests/test_deadman_check.py tests/test_gmail_outcomes.py -q` → all pass.
- [ ] Runbook: (1) owner enables 2-Step Verification; (2) generates an App Password (myaccount.google.com/apppasswords); (3) enables IMAP in Gmail (Settings → Forwarding and POP/IMAP → Enable IMAP); (4) writes `~/.applypilot/gmail_app_password.json` = `{"email":"...","app_password":"...(16 chars, spaces ok/stripped)..."}`; (5) `pip install -e .` not needed (no new entrypoints) but a fresh run picks up the module; (6) verify: `scan-gmail --days 1` now reads via IMAP; (7) note the OAuth token can be deleted — it's no longer used once the app password is present. Include the "delete `gmail_token.json`" cleanup + that the DeadMan now flags a bad app password / IMAP-off.
- [ ] Commit: `git add docs/imap-gmail-runbook.md && git commit -m "docs(imap): app-password + IMAP setup runbook"`
