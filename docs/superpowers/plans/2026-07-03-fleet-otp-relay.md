# Fleet-wide OTP / Email-Verification Relay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A remote apply worker clears an email-verification wall by getting the one-time code through fleet Postgres from a single Gmail-connected home box — Gmail credentials never leave the home box, and the relay is strictly additive (worst case = today's park/fail behavior).

**Architecture:** A home-side responder loop reads pending `otp_request` rows, scans its own Gmail for a matching code, and writes the code into the row with a short expiry; a worker-side relay client files a request and polls the row, consuming the code single-use; the existing launcher hint function gains a relay branch that returns the identical hint string. All coordination is over Postgres.

**Tech Stack:** Python 3.11+ (psycopg3), pytest with the repo's `fleet_db` disposable-Postgres fixture, the existing `inbox_auth.scan_gmail_for_auth_codes` Gmail matcher (mocked in tests).

## Global Constraints

- **Repo:** `C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot`, branch `applypilot-hardening-and-brainstorm-integration`. The working tree has UNRELATED uncommitted changes (resbuild bridge) and another session may commit concurrently — **never `git add -A`/`git add .`**; stage only files a task names, and immediately before each commit run `git diff --cached --name-only` to confirm only the task's files are staged.
- **Spec:** `docs/superpowers/specs/2026-07-03-fleet-otp-relay-design.md`.
- **Security invariants (every task):** the one-time code is NEVER written to any log (no `logger.*`/`print` of the value); Gmail is read ONLY on the home box (`answer_pending` is the only function that touches a Gmail service); the code rests in PG only between answer and consume, is nulled on consume, and is purged on expiry.
- **Consume is single-use:** enforced by `UPDATE … WHERE consumed_at IS NULL` under `FOR UPDATE`. A second consume returns nothing.
- **Matching is time-based, not sender-strict:** a candidate matches a request only if the email arrived AFTER `requested_at` (minus a small clock skew) and within the window; `sender_hint` is stored for audit but is NOT a hard filter (verification emails routinely come from a different domain than the apply host — this mirrors the existing local matcher `watch_gmail_for_auth_code`, which filters by time only). Each Gmail `message_id` is assigned to at most one request per responder cycle.
- **code_kind values:** `"code"` or `"magic_link"` (exactly the `VerificationCandidate.kind` values; `inbox_auth.py:121`).
- **Tests:** run from the repo root in PowerShell: `& .\.conda-env\python.exe -m pytest <file> -v`. PG-backed tests use the `fleet_db` fixture (`tests/conftest.py:97`) needing the `applypilot-pgtest` conda env; if that env is missing the fixture errors — report it, do not stub.
- **Commit style:** one commit per task, prefix `feat(otp-relay):` / `fix(otp-relay):` / `docs(otp-relay):`, ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Schema — add the code-transport columns to `otp_request`

The `otp_request` table (`fleet/schema_v3.sql:238-246`) has only bookkeeping columns. Add the short-lived transport columns and the responder's pending-scan index, idempotently.

**Files:**
- Modify: `src/applypilot/fleet/schema_v3.sql` (after the `otp_request` CREATE TABLE, ~line 246)
- Test: `tests/test_otp_relay_schema.py` (new)

**Interfaces:**
- Produces: `otp_request` columns `code TEXT`, `code_kind TEXT`, `expires_at TIMESTAMPTZ`, `answered_at TIMESTAMPTZ`; partial index `idx_otp_pending`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_otp_relay_schema.py`:

```python
"""otp_request gains the short-lived code-transport columns the relay uses."""
from applypilot.apply import pgqueue
from applypilot.fleet import schema as fleet_schema


def test_otp_request_has_transport_columns(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'otp_request'"
            )
            cols = {r["column_name"] for r in cur.fetchall()}
    for needed in ("code", "code_kind", "expires_at", "answered_at",
                   "worker_id", "url", "sender_hint", "requested_at", "consumed_at"):
        assert needed in cols, f"missing column {needed}: {sorted(cols)}"


def test_otp_request_dml_roundtrip(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO otp_request (worker_id, url, sender_hint, expires_at) "
                "VALUES ('mac-0', 'https://x/apply', 'greenhouse.io', now() + interval '5 min') "
                "RETURNING id"
            )
            rid = cur.fetchone()["id"]
            cur.execute("UPDATE otp_request SET code='123456', code_kind='code', "
                        "answered_at=now() WHERE id=%s", (rid,))
            cur.execute("SELECT code, code_kind FROM otp_request WHERE id=%s", (rid,))
            row = cur.fetchone()
        conn.commit()
    assert row["code"] == "123456" and row["code_kind"] == "code"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& .\.conda-env\python.exe -m pytest tests\test_otp_relay_schema.py -v`
Expected: FAIL — `test_otp_request_has_transport_columns` asserts `code` missing; the DML test errors with `column "code" of relation "otp_request" does not exist`.

- [ ] **Step 3: Add the columns + index**

In `src/applypilot/fleet/schema_v3.sql`, immediately after the `otp_request` `CREATE TABLE … );` block (line 246) add:

```sql
-- Relay transport columns (2026-07-03): the CODE lives here only for the seconds
-- between the home responder answering and the worker consuming it, then is nulled.
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS code        TEXT;
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS code_kind   TEXT;   -- 'code' | 'magic_link'
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS expires_at  TIMESTAMPTZ;
ALTER TABLE otp_request ADD COLUMN IF NOT EXISTS answered_at TIMESTAMPTZ;
-- The responder's pending-scan: unanswered, unconsumed requests.
CREATE INDEX IF NOT EXISTS idx_otp_pending ON otp_request (requested_at)
    WHERE code IS NULL AND consumed_at IS NULL;
