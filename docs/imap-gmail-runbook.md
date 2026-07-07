# Permanent Gmail read via IMAP + App Password — runbook

**Why:** the OAuth token that backed the fleet's email-2FA/OTP relay + outcome scan **expired
every 7 days** (Google "Testing"-mode apps kill refresh tokens weekly, and publishing needs a
paid restricted-scope verification not worth it for one inbox). This switches Gmail reading to
**IMAP with an App Password** — never expires, no consent screen, no verification. Purely
additive: with an app password present the fleet uses IMAP; without one it falls back to the old
OAuth path unchanged.

## One-time setup (owner)

1. **Enable 2-Step Verification** (required for app passwords): [myaccount.google.com/security](https://myaccount.google.com/security).
2. **Enable IMAP in Gmail:** Gmail → ⚙ Settings → **See all settings** → **Forwarding and POP/IMAP**
   → **Enable IMAP** → Save. (Without this, IMAP login succeeds but the mailbox is empty.)
3. **Generate an App Password:** [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   → name it "ApplyPilot" → Google shows a **16-character** password (with spaces; spaces are
   stripped automatically).
4. **Drop it in a gitignored file** at `C:\Users\JStal\.applypilot\gmail_app_password.json`:
   ```json
   { "email": "jonathanstallone15@gmail.com", "app_password": "the 16 chars" }
   ```
   (Alternatively set env `APPLYPILOT_GMAIL_ADDRESS` + `APPLYPILOT_GMAIL_APP_PASSWORD` — used by
   the fleet wrappers.) The password is **never logged and never committed** (`.gitignore` covers
   `gmail_app_password*`).
5. **Verify it reads:**
   ```powershell
   cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
   .\.conda-env\python.exe -c "from applypilot.mail_source import get_mail_source; src=get_mail_source(); print(type(src).__name__); print('count', len(src.fetch(since_days=1, max_messages=1)))"
   ```
   The backend should print `ImapMailSource`. This canary does not print email content. Do not
   use the wrapper's Gmail scan command as the IMAP canary unless the app-password file also exists
   under that wrapper's `APPLYPILOT_DIR`; otherwise it may fall back to OAuth and give a
   false-green result.
6. **Clean up the dead OAuth token (optional):** once the app password works, the OAuth token is
   unused — `Remove-Item "$env:USERPROFILE\.applypilot\gmail_token.json"`. (Leaving it is harmless;
   the factory prefers the app password.)

## What changed under the hood (nothing you interact with)
- A `mail_source.py` abstraction: `get_mail_source()` returns an **IMAP** backend when the app
  password is configured, else the **Gmail-API** backend (today's behavior). Both yield the same
  message shape.
- The **OTP relay** (`otp_relay.answer_pending`, the `otp_responder`), the **apply-time inbox-auth**
  (`watch_gmail_for_auth_code`), and the **outcome scan** all fetch through `get_mail_source()`.
- The OTP-code extractor and the outcome classifier are **byte-unchanged** — only *how* mail is
  pulled changed.

## Safety / monitoring
- The **DeadMan** `otp_relay_down` alert now validates the ACTIVE backend: it does a real
  `ImapMailSource.fetch(1 msg)` and raises the red console banner if the app password is wrong or
  **IMAP is disabled in Gmail** (a `MailSourceError`). A transient network blip does NOT false-alarm
  (unknown errors → no alert). So a broken mail read surfaces on your phone, not silently.
- IMAP is **read-only** (`select(readonly=True)`) — the fleet never modifies or deletes your mail.

## If it ever says "OTP relay down: Gmail token dead"
Check, in order: (1) the app password is correct in `gmail_app_password.json`; (2) IMAP is enabled
in Gmail settings; (3) the app password wasn't revoked at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords). Re-generate + repaste
if needed — but unlike OAuth, it won't expire on its own.
