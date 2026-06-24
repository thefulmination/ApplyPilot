# Inbox Auth Monitor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build an opt-in inbox authentication monitor that detects high-confidence job-application email verification codes, lets ApplyPilot auto-enter them, and records durable tracker/audit state.

**Architecture:** Add durable inbox/auth tables, a focused `inbox_auth` module for extraction, matching, Gmail polling, and challenge state, then wire it into the existing CLI and apply-agent prompt. Apply-agent code entry is performed through the existing Gmail MCP path when `--inbox-auth` is enabled; the database records challenge and inbox metadata without permanently storing raw email bodies or auth codes.

**Tech Stack:** Python 3.11, SQLite, Typer, Gmail API read-only OAuth, existing ApplyPilot application tracker, pytest.

---

## File Structure

- Create `src/applypilot/inbox_auth.py`
  - Owns verification-code extraction, magic-link extraction, confidence scoring, inbox event persistence, auth challenge persistence, stale challenge recovery, and bounded Gmail polling.
- Modify `src/applypilot/database.py`
  - Adds `inbox_events` and `auth_challenges` schema creation during DB initialization.
- Modify `src/applypilot/gmail_outcomes.py`
  - Extracts reusable Gmail OAuth service creation so inbox auth and outcome scanning share one read-only auth path.
- Modify `src/applypilot/apply/prompt.py`
  - Adds strict inbox-auth instructions when `APPLYPILOT_INBOX_AUTH=1`.
- Modify `src/applypilot/apply/launcher.py`
  - Parses inbox-auth audit lines from agent output and records challenge/inbox metadata.
- Modify `src/applypilot/cli.py`
  - Adds `inbox` command and `apply --inbox-auth`.
- Modify `run-applypilot.ps1`
  - Preserves current environment behavior; no new DB path changes beyond existing guard.
- Create tests:
  - `tests/test_inbox_auth_schema.py`
  - `tests/test_inbox_auth_extraction.py`
  - `tests/test_inbox_auth_persistence.py`
  - `tests/test_inbox_auth_gmail.py`
  - `tests/test_inbox_auth_cli.py`
  - `tests/test_apply_inbox_auth.py`

---

### Task 1: Add Inbox Auth Schema

**Files:**
- Modify: `src/applypilot/database.py`
- Test: `tests/test_inbox_auth_schema.py`

- [ ] **Step 1: Write the failing schema tests**

Create `tests/test_inbox_auth_schema.py`:

```python
from __future__ import annotations

from applypilot import database


def test_inbox_auth_tables_created(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }

    assert "inbox_events" in tables
    assert "auth_challenges" in tables


def test_inbox_events_message_id_is_unique(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")

    conn.execute(
        """
        INSERT INTO inbox_events (
            message_id, sender, subject, event_type, confidence, created_at
        )
        VALUES ('msg-1', 'no-reply@greenhouse.io', 'Verify', 'auth_code', 'high', '2026-06-24T00:00:00+00:00')
        """
    )
    conn.commit()

    try:
        conn.execute(
            """
            INSERT INTO inbox_events (
                message_id, sender, subject, event_type, confidence, created_at
            )
            VALUES ('msg-1', 'no-reply@greenhouse.io', 'Verify again', 'auth_code', 'high', '2026-06-24T00:00:01+00:00')
            """
        )
    except Exception as exc:
        assert "UNIQUE" in str(exc).upper()
    else:
        raise AssertionError("duplicate message_id should fail")


def test_auth_challenge_status_index_exists(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")

    indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(auth_challenges)").fetchall()
    }

    assert "idx_auth_challenges_status" in indexes
    assert "idx_auth_challenges_job_url" in indexes
```