```

- [ ] **Step 4: Ensure the test fixture truncates otp_request**

Check `tests/conftest.py` — the `_V3_TABLES` list used by the `fleet_db` fixture. If `otp_request` is not already in it, add it (keep alphabetical/existing order). Run:
`& .\.conda-env\python.exe -c "import re; s=open(r'tests/conftest.py').read(); print('otp_request' in s)"`
Expected: prints `True` (either already present, or after you add it). If you had to add it, include `tests/conftest.py` in this task's commit.

- [ ] **Step 5: Run tests to verify they pass**

Run: `& .\.conda-env\python.exe -m pytest tests\test_otp_relay_schema.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add src/applypilot/fleet/schema_v3.sql tests/test_otp_relay_schema.py
# add tests/conftest.py ONLY if you modified it in Step 4
git diff --cached --name-only   # confirm only these files
git commit -m "feat(otp-relay): otp_request code-transport columns + pending index

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `otp_relay.py` — worker-side client (request + single-use consume)

The worker files a request and polls the row for the answer, consuming it exactly once.

**Files:**
- Create: `src/applypilot/fleet/otp_relay.py`
- Test: `tests/test_otp_relay_worker.py` (new)

**Interfaces:**
- Consumes: `pgqueue.connect(dsn)` (existing); the Task 1 columns.
- Produces:
  - `RelayCode` dataclass: `value: str`, `kind: str`.
  - `request_code(conn, *, worker_id: str, job_url: str, application_url: str, ttl_seconds: int = 300) -> int`
  - `poll_for_code(conn, request_id: int, *, timeout_seconds: int = 300, poll_seconds: float = 5.0) -> RelayCode | None`
  - `_try_consume(conn, request_id: int) -> RelayCode | None` (one atomic CTE consume)
  - `_apply_domain(application_url: str) -> str`

- [ ] **Step 1: Write the failing test**

Create `tests/test_otp_relay_worker.py`:

```python
"""Worker side: file an otp_request, poll the row, consume the code exactly once."""
import time

from applypilot.apply import pgqueue
from applypilot.fleet import otp_relay, schema as fleet_schema


def _fresh(fleet_db):
    conn = pgqueue.connect(fleet_db)
    fleet_schema.ensure_schema_v3(conn)
    return conn


def _home_writes_code(conn, request_id, code="482913", kind="code"):
    with conn.cursor() as cur:
        cur.execute("UPDATE otp_request SET code=%s, code_kind=%s, answered_at=now() WHERE id=%s",
                    (code, kind, request_id))
    conn.commit()


def test_request_code_inserts_pending_row(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(conn, worker_id="mac-0",
                                     job_url="https://li/jobs/1",
                                     application_url="https://job-boards.greenhouse.io/x/jobs/9")
        with conn.cursor() as cur:
            cur.execute("SELECT worker_id, sender_hint, code, consumed_at, expires_at "
                        "FROM otp_request WHERE id=%s", (rid,))
            row = cur.fetchone()
    assert row["worker_id"] == "mac-0"
    assert row["sender_hint"] == "job-boards.greenhouse.io"
    assert row["code"] is None and row["consumed_at"] is None
    assert row["expires_at"] is not None


def test_poll_returns_code_then_single_use(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(conn, worker_id="mac-0",
                                     job_url="j", application_url="https://greenhouse.io/a")
        _home_writes_code(conn, rid, code="482913", kind="code")
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=2, poll_seconds=0.1)
        assert got is not None and got.value == "482913" and got.kind == "code"
        # consumed: code nulled, consumed_at set
        with conn.cursor() as cur:
            cur.execute("SELECT code, consumed_at FROM otp_request WHERE id=%s", (rid,))
            r = cur.fetchone()
        assert r["code"] is None and r["consumed_at"] is not None
        # a second poll finds nothing (single-use)
        again = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.1)
        assert again is None


def test_poll_times_out_when_no_answer(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(conn, worker_id="mac-0",
                                     job_url="j", application_url="https://greenhouse.io/a")
        start = time.monotonic()
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.2)
        assert got is None
        assert time.monotonic() - start >= 1.0


def test_poll_ignores_expired_code(fleet_db):
    with _fresh(fleet_db) as conn:
        rid = otp_relay.request_code(conn, worker_id="mac-0", job_url="j",
                                     application_url="https://greenhouse.io/a", ttl_seconds=1)
        with conn.cursor() as cur:  # answer but with an already-past expiry
            cur.execute("UPDATE otp_request SET code='999', code_kind='code', "
                        "answered_at=now(), expires_at=now() - interval '1 second' WHERE id=%s", (rid,))
        conn.commit()
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.2)
    assert got is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& .\.conda-env\python.exe -m pytest tests\test_otp_relay_worker.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'applypilot.fleet.otp_relay'`.

- [ ] **Step 3: Implement the worker-side client**

Create `src/applypilot/fleet/otp_relay.py`:

```python
"""Fleet-wide OTP (email-verification code) relay over Postgres.

A remote worker that hits an email-verification wall files an ``otp_request`` and
polls it for a code; the home-side responder (answer_pending, below) reads the
home box's Gmail and writes the code into the row. The code lives in PG only for
the seconds between answer and consume, is single-use, and is NEVER logged. Gmail
is read only by ``answer_pending`` (home box). See the 2026-07-03 relay spec."""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class RelayCode:
    value: str
    kind: str  # 'code' | 'magic_link'


def _apply_domain(application_url: str) -> str:
    return (urlparse(application_url or "").hostname or "").lower()


def request_code(conn, *, worker_id: str, job_url: str, application_url: str,
                 ttl_seconds: int = 300) -> int:
    """File a pending OTP request; return its id. Never blocks."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO otp_request (worker_id, url, sender_hint, expires_at) "
            "VALUES (%s, %s, %s, now() + make_interval(secs => %s)) RETURNING id",
            (worker_id, application_url or job_url, _apply_domain(application_url), ttl_seconds),
        )
        rid = cur.fetchone()["id"]
    conn.commit()
    return rid


def _try_consume(conn, request_id: int) -> RelayCode | None:
    """Atomically capture-and-null an unexpired, unconsumed code. Single-use."""
    with conn.cursor() as cur:
        cur.execute(
            "WITH picked AS ("
            "  SELECT id, code, code_kind FROM otp_request "
            "  WHERE id = %s AND consumed_at IS NULL AND code IS NOT NULL "
            "        AND (expires_at IS NULL OR expires_at > now()) "
            "  FOR UPDATE"
            ") "
            "UPDATE otp_request o SET consumed_at = now(), code = NULL "
            "FROM picked WHERE o.id = picked.id "
            "RETURNING picked.code AS code, picked.code_kind AS code_kind",
            (request_id,),
        )
        row = cur.fetchone()
    conn.commit()
    if not row:
        return None
    return RelayCode(value=row["code"], kind=(row["code_kind"] or "code"))


def poll_for_code(conn, request_id: int, *, timeout_seconds: int = 300,
                  poll_seconds: float = 5.0) -> RelayCode | None:
    """Poll the request row until a code is available, consuming it, or timeout."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        code = _try_consume(conn, request_id)
        if code is not None:
            return code
        if time.monotonic() >= deadline:
            return None
        time.sleep(max(0.0, poll_seconds))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `& .\.conda-env\python.exe -m pytest tests\test_otp_relay_worker.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add src/applypilot/fleet/otp_relay.py tests/test_otp_relay_worker.py
git diff --cached --name-only
git commit -m "feat(otp-relay): worker-side request_code + single-use poll_for_code

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `otp_relay.py` — home-side responder (match Gmail → write code) + purge

The home box reads its Gmail and answers pending requests. Matching is time-based (email after the request, within window), highest confidence first, each `message_id` used once.

**Files:**
- Modify: `src/applypilot/fleet/otp_relay.py`
- Test: `tests/test_otp_relay_responder.py` (new)

**Interfaces:**
- Consumes: `inbox_auth.scan_gmail_for_auth_codes(service=..., minutes=..., max_messages=...) -> list[AuthEmailMatch]`; `AuthEmailMatch` has `.message_id`, `.received_at` (RFC2822 string), `.candidate.value`, `.candidate.kind` (`"code"|"magic_link"`).
- Produces:
  - `answer_pending(conn, gmail_service, *, window_minutes: int = 15, max_messages: int = 25, skew_seconds: int = 60, answered_ttl_seconds: int = 120) -> int`
  - `purge_expired(conn) -> int`
  - `_parse_email_dt(raw: str | None) -> datetime | None`

- [ ] **Step 1: Write the failing test**

Create `tests/test_otp_relay_responder.py`:

```python
"""Home responder: match Gmail codes to pending requests (time-based, single-assign)."""
import datetime as dt

from applypilot.apply import pgqueue
from applypilot.fleet import otp_relay, schema as fleet_schema


class _Cand:
    def __init__(self, value, kind="code"):
        self.value, self.kind = value, kind


class _Match:
    def __init__(self, message_id, received_at, value, kind="code"):
        self.message_id = message_id
        self.received_at = received_at  # RFC2822 string
        self.candidate = _Cand(value, kind)


class _FakeGmail:
    """Stands in for a Gmail service via scan_gmail_for_auth_codes monkeypatch."""
    def __init__(self, matches):
        self.matches = matches


def _rfc(when: dt.datetime) -> str:
    from email.utils import format_datetime
    return format_datetime(when)


def _fresh(fleet_db):
    conn = pgqueue.connect(fleet_db)
    fleet_schema.ensure_schema_v3(conn)
    return conn


def _pending(conn, worker_id="mac-0", domain="greenhouse.io"):
    return otp_relay.request_code(conn, worker_id=worker_id, job_url="j",
                                  application_url=f"https://{domain}/a")


def test_answer_writes_code_for_email_after_request(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [_Match("m1", _rfc(now + dt.timedelta(seconds=30)), "554466")]
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes",
                        lambda **kw: matches)
    with _fresh(fleet_db) as conn:
        rid = _pending(conn)
        n = otp_relay.answer_pending(conn, _FakeGmail(matches))
        assert n == 1
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.1)
    assert got is not None and got.value == "554466"


def test_stale_email_before_request_is_not_matched(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    # email arrived 10 minutes BEFORE the request -> must not match
    matches = [_Match("m_old", _rfc(now - dt.timedelta(minutes=10)), "000000")]
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes",
                        lambda **kw: matches)
    with _fresh(fleet_db) as conn:
        rid = _pending(conn)
        n = otp_relay.answer_pending(conn, _FakeGmail(matches))
        assert n == 0
        got = otp_relay.poll_for_code(conn, rid, timeout_seconds=1, poll_seconds=0.1)
    assert got is None


def test_two_requests_get_distinct_codes_one_message_each(fleet_db, monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    matches = [
        _Match("mA", _rfc(now + dt.timedelta(seconds=20)), "111111"),
        _Match("mB", _rfc(now + dt.timedelta(seconds=40)), "222222"),
    ]
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes",
                        lambda **kw: matches)
    with _fresh(fleet_db) as conn:
        r1 = _pending(conn, worker_id="mac-0")
        r2 = _pending(conn, worker_id="m2-0")
        n = otp_relay.answer_pending(conn, _FakeGmail(matches))
        assert n == 2
        c1 = otp_relay.poll_for_code(conn, r1, timeout_seconds=1, poll_seconds=0.1)
        c2 = otp_relay.poll_for_code(conn, r2, timeout_seconds=1, poll_seconds=0.1)
    vals = {c1.value, c2.value}
    assert vals == {"111111", "222222"}  # distinct codes, no double-assignment


def test_purge_expired_nulls_code_keeps_row(fleet_db, monkeypatch):
    monkeypatch.setattr(otp_relay.inbox_auth, "scan_gmail_for_auth_codes", lambda **kw: [])
    with _fresh(fleet_db) as conn:
        rid = _pending(conn)
        with conn.cursor() as cur:
            cur.execute("UPDATE otp_request SET code='777', code_kind='code', "
                        "expires_at=now() - interval '1 min' WHERE id=%s", (rid,))
        conn.commit()
        purged = otp_relay.purge_expired(conn)
        assert purged == 1
        with conn.cursor() as cur:
            cur.execute("SELECT code, worker_id FROM otp_request WHERE id=%s", (rid,))
            row = cur.fetchone()
    assert row["code"] is None and row["worker_id"] == "mac-0"  # audit row kept
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& .\.conda-env\python.exe -m pytest tests\test_otp_relay_responder.py -v`
Expected: FAIL — `AttributeError: module 'applypilot.fleet.otp_relay' has no attribute 'inbox_auth'` (and `answer_pending`/`purge_expired` undefined).

- [ ] **Step 3: Implement the responder + purge**

Append to `src/applypilot/fleet/otp_relay.py` (and add the imports at the top with the existing ones):

```python
import datetime as _dt
from email.utils import parsedate_to_datetime

from applypilot import inbox_auth
```

Then append these functions:

```python
def _parse_email_dt(raw):
    """Parse an RFC2822 'Date' header to an aware UTC datetime, or None."""
    if not raw:
        return None
    try:
        d = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return d.astimezone(_dt.timezone.utc)


def answer_pending(conn, gmail_service, *, window_minutes: int = 15,
                   max_messages: int = 25, skew_seconds: int = 60,
                   answered_ttl_seconds: int = 120) -> int:
    """Read Gmail ONCE and answer every pending request whose code arrived after it.

    Home box only (this is the sole function that touches Gmail). Time-based match:
    a candidate fits a request when its email arrived >= requested_at - skew. Each
    message_id is assigned to at most one request. The code is NEVER logged."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, requested_at FROM otp_request "
            "WHERE code IS NULL AND consumed_at IS NULL "
            "      AND (expires_at IS NULL OR expires_at > now()) "
            "ORDER BY requested_at"
        )
        pending = cur.fetchall()
    if not pending:
        return 0

    matches = inbox_auth.scan_gmail_for_auth_codes(
        service=gmail_service, minutes=window_minutes, max_messages=max_messages)
    # Newest first so the freshest code goes to the oldest waiting request.
    parsed = [(m, _parse_email_dt(m.received_at)) for m in matches]
    parsed = [(m, ts) for (m, ts) in parsed if ts is not None]
    parsed.sort(key=lambda mt: mt[1], reverse=True)

    used_messages: set = set()
    answered = 0
    for req in pending:
        req_floor = req["requested_at"] - _dt.timedelta(seconds=skew_seconds)
        chosen = None
        for m, ts in parsed:
            if m.message_id in used_messages:
                continue
            if ts >= req_floor:
                chosen = m
                break
        if chosen is None:
            continue
        used_messages.add(chosen.message_id)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE otp_request SET code=%s, code_kind=%s, matched_email_ts=%s, "
                "answered_at=now(), expires_at = now() + make_interval(secs => %s) "
                "WHERE id=%s AND code IS NULL AND consumed_at IS NULL",
                (chosen.candidate.value, chosen.candidate.kind,
                 _parse_email_dt(chosen.received_at), answered_ttl_seconds, req["id"]),
            )
            if cur.rowcount:
                answered += 1
        conn.commit()
    return answered


def purge_expired(conn) -> int:
    """Null the code on expired/consumed rows so no code lingers; keep the audit row."""
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE otp_request SET code = NULL "
            "WHERE code IS NOT NULL AND expires_at IS NOT NULL AND expires_at <= now()"
        )
        n = cur.rowcount
    conn.commit()
    return n
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `& .\.conda-env\python.exe -m pytest tests\test_otp_relay_responder.py -v`
Expected: 4 passed. (The monkeypatch replaces `otp_relay.inbox_auth.scan_gmail_for_auth_codes`, so the fake gmail service's contents are irrelevant — the patched function returns the test's `matches`.)

- [ ] **Step 5: Run the whole relay module's tests**

Run: `& .\.conda-env\python.exe -m pytest tests\test_otp_relay_worker.py tests\test_otp_relay_responder.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add src/applypilot/fleet/otp_relay.py tests/test_otp_relay_responder.py
git diff --cached --name-only
git commit -m "feat(otp-relay): home-side answer_pending (time-based match, single-assign) + purge

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Home responder entrypoint `applypilot-fleet-otp-home`

A loop the owner runs on the home box: build Gmail once per cycle, answer + purge, heartbeat, sleep.

**Files:**
- Create: `src/applypilot/fleet/otp_responder_main.py`
- Modify: `pyproject.toml` ([project.scripts])
- Test: `tests/test_otp_responder_main.py` (new)

**Interfaces:**
- Consumes: `otp_relay.answer_pending`, `otp_relay.purge_expired`; `gmail_outcomes.build_gmail_service()`; `fleet.worker._heartbeat` (signature `_heartbeat(conn, *, worker_id, machine_owner, home_ip, role, state, current_job=None, ...)`).
- Produces: `run_once(conn, gmail_service) -> dict`, `main(argv=None) -> int`, console script `applypilot-fleet-otp-home`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_otp_responder_main.py`:

```python
"""The responder entrypoint answers + purges in one cycle and is import-safe."""
from applypilot.apply import pgqueue
from applypilot.fleet import otp_responder_main, schema as fleet_schema


def test_run_once_answers_and_purges(fleet_db, monkeypatch):
    calls = {"answer": 0, "purge": 0}
    monkeypatch.setattr(otp_responder_main.otp_relay, "answer_pending",
                        lambda conn, svc, **kw: calls.__setitem__("answer", calls["answer"] + 1) or 3)
    monkeypatch.setattr(otp_responder_main.otp_relay, "purge_expired",
                        lambda conn: calls.__setitem__("purge", calls["purge"] + 1) or 1)
    with pgqueue.connect(fleet_db) as conn:
        fleet_schema.ensure_schema_v3(conn)
        out = otp_responder_main.run_once(conn, gmail_service=object())
    assert out == {"answered": 3, "purged": 1}
    assert calls == {"answer": 1, "purge": 1}


def test_main_requires_dsn(monkeypatch):
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    try:
        otp_responder_main.main(["--once"])
        assert False, "expected SystemExit"
    except SystemExit as e:
        assert "dsn" in str(e).lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& .\.conda-env\python.exe -m pytest tests\test_otp_responder_main.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'applypilot.fleet.otp_responder_main'`.

- [ ] **Step 3: Implement the entrypoint**

Create `src/applypilot/fleet/otp_responder_main.py`:

```python
"""Home-box OTP responder loop (entrypoint: applypilot-fleet-otp-home).

Runs alongside the watchdog/doctor on the box that holds the Gmail token. Each
cycle reads Gmail once (only when requests are pending), answers matching
requests, purges expired codes, and heartbeats. The verification code is never
logged. See the 2026-07-03 relay spec."""
from __future__ import annotations

import argparse
import logging
import os
import time