- [ ] **Step 2: Run the schema tests and verify they fail**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_schema.py -q
```

Expected: tests fail because `inbox_events` and `auth_challenges` do not exist.

- [ ] **Step 3: Add schema creation**

In `src/applypilot/database.py`, add this function after `ensure_application_tables`:

```python
def ensure_inbox_auth_tables(conn: sqlite3.Connection | None = None) -> None:
    """Create durable inbox/auth-code tracking tables."""
    if conn is None:
        conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS inbox_events (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id            TEXT NOT NULL UNIQUE,
            thread_id             TEXT,
            sender                TEXT,
            sender_domain         TEXT,
            subject               TEXT,
            received_at           TEXT,
            event_type            TEXT NOT NULL,
            confidence            TEXT NOT NULL,
            matched_job_url       TEXT,
            matched_company       TEXT,
            matched_method        TEXT,
            snippet               TEXT,
            created_at            TEXT NOT NULL,
            FOREIGN KEY(matched_job_url) REFERENCES jobs(url)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS auth_challenges (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            job_url               TEXT NOT NULL,
            application_url       TEXT,
            provider              TEXT,
            challenge_type        TEXT NOT NULL,
            status                TEXT NOT NULL,
            requested_at          TEXT NOT NULL,
            expires_at            TEXT NOT NULL,
            resolved_at           TEXT,
            inbox_event_id        INTEGER,
            attempt_count         INTEGER NOT NULL DEFAULT 0,
            last_error            TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            FOREIGN KEY(job_url) REFERENCES jobs(url),
            FOREIGN KEY(inbox_event_id) REFERENCES inbox_events(id)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_events_job_url ON inbox_events(matched_job_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inbox_events_received_at ON inbox_events(received_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_challenges_status ON auth_challenges(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_challenges_job_url ON auth_challenges(job_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_auth_challenges_expires_at ON auth_challenges(expires_at)")
    conn.commit()
```

In `init_db`, call it after `ensure_application_tables(conn)`:

```python
ensure_application_tables(conn)
ensure_inbox_auth_tables(conn)
ensure_pipeline_tables(conn)
```

- [ ] **Step 4: Run the schema tests and verify they pass**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_schema.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit schema changes**

```powershell
git add src\applypilot\database.py tests\test_inbox_auth_schema.py
git commit -m "Add inbox auth database schema"
```

---

### Task 2: Add Deterministic Code and Link Extraction

**Files:**
- Create: `src/applypilot/inbox_auth.py`
- Test: `tests/test_inbox_auth_extraction.py`

- [ ] **Step 1: Write failing extraction tests**

Create `tests/test_inbox_auth_extraction.py`:

```python
from __future__ import annotations

from applypilot.inbox_auth import (
    extract_verification_candidates,
    is_google_security_prompt,
)


def test_extracts_greenhouse_numeric_code():
    candidates = extract_verification_candidates(
        subject="Your Greenhouse verification code",
        body="Use verification code 839214 to continue your application.",
        sender="no-reply@greenhouse.io",
    )

    assert candidates
    assert candidates[0].kind == "code"
    assert candidates[0].value == "839214"
    assert candidates[0].confidence == "high"


def test_extracts_magic_link():
    candidates = extract_verification_candidates(
        subject="Confirm your email",
        body="Click https://boards.greenhouse.io/verify?token=abc123 to continue.",
        sender="no-reply@greenhouse.io",
    )

    assert candidates
    assert candidates[0].kind == "magic_link"
    assert candidates[0].value.startswith("https://boards.greenhouse.io/verify")


def test_ignores_year_and_phone_false_positives():
    candidates = extract_verification_candidates(
        subject="Thanks for applying",
        body="Founded in 2019. Call 415-747-2735 if needed. Job ID 123456789.",
        sender="recruiting@example.com",
    )

    assert candidates == []


def test_google_security_prompt_is_never_auto_handled():
    assert is_google_security_prompt(
        subject="Security alert",
        body="A new sign-in on Windows requires your passkey.",
        sender="no-reply@accounts.google.com",
    )
```

- [ ] **Step 2: Run extraction tests and verify they fail**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_extraction.py -q
```

Expected: import failure because `applypilot.inbox_auth` does not exist.

- [ ] **Step 3: Add extraction module**

Create `src/applypilot/inbox_auth.py` with:

```python
"""Inbox authentication helpers for application email verification."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import urlparse

from applypilot.database import get_connection


Confidence = Literal["low", "medium", "high"]
CandidateKind = Literal["code", "magic_link"]

KNOWN_ATS_DOMAINS = {
    "greenhouse.io",
    "boards.greenhouse.io",
    "myworkday.com",
    "myworkdayjobs.com",
    "lever.co",
    "ashbyhq.com",
    "icims.com",
    "smartrecruiters.com",
    "workable.com",
    "taleo.net",
    "oraclecloud.com",
}

VERIFY_WORDS = (
    "verification", "verify", "code", "one-time", "one time", "otp",
    "confirm", "confirmation", "magic link", "continue your application",
)


@dataclass(frozen=True)
class VerificationCandidate:
    kind: CandidateKind
    value: str
    confidence: Confidence
    reasons: tuple[str, ...]


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def sender_domain(sender: str) -> str | None:
    match = re.search(r"@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", sender or "")
    if not match:
        return None
    domain = match.group(1).lower().strip(".")
    return domain[4:] if domain.startswith("www.") else domain


def url_domain(url: str) -> str | None:
    try:
        host = urlparse(url).hostname or ""
    except ValueError:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def is_known_ats_domain(domain: str | None) -> bool:
    if not domain:
        return False
    return any(domain == d or domain.endswith("." + d) for d in KNOWN_ATS_DOMAINS)


def is_google_security_prompt(subject: str, body: str, sender: str) -> bool:
    text = f"{subject}\n{body}\n{sender}".lower()
    return (
        "accounts.google.com" in text
        or "security alert" in text
        or "passkey" in text
        or "2-step verification" in text
        or "2fa" in text
        or "suspicious" in text
        or "new sign-in" in text
    )


def _has_verify_context(text: str, start: int, end: int) -> bool:
    window = text[max(0, start - 80): min(len(text), end + 80)].lower()
    return any(word in window for word in VERIFY_WORDS)


def _confidence(reasons: list[str]) -> Confidence:
    score = 0
    if "known_ats_sender" in reasons:
        score += 2
    if "verification_language" in reasons:
        score += 2
    if "single_candidate" in reasons:
        score += 1
    if score >= 4:
        return "high"
    if score >= 2:
        return "medium"
    return "low"


def extract_verification_candidates(subject: str, body: str, sender: str) -> list[VerificationCandidate]:
    if is_google_security_prompt(subject, body, sender):
        return []

    text = f"{subject}\n{body}"
    domain = sender_domain(sender)
    reasons_base: list[str] = []
    if is_known_ats_domain(domain):
        reasons_base.append("known_ats_sender")
    if any(word in text.lower() for word in VERIFY_WORDS):
        reasons_base.append("verification_language")

    raw: list[tuple[CandidateKind, str, list[str]]] = []

    for match in re.finditer(r"(?<![\d-])\b\d{4,8}\b(?![\d-])", text):
        value = match.group(0)
        if value.startswith(("19", "20")) and len(value) == 4:
            continue
        if not _has_verify_context(text, match.start(), match.end()):
            continue
        raw.append(("code", value, list(reasons_base)))

    for match in re.finditer(r"https?://[^\s)>\"]+", text):
        value = match.group(0).rstrip(".,")
        lowered = value.lower()
        if any(token in lowered for token in ("verify", "confirm", "token", "magic", "continue")):
            reasons = list(reasons_base)
            if is_known_ats_domain(url_domain(value)):
                reasons.append("known_ats_link")
            raw.append(("magic_link", value, reasons))

    if len(raw) == 1:
        raw[0][2].append("single_candidate")

    candidates = [
        VerificationCandidate(kind=kind, value=value, confidence=_confidence(reasons), reasons=tuple(reasons))
        for kind, value, reasons in raw
    ]
    rank = {"high": 2, "medium": 1, "low": 0}
    return sorted(candidates, key=lambda c: (rank[c.confidence], c.kind == "code"), reverse=True)
```

- [ ] **Step 4: Run extraction tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_extraction.py -q
```

Expected: `4 passed`.

- [ ] **Step 5: Commit extraction code**

```powershell
git add src\applypilot\inbox_auth.py tests\test_inbox_auth_extraction.py
git commit -m "Add inbox auth code extraction"
```

---

### Task 3: Add Challenge and Inbox Event Persistence

**Files:**
- Modify: `src/applypilot/inbox_auth.py`
- Test: `tests/test_inbox_auth_persistence.py`

- [ ] **Step 1: Write failing persistence tests**

Create `tests/test_inbox_auth_persistence.py`:

```python
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from applypilot import database
from applypilot import inbox_auth


def test_record_inbox_event_is_idempotent(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)

    first = inbox_auth.record_inbox_event(
        message_id="msg-1",
        thread_id="thread-1",
        sender="no-reply@greenhouse.io",
        subject="Verification code",
        event_type="auth_code",
        confidence="high",
        snippet="Use code 839214",
    )
    second = inbox_auth.record_inbox_event(
        message_id="msg-1",
        thread_id="thread-1",
        sender="no-reply@greenhouse.io",
        subject="Verification code",
        event_type="auth_code",
        confidence="high",
        snippet="Use code 839214",
    )

    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM inbox_events").fetchone()[0] == 1


def test_expire_stale_challenges(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    now = datetime.now(timezone.utc)
    conn.execute(
        """
        INSERT INTO jobs (url, title, site, discovered_at)
        VALUES ('https://jobs.example/1', 'Role', 'Example', ?)
        """,
        (now.isoformat(),),
    )
    challenge_id = inbox_auth.create_auth_challenge(
        job_url="https://jobs.example/1",
        application_url="https://jobs.example/1/apply",
        provider="greenhouse",
        challenge_type="email_code",
        ttl_seconds=1,
    )
    conn.execute(
        "UPDATE auth_challenges SET status='watching', expires_at=? WHERE id=?",
        ((now - timedelta(seconds=5)).isoformat(), challenge_id),
    )
    conn.commit()

    expired = inbox_auth.expire_stale_challenges()

    assert expired == 1
    status = conn.execute("SELECT status FROM auth_challenges WHERE id=?", (challenge_id,)).fetchone()[0]
    assert status == "expired"


def test_resolve_challenge_requires_pending_or_watching(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO jobs (url, title, site, discovered_at) VALUES ('u', 'T', 'S', ?)", (now,))
    challenge_id = inbox_auth.create_auth_challenge(
        job_url="u",
        application_url="u",
        provider="greenhouse",
        challenge_type="email_code",
    )
    event_id = inbox_auth.record_inbox_event(
        message_id="msg-2",
        sender="no-reply@greenhouse.io",
        subject="Code",
        event_type="auth_code",
        confidence="high",
    )

    assert inbox_auth.resolve_auth_challenge(challenge_id, event_id) is True
    assert inbox_auth.resolve_auth_challenge(challenge_id, event_id) is False
```

- [ ] **Step 2: Run persistence tests and verify they fail**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_persistence.py -q
```

Expected: missing functions fail.

- [ ] **Step 3: Add persistence functions**

Append to `src/applypilot/inbox_auth.py`:

```python
def create_auth_challenge(
    *,
    job_url: str,
    application_url: str | None,
    provider: str | None,
    challenge_type: str = "email_code",
    ttl_seconds: int = 300,
) -> int:
    conn = get_connection()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(seconds=ttl_seconds)
    cur = conn.execute(
        """
        INSERT INTO auth_challenges (
            job_url, application_url, provider, challenge_type, status,
            requested_at, expires_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)
        """,
        (
            job_url,
            application_url,
            provider,
            challenge_type,
            now.isoformat(),
            expires_at.isoformat(),
            now.isoformat(),
            now.isoformat(),
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def record_inbox_event(
    *,
    message_id: str,
    thread_id: str | None = None,
    sender: str | None = None,
    subject: str | None = None,
    event_type: str,
    confidence: Confidence,
    matched_job_url: str | None = None,
    matched_company: str | None = None,
    matched_method: str | None = None,
    snippet: str | None = None,
    received_at: str | None = None,
) -> int:
    conn = get_connection()
    now = now_utc()
    conn.execute(
        """
        INSERT OR IGNORE INTO inbox_events (
            message_id, thread_id, sender, sender_domain, subject, received_at,
            event_type, confidence, matched_job_url, matched_company,
            matched_method, snippet, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            message_id,
            thread_id,
            sender,
            sender_domain(sender or ""),
            subject,
            received_at,
            event_type,
            confidence,
            matched_job_url,
            matched_company,
            matched_method,
            snippet,
            now,
        ),
    )
    row = conn.execute(
        "SELECT id FROM inbox_events WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    conn.commit()
    return int(row[0])


def expire_stale_challenges() -> int:
    conn = get_connection()
    now = now_utc()
    cur = conn.execute(
        """
        UPDATE auth_challenges
           SET status = 'expired',
               updated_at = ?,
               last_error = COALESCE(last_error, 'challenge expired before code arrived')
         WHERE status IN ('pending', 'watching')
           AND expires_at < ?
        """,
        (now, now),
    )
    conn.commit()
    return int(cur.rowcount)


def resolve_auth_challenge(challenge_id: int, inbox_event_id: int) -> bool:
    conn = get_connection()
    now = now_utc()
    cur = conn.execute(
        """
        UPDATE auth_challenges
           SET status = 'resolved',
               inbox_event_id = ?,
               resolved_at = ?,
               updated_at = ?
         WHERE id = ?
           AND status IN ('pending', 'watching')
        """,
        (inbox_event_id, now, now, challenge_id),
    )
    conn.commit()
    return cur.rowcount == 1
```

- [ ] **Step 4: Run persistence tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_persistence.py -q
```

Expected: `3 passed`.

- [ ] **Step 5: Commit persistence helpers**

```powershell
git add src\applypilot\inbox_auth.py tests\test_inbox_auth_persistence.py
git commit -m "Add inbox auth persistence helpers"
```

---

### Task 4: Reuse Gmail Read-Only OAuth and Add Bounded Watcher

**Files:**
- Modify: `src/applypilot/gmail_outcomes.py`
- Modify: `src/applypilot/inbox_auth.py`
- Test: `tests/test_inbox_auth_gmail.py`

- [ ] **Step 1: Write failing Gmail watcher tests**

Create `tests/test_inbox_auth_gmail.py`:

```python
from __future__ import annotations

from applypilot import inbox_auth


class FakeMessages:
    def __init__(self, messages):
        self.messages = messages
        self.fetch_id = None

    def list(self, **kwargs):
        return self

    def get(self, **kwargs):
        self.fetch_id = kwargs["id"]
        return self

    def execute(self):
        if self.fetch_id:
            msg = self.messages[self.fetch_id]
            self.fetch_id = None
            return msg
        return {"messages": [{"id": key, "threadId": self.messages[key].get("threadId", key)} for key in self.messages]}


class FakeUsers:
    def __init__(self, messages):
        self._messages = FakeMessages(messages)

    def messages(self):
        return self._messages


class FakeGmail:
    def __init__(self, messages):
        self._users = FakeUsers(messages)

    def users(self):
        return self._users


def gmail_message(subject, sender, body):
    return {
        "id": "msg-1",
        "threadId": "thread-1",
        "payload": {
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
                {"name": "Date", "value": "Wed, 24 Jun 2026 10:00:00 -0400"},
            ],
            "mimeType": "text/plain",
            "body": {"data": inbox_auth._b64url(body)},
        },
    }


def test_scan_gmail_for_auth_codes_returns_high_confidence_code(monkeypatch):
    service = FakeGmail({
        "msg-1": gmail_message(
            "Your Greenhouse verification code",
            "no-reply@greenhouse.io",
            "Use verification code 839214 to continue your application.",
        )
    })

    results = inbox_auth.scan_gmail_for_auth_codes(service=service, minutes=10, max_messages=10)

    assert len(results) == 1
    assert results[0].message_id == "msg-1"
    assert results[0].candidate.value == "839214"
    assert results[0].candidate.confidence == "high"


def test_watch_retries_transient_gmail_error(monkeypatch):
    calls = {"count": 0}

    def flaky_scan(**kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary Gmail failure")
        return []

    monkeypatch.setattr(inbox_auth, "scan_gmail_for_auth_codes", flaky_scan)
    monkeypatch.setattr(inbox_auth.time, "sleep", lambda _: None)

    results = inbox_auth.watch_gmail_for_auth_code(service=object(), timeout_seconds=1, poll_seconds=0, max_errors=2)

    assert results is None
    assert calls["count"] >= 2
```

- [ ] **Step 2: Run Gmail watcher tests and verify they fail**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_gmail.py -q
```

Expected: missing functions fail.

- [ ] **Step 3: Extract Gmail service builder**

In `src/applypilot/gmail_outcomes.py`, create a reusable function above `scan_inbox`:

```python
def build_gmail_service(credentials_path: Path | None = None, token_path: Path | None = None):
    """Build a read-only Gmail API service using ApplyPilot's OAuth files."""
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise ImportError(
            "Gmail scanning requires optional dependencies:\n"
            "  pip install google-auth-oauthlib google-api-python-client\n"
            "Then set up credentials — run `applypilot scan-gmail --help`."
        ) from exc

    from applypilot.config import APP_DIR

    creds_path = credentials_path or (APP_DIR / "gmail_credentials.json")
    tok_path = token_path or (APP_DIR / "gmail_token.json")

    if not creds_path.exists():
        raise FileNotFoundError(
            f"Gmail credentials not found: {creds_path}\n\n"
            "One-time setup:\n"
            "  1. console.cloud.google.com -> APIs & Services -> Enable APIs -> Gmail API\n"
            "  2. Credentials -> Create OAuth 2.0 Client ID (Desktop app)\n"
            "  3. Download JSON -> save as:\n"
            f"     {creds_path}"
        )

    creds = None
    if tok_path.exists():
        creds = Credentials.from_authorized_user_file(str(tok_path), GMAIL_SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        tok_path.write_text(creds.to_json(), encoding="utf-8")
        log.info("Gmail token saved to %s", tok_path)

    return build("gmail", "v1", credentials=creds)
```

Then replace the duplicated auth block in `scan_inbox` with:

```python
service = build_gmail_service(credentials_path=credentials_path, token_path=token_path)
```

- [ ] **Step 4: Add Gmail auth scanning helpers**

Append to `src/applypilot/inbox_auth.py`:

```python
import base64
import time
from dataclasses import field


@dataclass(frozen=True)
class AuthEmailMatch:
    message_id: str
    thread_id: str | None
    sender: str
    subject: str
    received_at: str | None
    snippet: str
    candidate: VerificationCandidate
    matched_job_url: str | None = None
    matched_company: str | None = None
    matched_method: str | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_b64url(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8", errors="replace")


def _payload_text(payload: dict) -> str:
    body = payload.get("body", {})
    if body.get("data"):
        return _decode_b64url(body["data"])
    parts = payload.get("parts") or []
    for part in parts:
        if part.get("mimeType") in {"text/plain", "text/html"}:
            text = _payload_text(part)
            if text:
                return text
    return ""


def _headers(payload: dict) -> dict[str, str]:
    return {
        h.get("name", "").lower(): h.get("value", "")
        for h in payload.get("headers", [])
    }


def scan_gmail_for_auth_codes(*, service, minutes: int = 10, max_messages: int = 25) -> list[AuthEmailMatch]:
    query = (
        f'newer_than:1d (verification OR verify OR code OR "one-time" '
        f'OR "one time" OR "confirm your email" OR "magic link")'
    )
    resp = service.users().messages().list(userId="me", q=query, maxResults=max_messages).execute()
    matches: list[AuthEmailMatch] = []
    for ref in resp.get("messages", []):
        msg = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
        payload = msg.get("payload", {})
        headers = _headers(payload)
        subject = headers.get("subject", "")
        sender = headers.get("from", "")
        received_at = headers.get("date")
        body = _payload_text(payload)
        candidates = extract_verification_candidates(subject, body, sender)
        for candidate in candidates:
            if candidate.confidence != "high":
                continue
            matches.append(AuthEmailMatch(
                message_id=ref["id"],
                thread_id=ref.get("threadId"),
                sender=sender,
                subject=subject,
                received_at=received_at,
                snippet=body[:200].replace("\n", " ").strip(),
                candidate=candidate,
                reasons=candidate.reasons,
            ))
    return matches


def watch_gmail_for_auth_code(
    *,
    service,
    timeout_seconds: int = 300,
    poll_seconds: int = 5,
    max_errors: int = 3,
) -> AuthEmailMatch | None:
    deadline = time.monotonic() + timeout_seconds
    errors = 0
    while time.monotonic() < deadline:
        try:
            matches = scan_gmail_for_auth_codes(service=service)
            if matches:
                return matches[0]
            errors = 0
        except Exception:
            errors += 1
            if errors >= max_errors:
                return None
        time.sleep(max(0, poll_seconds))
    return None
```

- [ ] **Step 5: Run Gmail watcher tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_gmail.py tests\test_gmail_outcomes.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit Gmail watcher**

```powershell
git add src\applypilot\gmail_outcomes.py src\applypilot\inbox_auth.py tests\test_inbox_auth_gmail.py
git commit -m "Add Gmail inbox auth watcher"
```

---

### Task 5: Add Inbox CLI

**Files:**
- Modify: `src/applypilot/cli.py`
- Test: `tests/test_inbox_auth_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Create `tests/test_inbox_auth_cli.py`:

```python
from __future__ import annotations

from typer.testing import CliRunner

from applypilot.cli import app


runner = CliRunner()


def test_inbox_help_mentions_auth_codes():
    result = runner.invoke(app, ["inbox", "--help"])

    assert result.exit_code == 0
    assert "--auth-codes" in result.output
    assert "--watch" in result.output


def test_apply_help_mentions_inbox_auth():
    result = runner.invoke(app, ["apply", "--help"])

    assert result.exit_code == 0
    assert "--inbox-auth" in result.output
```

- [ ] **Step 2: Run CLI tests and verify they fail**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_cli.py -q
```

Expected: `inbox` command and `--inbox-auth` are missing.

- [ ] **Step 3: Add `inbox` command**

In `src/applypilot/cli.py`, add this command near `scan-gmail`:

```python
@app.command("inbox")
def inbox_command(
    scan: bool = typer.Option(False, "--scan", help="Scan Gmail for recent application auth messages."),
    watch: bool = typer.Option(False, "--watch", help="Poll Gmail for auth messages for a bounded window."),
    auth_codes: bool = typer.Option(False, "--auth-codes", help="Show detected email verification codes and magic links."),
    minutes: int = typer.Option(5, "--minutes", help="Watch window in minutes."),
    credentials: Optional[Path] = typer.Option(None, "--credentials", help="Path to gmail_credentials.json."),
    auth_required: bool = typer.Option(False, "--auth-required", help="Show unresolved auth challenges."),
) -> None:
    """Inspect inbox authentication events and auth-code challenges."""
    _bootstrap()

    from applypilot import inbox_auth

    if auth_required:
        rows = inbox_auth.list_auth_challenges(statuses={"pending", "watching", "expired", "manual_required", "failed"})
        table = Table(title="Inbox Auth Challenges", show_header=True, header_style="bold cyan")
        table.add_column("Status")
        table.add_column("Provider")
        table.add_column("Type")
        table.add_column("Job URL", max_width=70)
        table.add_column("Expires")
        table.add_column("Last Error", max_width=40)
        for row in rows:
            table.add_row(
                str(row.get("status") or ""),
                str(row.get("provider") or ""),
                str(row.get("challenge_type") or ""),
                str(row.get("job_url") or ""),
                str(row.get("expires_at") or ""),
                str(row.get("last_error") or ""),
            )
        console.print(table)
        return

    if not (scan or watch):
        console.print("[yellow]Choose --scan, --watch, or --auth-required.[/yellow]")
        raise typer.Exit(1)

    try:
        from applypilot.gmail_outcomes import build_gmail_service
        service = build_gmail_service(credentials_path=credentials)
    except FileNotFoundError as exc:
        console.print(f"[red]Setup required:[/red]\n{exc}")
        raise typer.Exit(1)
    except ImportError as exc:
        console.print(f"[red]Missing dependencies:[/red] {exc}")
        raise typer.Exit(1)

    if watch:
        match = inbox_auth.watch_gmail_for_auth_code(service=service, timeout_seconds=minutes * 60)
        matches = [match] if match else []
    else:
        matches = inbox_auth.scan_gmail_for_auth_codes(service=service, minutes=minutes)

    table = Table(title="Inbox Auth Codes", show_header=True, header_style="bold cyan")
    table.add_column("Kind")
    table.add_column("Confidence")
    table.add_column("Sender")
    table.add_column("Subject", max_width=50)
    table.add_column("Value", max_width=60)
    for match in matches:
        value = match.candidate.value if auth_codes else "[hidden; use --auth-codes]"
        table.add_row(match.candidate.kind, match.candidate.confidence, match.sender, match.subject, value)
    console.print(table)
```

- [ ] **Step 4: Add `list_auth_challenges` helper**

Append to `src/applypilot/inbox_auth.py`:

```python
def list_auth_challenges(statuses: set[str] | None = None, limit: int = 50) -> list[dict]:
    conn = get_connection()
    params: list[str | int] = []
    where = "1=1"
    if statuses:
        placeholders = ",".join("?" * len(statuses))
        where = f"status IN ({placeholders})"
        params.extend(sorted(statuses))
    sql = f"""
        SELECT *
          FROM auth_challenges
         WHERE {where}
         ORDER BY requested_at DESC
    """
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]
```

- [ ] **Step 5: Add `--inbox-auth` option skeleton**

In `apply_command` options in `src/applypilot/cli.py`, add:

```python
    inbox_auth: bool = typer.Option(False, "--inbox-auth", help="Enable read-only Gmail auth-code handling during apply."),
```

Before `apply_main(...)`, add:

```python
    if inbox_auth:
        os.environ["APPLYPILOT_INBOX_AUTH"] = "1"
        os.environ["APPLYPILOT_ENABLE_GMAIL_MCP"] = "1"
```

- [ ] **Step 6: Run CLI tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_cli.py -q
```

Expected: `2 passed`.

- [ ] **Step 7: Commit CLI**

```powershell
git add src\applypilot\cli.py src\applypilot\inbox_auth.py tests\test_inbox_auth_cli.py
git commit -m "Add inbox auth CLI"
```

---

### Task 6: Wire Apply Prompt and Launcher Audit

**Files:**
- Modify: `src/applypilot/apply/prompt.py`
- Modify: `src/applypilot/apply/launcher.py`
- Test: `tests/test_apply_inbox_auth.py`

- [ ] **Step 1: Write failing apply integration tests**

Create `tests/test_apply_inbox_auth.py`:

```python
from __future__ import annotations

from applypilot.apply import prompt
from applypilot.apply import launcher


def test_prompt_enables_inbox_auth_when_env_set(monkeypatch, tmp_path):
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")
    monkeypatch.setenv("APPLYPILOT_ENABLE_GMAIL_MCP", "1")
    monkeypatch.setattr(prompt.config, "resolve_resume_stem", lambda _: str(tmp_path / "resume"))
    monkeypatch.setattr(prompt.config, "load_profile", lambda: {
        "personal": {
            "full_name": "Jonathan Stallone",
            "preferred_name": "Jonathan",
            "email": "jonathan@example.com",
            "phone": "4157472735",
            "city": "San Francisco",
        },
        "work_authorization": {},
        "preferences": {},
    })
    monkeypatch.setattr(prompt.config, "load_search_config", lambda: {"locations": []})
    monkeypatch.setattr(prompt.config, "load_blocked_sso", lambda: ["accounts.google.com"])
    (tmp_path / "resume.pdf").write_bytes(b"%PDF-1.4")

    text = prompt.build_prompt(
        {
            "url": "https://jobs.greenhouse.io/acme/1",
            "application_url": "https://jobs.greenhouse.io/acme/1",
            "title": "Chief of Staff",
            "site": "Acme",
            "fit_score": 10,
            "tailored_resume_path": str(tmp_path / "resume.txt"),
        },
        tailored_resume="Resume text",
    )

    assert "INBOX AUTH ENABLED" in text
    assert "INBOX_AUTH_USED" in text
    assert "Never use Gmail for Google account security" in text


def test_parse_inbox_auth_line():
    parsed = launcher._parse_inbox_auth_line(
        "INBOX_AUTH_USED provider=greenhouse kind=code confidence=high message_id=msg-1 subject=\"Your code\""
    )

    assert parsed == {
        "provider": "greenhouse",
        "kind": "code",
        "confidence": "high",
        "message_id": "msg-1",
        "subject": "Your code",
    }
```

- [ ] **Step 2: Run apply integration tests and verify they fail**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_apply_inbox_auth.py -q
```

Expected: missing prompt text and parser fail.

- [ ] **Step 3: Add inbox-auth prompt section**

In `src/applypilot/apply/prompt.py`, near the Gmail instruction setup, add:

```python
    inbox_auth_enabled = os.environ.get("APPLYPILOT_INBOX_AUTH", "").lower() in {"1", "true", "yes", "on"}
    if inbox_auth_enabled and gmail_tools_enabled:
        inbox_auth_section = f"""
== INBOX AUTH ENABLED ==
If an application page asks for an EMAIL verification code or application magic link:
- Use Gmail search/read tools to find the newest relevant message from the application provider.
- Only use messages tied to this active application, this company, or the current ATS domain.
- High-confidence providers include Greenhouse, Workday, Lever, Ashby, iCIMS, SmartRecruiters, Workable, Oracle/Taleo.
- Never use Gmail for Google account security, password reset, passkey, authenticator, SMS, suspicious-login, or recovery prompts.
- If exactly one high-confidence code or magic link is found, enter it and continue the application.
- If multiple plausible codes exist, or the message is unrelated, output RESULT:AUTH_REQUIRED and stop.
- After using a code or link, include one audit line before the final RESULT line:
  INBOX_AUTH_USED provider=<provider> kind=<code|magic_link> confidence=high message_id=<gmail_message_id> subject="<email subject>"
"""
    elif inbox_auth_enabled:
        inbox_auth_section = """
== INBOX AUTH REQUESTED BUT GMAIL TOOLS UNAVAILABLE ==
If an application page asks for email verification, output RESULT:AUTH_REQUIRED.
"""
    else:
        inbox_auth_section = ""
```

Add `{inbox_auth_section}` before `== STEP-BY-STEP ==`.

- [ ] **Step 4: Add launcher parser and recorder hook**

In `src/applypilot/apply/launcher.py`, add near helper functions:

```python
def _parse_inbox_auth_line(line: str) -> dict[str, str] | None:
    if not line.startswith("INBOX_AUTH_USED "):
        return None
    import shlex
    parts = shlex.split(line[len("INBOX_AUTH_USED "):])
    parsed: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        parsed[key.strip()] = value.strip()
    return parsed or None
```

In the final-output parsing path after collecting `final_result_text`, scan lines:

```python
            inbox_auth_lines = [
                _parse_inbox_auth_line(line.strip())
                for line in "\n".join(final_result_text).splitlines()
            ]
            inbox_auth_lines = [line for line in inbox_auth_lines if line]
            for inbox_auth in inbox_auth_lines:
                try:
                    from applypilot.inbox_auth import record_inbox_event
                    record_inbox_event(
                        message_id=inbox_auth.get("message_id") or f"agent-{job['url']}-{time.time()}",
                        sender=None,
                        subject=inbox_auth.get("subject"),
                        event_type="auth_code" if inbox_auth.get("kind") == "code" else "magic_link",
                        confidence=inbox_auth.get("confidence", "medium"),
                        matched_job_url=job["url"],
                        matched_company=job.get("company") or job.get("site"),
                        matched_method="apply_agent_audit",
                        snippet=f"provider={inbox_auth.get('provider')} kind={inbox_auth.get('kind')}",
                    )
                except Exception as exc:
                    logger.warning("Could not record inbox auth audit for %s: %s", job.get("url"), exc)
```

- [ ] **Step 5: Run apply integration tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_apply_inbox_auth.py -q
```

Expected: `2 passed`.

- [ ] **Step 6: Commit apply integration**

```powershell
git add src\applypilot\apply\prompt.py src\applypilot\apply\launcher.py tests\test_apply_inbox_auth.py
git commit -m "Wire inbox auth into apply agent"
```

---

### Task 7: Add Resilience Views and Expiry Recovery

**Files:**
- Modify: `src/applypilot/inbox_auth.py`
- Modify: `src/applypilot/cli.py`
- Test: `tests/test_inbox_auth_persistence.py`

- [ ] **Step 1: Extend persistence tests for fail-closed recovery**

Append to `tests/test_inbox_auth_persistence.py`:

```python
def test_mark_challenge_manual_required_is_conditional(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(inbox_auth, "get_connection", lambda: conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO jobs (url, title, site, discovered_at) VALUES ('u2', 'T', 'S', ?)", (now,))
    challenge_id = inbox_auth.create_auth_challenge(
        job_url="u2",
        application_url="u2",
        provider="greenhouse",
        challenge_type="email_code",
    )

    assert inbox_auth.mark_challenge_manual_required(challenge_id, "gmail unavailable") is True
    assert inbox_auth.mark_challenge_manual_required(challenge_id, "second write") is False
```

- [ ] **Step 2: Run the extended persistence tests and verify they fail**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_persistence.py -q
```

Expected: missing `mark_challenge_manual_required`.

- [ ] **Step 3: Add fail-closed challenge status helper**

Append to `src/applypilot/inbox_auth.py`:

```python
def mark_challenge_manual_required(challenge_id: int, reason: str) -> bool:
    conn = get_connection()
    now = now_utc()
    cur = conn.execute(
        """
        UPDATE auth_challenges
           SET status = 'manual_required',
               last_error = ?,
               updated_at = ?
         WHERE id = ?
           AND status IN ('pending', 'watching')
        """,
        (reason, now, challenge_id),
    )
    conn.commit()
    return cur.rowcount == 1
```

In `inbox_command`, call expiry recovery before listing challenges:

```python
    inbox_auth.expire_stale_challenges()
```

- [ ] **Step 4: Run resilience tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_persistence.py tests\test_inbox_auth_cli.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit resilience helpers**

```powershell
git add src\applypilot\inbox_auth.py src\applypilot\cli.py tests\test_inbox_auth_persistence.py
git commit -m "Add inbox auth recovery helpers"
```

---

### Task 8: Full Verification and Docs

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Add README usage section**

Add this section near Gmail/application tracking documentation in `README.md`:

```markdown
### Inbox Auth Monitor

ApplyPilot can optionally monitor Gmail for job-application email verification codes.
This is read-only Gmail OAuth and is intended for ATS verification emails such as Greenhouse codes.
It does not automate Google passwords, passkeys, suspicious-login prompts, SMS, or authenticator-app 2FA.

```powershell
.\run-applypilot.ps1 inbox --scan --auth-codes --minutes 5
.\run-applypilot.ps1 inbox --auth-required
.\run-applypilot.ps1 apply --inbox-auth --base-resume --agents "claude,codex" --workers 2
```

Gmail setup uses `.applypilot\gmail_credentials.json` and `.applypilot\gmail_token.json`.
```

- [ ] **Step 2: Run focused tests**

Run:

```powershell
.\.conda-env\python.exe -m pytest tests\test_inbox_auth_schema.py tests\test_inbox_auth_extraction.py tests\test_inbox_auth_persistence.py tests\test_inbox_auth_gmail.py tests\test_inbox_auth_cli.py tests\test_apply_inbox_auth.py -q
```

Expected: all focused inbox-auth tests pass.

- [ ] **Step 3: Run full test suite**

Run:

```powershell
.\.conda-env\python.exe -m pytest -q
```

Expected: full test suite passes.

- [ ] **Step 4: Verify CLI help**

Run:

```powershell
.\run-applypilot.ps1 inbox --help
.\run-applypilot.ps1 apply --help
```

Expected:

- `inbox --help` shows `--scan`, `--watch`, `--auth-codes`, and `--auth-required`.
- `apply --help` shows `--inbox-auth`.

- [ ] **Step 5: Commit docs and final verification state**

```powershell
git add README.md
git commit -m "Document inbox auth monitor"
```

---

## Self-Review Checklist

- Spec coverage:
  - Durable inbox events: Task 1 and Task 3.
  - Auth challenges with status and expiry: Task 1, Task 3, Task 7.
  - Gmail read-only OAuth reuse: Task 4.
  - Deterministic extraction and false-positive suppression: Task 2.
  - Auto-entry through apply flow: Task 6.
  - CLI views: Task 5 and Task 7.
  - Resilience requirements: Task 3, Task 4, Task 7, Task 8.
  - No raw body or permanent code storage: Task 2 through Task 6 use snippets and metadata only.
- Type consistency:
  - Challenge status strings are `pending`, `watching`, `resolved`, `expired`, `manual_required`, and `failed`.
  - Inbox event confidence strings are `low`, `medium`, and `high`.
  - Candidate kinds are `code` and `magic_link`.
- Verification commands:
  - Focused pytest commands are listed in each task.
  - Full suite and CLI checks are listed in Task 8.