from applypilot.fleet import otp_relay

logger = logging.getLogger(__name__)


def run_once(conn, gmail_service) -> dict:
    answered = otp_relay.answer_pending(conn, gmail_service)
    purged = otp_relay.purge_expired(conn)
    return {"answered": answered, "purged": purged}


def _beat(conn, *, machine_owner, state):
    try:
        from applypilot.fleet.worker import _heartbeat
        _heartbeat(conn, worker_id="otp_responder", machine_owner=machine_owner,
                   home_ip="0.0.0.0", role="otp_responder", state=state)
    except Exception:  # pragma: no cover - heartbeat is best-effort
        logger.debug("otp_responder heartbeat failed", exc_info=True)


def main(argv=None) -> int:  # pragma: no cover - long-running loop
    p = argparse.ArgumentParser(prog="applypilot-fleet-otp-home")
    p.add_argument("--dsn", default=os.environ.get("FLEET_PG_DSN"))
    p.add_argument("--interval", type=float, default=5.0)
    p.add_argument("--machine-owner", default=os.environ.get("FLEET_MACHINE_OWNER", "home"))
    p.add_argument("--once", action="store_true", help="run a single cycle then exit")
    args = p.parse_args(argv)
    if not args.dsn:
        raise SystemExit("set --dsn or FLEET_PG_DSN")

    from applypilot.apply import pgqueue
    from applypilot.gmail_outcomes import build_gmail_service

    while True:
        try:
            gmail_service = build_gmail_service()
            with pgqueue.connect(args.dsn) as conn:
                _beat(conn, machine_owner=args.machine_owner, state="busy")
                out = run_once(conn, gmail_service)
                _beat(conn, machine_owner=args.machine_owner, state="idle")
            logger.info("otp responder cycle: answered=%s purged=%s",
                        out["answered"], out["purged"])
        except Exception:
            logger.exception("otp responder cycle failed; backing off")
        if args.once:
            return 0
        time.sleep(max(0.5, args.interval))
```

- [ ] **Step 4: Register the console script**

In `pyproject.toml`, under `[project.scripts]`, add alongside the other `applypilot-fleet-*` entries:

```toml
applypilot-fleet-otp-home = "applypilot.fleet.otp_responder_main:main"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `& .\.conda-env\python.exe -m pytest tests\test_otp_responder_main.py -v`
Expected: 2 passed.

- [ ] **Step 6: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add src/applypilot/fleet/otp_responder_main.py pyproject.toml tests/test_otp_responder_main.py
git diff --cached --name-only
git commit -m "feat(otp-relay): applypilot-fleet-otp-home responder entrypoint

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: Launcher wiring — relay branch in `_poll_inbox_auth_hint`

On a remote worker (`APPLYPILOT_INBOX_AUTH_MODE=relay`) the launcher asks the fleet instead of reading Gmail locally, returning the identical hint format.

**Files:**
- Modify: `src/applypilot/apply/launcher.py` (`_poll_inbox_auth_hint`, ~1597-1664)
- Test: `tests/test_launcher_inbox_relay.py` (new)

**Interfaces:**
- Consumes: `otp_relay.request_code`, `otp_relay.poll_for_code`, `otp_relay.RelayCode`.
- Produces: `_inbox_auth_mode() -> str`, `_relay_inbox_auth_hint(job) -> str | None`; `_poll_inbox_auth_hint` dispatches on mode. Hint format: magic link → `"magic_link=<v>\nsource=fleet_relay"`, code → `"code=<v>\nsource=fleet_relay"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_launcher_inbox_relay.py`:

```python
"""Launcher relay mode returns the standard hint via the fleet, not local Gmail."""
from applypilot.apply import launcher
from applypilot.fleet import otp_relay


def test_relay_mode_returns_code_hint(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_MODE", "relay")
    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://stub")

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn: _Conn())
    monkeypatch.setattr(otp_relay, "request_code", lambda conn, **kw: 7)
    monkeypatch.setattr(otp_relay, "poll_for_code",
                        lambda conn, rid, **kw: otp_relay.RelayCode(value="246810", kind="code"))

    hint = launcher._poll_inbox_auth_hint({"url": "https://li/1",
                                           "application_url": "https://greenhouse.io/a"})
    assert hint is not None
    assert "code=246810" in hint and "fleet_relay" in hint


def test_relay_mode_magic_link_hint(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_MODE", "relay")
    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://stub")

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn: _Conn())
    monkeypatch.setattr(otp_relay, "request_code", lambda conn, **kw: 8)
    monkeypatch.setattr(otp_relay, "poll_for_code",
                        lambda conn, rid, **kw: otp_relay.RelayCode(
                            value="https://x/verify?t=abc", kind="magic_link"))

    hint = launcher._poll_inbox_auth_hint({"url": "j", "application_url": "https://greenhouse.io/a"})
    assert hint is not None and hint.startswith("magic_link=https://x/verify?t=abc")


def test_relay_mode_timeout_returns_none(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH", "1")
    monkeypatch.setenv("APPLYPILOT_INBOX_AUTH_MODE", "relay")
    monkeypatch.setenv("FLEET_PG_DSN", "postgresql://stub")

    class _Conn:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def close(self): pass
    monkeypatch.setattr("applypilot.apply.pgqueue.connect", lambda dsn: _Conn())
    monkeypatch.setattr(otp_relay, "request_code", lambda conn, **kw: 9)
    monkeypatch.setattr(otp_relay, "poll_for_code", lambda conn, rid, **kw: None)

    assert launcher._poll_inbox_auth_hint({"url": "j", "application_url": "https://greenhouse.io/a"}) is None


def test_local_mode_unchanged_when_disabled(monkeypatch):
    monkeypatch.delenv("APPLYPILOT_INBOX_AUTH", raising=False)  # disabled entirely
    assert launcher._poll_inbox_auth_hint({"url": "j", "application_url": "https://greenhouse.io/a"}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& .\.conda-env\python.exe -m pytest tests\test_launcher_inbox_relay.py -v`
Expected: FAIL — `test_relay_mode_returns_code_hint` gets `None` (no relay branch exists; `_poll_inbox_auth_hint` runs the local Gmail path which returns None here).

- [ ] **Step 3: Implement the relay branch**

In `src/applypilot/apply/launcher.py`, add after `_inbox_auth_enabled()` (line ~1582):

```python
def _inbox_auth_mode() -> str:
    """'relay' (ask the fleet for the code) or 'local' (read Gmail here, default)."""
    return os.environ.get("APPLYPILOT_INBOX_AUTH_MODE", "local").strip().lower()


def _relay_inbox_auth_hint(job: dict) -> str | None:
    """Remote-worker path: get the verification code from the fleet OTP relay."""
    try:
        from applypilot.apply import pgqueue
        from applypilot.fleet import otp_relay

        dsn = os.environ.get("FLEET_PG_DSN")
        if not dsn:
            return None
        worker_id = os.environ.get("FLEET_WORKER_ID", "worker")
        timeout = int(os.environ.get("APPLYPILOT_INBOX_AUTH_TIMEOUT", "300"))
        poll = int(os.environ.get("APPLYPILOT_INBOX_AUTH_POLL_SECONDS", "5"))
        apply_target = job.get("application_url") or job["url"]
        with pgqueue.connect(dsn) as conn:
            rid = otp_relay.request_code(conn, worker_id=worker_id, job_url=job["url"],
                                         application_url=apply_target, ttl_seconds=timeout)
            code = otp_relay.poll_for_code(conn, rid, timeout_seconds=timeout, poll_seconds=poll)
        if code is None:
            return None
        if code.kind == "magic_link":
            return f"magic_link={code.value}\nsource=fleet_relay"
        return f"code={code.value}\nsource=fleet_relay"
    except Exception:
        logger.debug("Relay inbox auth failed", exc_info=True)
        return None
```

Then, at the very top of `_poll_inbox_auth_hint` (line ~1598), right after the docstring and before the existing `try:`, add the dispatch:

```python
    if not _inbox_auth_enabled():
        return None
    if _inbox_auth_mode() == "relay":
        return _relay_inbox_auth_hint(job)
```

(The existing body already re-checks `_inbox_auth_enabled()` inside its `try`; leaving that in place is harmless. The new guard makes the disabled-and-relay branches explicit before the local Gmail import.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `& .\.conda-env\python.exe -m pytest tests\test_launcher_inbox_relay.py -v`
Expected: 4 passed.

- [ ] **Step 5: Regression-check the launcher imports cleanly**

Run: `& .\.conda-env\python.exe -c "import applypilot.apply.launcher; print('ok')"`
Expected: prints `ok` (no import error from the new branch).

- [ ] **Step 6: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add src/applypilot/apply/launcher.py tests/test_launcher_inbox_relay.py
git diff --cached --name-only
git commit -m "feat(otp-relay): launcher relay branch (APPLYPILOT_INBOX_AUTH_MODE=relay)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Mac worker config + owner runbook

Turn the relay on for the Mac and document running the responder on the home box.

**Files:**
- Modify: `setup-mac-worker.sh` (env-file heredoc)
- Modify: `docs/fleet-mac-worker-runbook.md`
- Create: `docs/fleet-otp-relay-runbook.md`

**Interfaces:**
- Consumes: env vars `APPLYPILOT_INBOX_AUTH`, `APPLYPILOT_INBOX_AUTH_MODE`, `FLEET_WORKER_ID` (Task 5).

- [ ] **Step 1: Add relay env vars to the Mac installer**

In `setup-mac-worker.sh`, inside the `cat > "$ENV_FILE" <<EOF` heredoc, add these three lines alongside the others (single-quoted, matching the file's convention):

```
APPLYPILOT_INBOX_AUTH='1'
APPLYPILOT_INBOX_AUTH_MODE='relay'
FLEET_WORKER_ID='mac-0'
```

- [ ] **Step 2: Verify the installer still parses**

Run (Git Bash via the Bash tool): `bash -n "C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot/setup-mac-worker.sh" && echo OK`
Expected: `OK`.

- [ ] **Step 3: Write the relay runbook**

Create `docs/fleet-otp-relay-runbook.md`:

````markdown
# Fleet OTP / Email-Verification Relay — Owner Runbook

Lets remote workers (the Mac, any offsite box) clear email-verification walls using
codes read from the home box's Gmail — Gmail credentials never leave the home box.
Design: `docs/superpowers/specs/2026-07-03-fleet-otp-relay-design.md`.

## Home box (one-time + keep running)

The responder must run on the box that has Gmail (`~/.applypilot/gmail_credentials.json`).
Run it alongside your other fleet processes:

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
$env:FLEET_PG_DSN = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
.\.conda-env\Scripts\applypilot-fleet-otp-home.exe
```

Leave it running (or register it as a scheduled task the same way as the other fleet
loops). It scans Gmail only when a request is actually pending, so it is cheap when idle.

## Remote workers

The Mac installer now sets `APPLYPILOT_INBOX_AUTH=1` and `APPLYPILOT_INBOX_AUTH_MODE=relay`
automatically, so a freshly set-up worker uses the relay. For an already-installed Mac,
add those two lines to `~/applypilot-fleet/.applypilot/fleet-worker.env` and restart:
`launchctl kickstart -k gui/$(id -u)/com.applypilot.fleetworker`.

## Verify end to end

With the responder running and a worker applying to a job that needs email verification,
watch a request appear and get answered (run on the home box):

```powershell
.\.conda-env\python.exe -c 'from applypilot.apply import pgqueue; c=pgqueue.connect("host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"); cur=c.cursor(); cur.execute("SELECT id, worker_id, sender_hint, requested_at, answered_at, consumed_at, (code IS NOT NULL) AS has_code FROM otp_request ORDER BY id DESC LIMIT 5"); [print(r) for r in cur.fetchall()]; c.close()'
```

A healthy cycle shows a row go `requested_at` set → `answered_at` set (`has_code` briefly true)
→ `consumed_at` set (`has_code` false). The code value is never displayed or logged.

## Notes

- If the home box or responder is down, remote workers time out and the job parks/fails
  gracefully exactly as before — the relay never makes things worse.
- Matching is time-based (the code email must arrive AFTER the request); concurrent
  applies on the same ATS are assigned nearest-in-time, one email per request.
````

- [ ] **Step 4: Cross-link from the Mac runbook**

In `docs/fleet-mac-worker-runbook.md`, in the section G troubleshooting table, update the Gmail/OTP row to point at the relay. Replace the existing row:

```
| Gmail/OTP challenges park as auth_challenge | Expected: `APPLYPILOT_ENABLE_GMAIL_MCP=0` on the Mac v1 (no Gmail OAuth creds there). Resolve challenges from the console, or copy Gmail creds + set the flag to 1 later. |
```

with:

```
| Email-verification jobs fail/park | The Mac now uses the fleet OTP relay (`APPLYPILOT_INBOX_AUTH_MODE=relay`). Ensure `applypilot-fleet-otp-home` is running on the home box — see docs/fleet-otp-relay-runbook.md. |
```

- [ ] **Step 5: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add setup-mac-worker.sh docs/fleet-otp-relay-runbook.md docs/fleet-mac-worker-runbook.md
git diff --cached --name-only
git commit -m "docs(otp-relay): enable relay in Mac installer + owner runbook

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Full regression + wrap-up

**Files:**
- Modify (status line only): `docs/superpowers/specs/2026-07-03-fleet-otp-relay-design.md`

- [ ] **Step 1: Run the relay + fleet + launcher test sweep**

Run: `& .\.conda-env\python.exe -m pytest tests -q -k "otp or inbox or fleet or launcher"`
Expected: all pass. Report any pre-existing failures unrelated to this feature separately (do not fix them here). If PG-fixture tests error because the `applypilot-pgtest` env is missing, report it and run the non-PG subset.

- [ ] **Step 2: Confirm no code-leak and no stray staging**

Run: `git grep -n "logger.*code\.value\|print(.*code" -- src/applypilot/fleet/otp_relay.py src/applypilot/fleet/otp_responder_main.py`
Expected: no matches (the code value is never logged). Then `git status --short` — working tree clean except unrelated files.

- [ ] **Step 3: Mark the spec implemented**

In `docs/superpowers/specs/2026-07-03-fleet-otp-relay-design.md` change `**Status:** Approved by owner (brainstorming session)` to `**Status:** Implemented (see docs/superpowers/plans/2026-07-03-fleet-otp-relay.md); owner runs applypilot-fleet-otp-home per docs/fleet-otp-relay-runbook.md`.

- [ ] **Step 4: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add docs/superpowers/specs/2026-07-03-fleet-otp-relay-design.md
git commit -m "docs(otp-relay): mark spec implemented

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
