# ApplyPilot Outcomes Tracker — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Read each application-related email, capture outcome/reason/title/timestamp into a new `email_events` table, and surface a full per-application timeline, analytics, a follow-up worklist, and descriptive "lane insights" in a local read-only web dashboard.

**Architecture:** Additive layer on the existing SQLite brain. A new `email_events` table (idempotent by Gmail `message_id`) is the evidence layer. An LLM extractor (with deterministic heuristic fallback) turns each candidate email into structured fields; a scanner reuses the existing Gmail OAuth + job-matching to populate the table. Two pure modules (`outcome_timeline`, `lane_insights`) compute metrics; a stdlib `http.server` dashboard renders them read-only and exports CSV. Two flat CLI commands drive it.

**Tech Stack:** Python 3.10+, SQLite (stdlib `sqlite3`), Typer + Rich (CLI), stdlib `http.server` (dashboard — NO new web deps), existing `applypilot.llm.LLMClient`, pytest.

## Global Constraints

- **Runtime interpreter:** the repo's `.conda-env` Python. Run everything as `.conda-env/python.exe -m pytest ...` from the repo root (`New project/ApplyPilot`). The package is editable-installed there.
- **No new pip dependencies.** The dashboard uses stdlib `http.server` only, exactly like `src/applypilot/fleet/console_app.py`.
- **Python owns the brain schema.** New tables are added via an `ensure_*` function called from `init_db()` in `src/applypilot/database.py` — never ad-hoc DDL elsewhere.
- **Canonical store is the SQLite brain** at `%LOCALAPPDATA%\ApplyPilot\applypilot.db`. Never write Postgres. The dashboard opens the brain **read-only** (`sqlite3.connect("file:...?mode=ro", uri=True)`).
- **Gmail stays read-only** (`gmail.readonly`); reuse `build_gmail_service`. Promotion into the `applications` tracker is **dry-run by default**.
- **Idempotency:** every email row is keyed by `message_id`; re-runs must not duplicate or re-extract unless `--reextract`.
- **Stage vocabulary (fixed):** `acknowledged | screen | assessment | interview | offer | rejected | other`. **Outcome (terminal):** `offer | rejected | None`.
- **Every git commit message ends with the trailer:**
  `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`
- **Spec:** `docs/superpowers/specs/2026-06-29-applypilot-outcomes-tracker-design.md`.

---

## File Structure

- `src/applypilot/database.py` — **modify**: add `ensure_outcome_tables(conn)` and call it from `init_db()`.
- `src/applypilot/outcome_extract.py` — **create**: `ExtractedOutcome` + `extract_outcome(...)` (LLM + heuristic fallback + JSON validation).
- `src/applypilot/outcome_scan.py` — **create**: `build_email_event(...)`, `upsert_email_event(...)`, `scan_outcomes(...)` (Gmail orchestration; injectable fetch for tests).
- `src/applypilot/outcome_timeline.py` — **create (pure)**: `build_timeline(applied_at, events, now_iso)`.
- `src/applypilot/lane_insights.py` — **create (pure)**: `derive_segments(job)`, `wilson_interval(...)`, `compute_lane_insights(apps, floor)`.
- `src/applypilot/outcome_dashboard.py` — **create**: `build_application_rows(conn)`, `build_csv(rows)`, stdlib `http.server` `serve(...)`.
- `src/applypilot/cli.py` — **modify**: add `outcomes-scan` and `outcomes-dashboard` flat commands near `scan-gmail` (~line 1577).
- `tests/test_outcome_schema.py`, `tests/test_outcome_extract.py`, `tests/test_outcome_scan.py`, `tests/test_outcome_timeline.py`, `tests/test_lane_insights.py`, `tests/test_outcome_dashboard.py` — **create**.

---

### Task 1: `email_events` schema migration

**Files:**
- Modify: `src/applypilot/database.py` (add `ensure_outcome_tables`; call it inside `init_db`, after `ensure_research_tables(conn)` near line 184)
- Test: `tests/test_outcome_schema.py`

**Interfaces:**
- Produces: `ensure_outcome_tables(conn: sqlite3.Connection | None = None) -> None`; a table `email_events` with columns `message_id (PK), thread_id, job_url, occurred_at, sender, sender_domain, subject, stage, outcome, reason, title, company, match_method, match_score, confidence, body_text, snippet, extracted_by, scanned_at` and indexes on `job_url`, `occurred_at`, `stage`.
- Consumes: `database.init_db`, `database.get_connection`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outcome_schema.py
from applypilot import database


def test_email_events_table_created(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    cols = {row[1] for row in conn.execute("PRAGMA table_info(email_events)").fetchall()}
    assert cols == {
        "message_id", "thread_id", "job_url", "occurred_at", "sender",
        "sender_domain", "subject", "stage", "outcome", "reason", "title",
        "company", "match_method", "match_score", "confidence", "body_text",
        "snippet", "extracted_by", "scanned_at",
    }


def test_email_events_message_id_is_primary_key(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    pk = [row[1] for row in conn.execute("PRAGMA table_info(email_events)").fetchall() if row[5]]
    assert pk == ["message_id"]


def test_ensure_outcome_tables_is_idempotent(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    # Second call must not raise.
    database.ensure_outcome_tables(conn)
    idx = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='email_events'"
    ).fetchall()}
    assert {"idx_email_events_job", "idx_email_events_occurred", "idx_email_events_stage"} <= idx
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_schema.py -v`
Expected: FAIL — `email_events` table does not exist / `ensure_outcome_tables` not defined.

- [ ] **Step 3: Add `ensure_outcome_tables` to `src/applypilot/database.py`**

Add this function next to `ensure_inbox_auth_tables` (after it, ~line 602):

```python
def ensure_outcome_tables(conn: sqlite3.Connection | None = None) -> None:
    """Create the email-driven outcome-tracking table (outcomes tracker spec
    2026-06-29). Distinct from inbox_events (which tracks apply-time auth/OTP
    challenges): email_events is the evidence layer for application OUTCOMES --
    one row per recruiter email, idempotent by Gmail message_id. ADDITIVE."""
    if conn is None:
        conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS email_events (
            message_id     TEXT PRIMARY KEY,
            thread_id      TEXT,
            job_url        TEXT,
            occurred_at    TEXT NOT NULL,
            sender         TEXT,
            sender_domain  TEXT,
            subject        TEXT,
            stage          TEXT NOT NULL,
            outcome        TEXT,
            reason         TEXT,
            title          TEXT,
            company        TEXT,
            match_method   TEXT,
            match_score    REAL,
            confidence     TEXT,
            body_text      TEXT,
            snippet        TEXT,
            extracted_by   TEXT,
            scanned_at     TEXT NOT NULL,
            FOREIGN KEY(job_url) REFERENCES jobs(url)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_events_job ON email_events(job_url)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_events_occurred ON email_events(occurred_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_email_events_stage ON email_events(stage)")
    conn.commit()
```

Then, inside `init_db`, add the call right after `ensure_research_tables(conn)` (line 184):

```python
    ensure_research_tables(conn)
    ensure_outcome_tables(conn)

    return conn
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_schema.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/database.py tests/test_outcome_schema.py
git commit -m "feat(outcomes): add email_events table + ensure_outcome_tables migration"
```

---

### Task 2: `outcome_extract` — LLM extraction with heuristic fallback

**Files:**
- Create: `src/applypilot/outcome_extract.py`
- Test: `tests/test_outcome_extract.py`

**Interfaces:**
- Produces:
  - `STAGES = ("acknowledged","screen","assessment","interview","offer","rejected","other")`
  - `@dataclass ExtractedOutcome(stage, outcome, reason, title, company, confidence, extracted_by)`
  - `extract_outcome(subject: str, body: str, sender: str = "", *, client=None) -> ExtractedOutcome`
- Consumes: `applypilot.llm.get_client`, `applypilot.gmail_outcomes.classify_email_outcome`.
- Behavior: builds a strict-JSON prompt, calls `client.chat(...)`, parses + validates (clamps `stage`/`outcome` to the allowed sets). On any exception or invalid JSON, falls back to `classify_email_outcome` and sets `extracted_by="heuristic_fallback"`, `reason=None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outcome_extract.py
from applypilot.outcome_extract import ExtractedOutcome, extract_outcome


class FakeClient:
    def __init__(self, reply): self._reply = reply
    def chat(self, messages, **kw): return self._reply


def test_extract_parses_valid_json():
    reply = (
        '{"stage": "rejected", "outcome": "rejected", '
        '"reason": "position was filled internally", '
        '"title": "Quant Analyst", "company": "Acme", "confidence": "high"}'
    )
    r = extract_outcome("Update on your application", "We went another direction.",
                        "careers@acme.com", client=FakeClient(reply))
    assert r.stage == "rejected"
    assert r.outcome == "rejected"
    assert r.reason == "position was filled internally"
    assert r.title == "Quant Analyst"
    assert r.company == "Acme"
    assert r.extracted_by != "heuristic_fallback"


def test_extract_handles_json_wrapped_in_text():
    reply = 'Here is the result:\n{"stage":"interview","outcome":null,"reason":null,"title":null,"company":null,"confidence":"medium"}\nDone.'
    r = extract_outcome("Interview invitation", "Use the calendly link.", client=FakeClient(reply))
    assert r.stage == "interview"
    assert r.outcome is None


def test_extract_clamps_invalid_stage_to_other():
    reply = '{"stage":"banana","outcome":"banana","reason":null,"title":null,"company":null,"confidence":"low"}'
    r = extract_outcome("hi", "hi", client=FakeClient(reply))
    assert r.stage == "other"
    assert r.outcome is None


def test_extract_falls_back_to_heuristic_when_client_raises():
    class Boom:
        def chat(self, messages, **kw): raise RuntimeError("model down")
    r = extract_outcome(
        "Unfortunately we won't be moving forward",
        "After careful consideration we have decided to pursue other candidates.",
        client=Boom(),
    )
    assert r.stage == "rejected"
    assert r.outcome == "rejected"
    assert r.extracted_by == "heuristic_fallback"
    assert r.reason is None


def test_extract_falls_back_on_unparseable_reply():
    class Junk:
        def chat(self, messages, **kw): return "no json here at all"
    r = extract_outcome("Thanks for applying", "We received your application.", client=Junk())
    assert r.stage == "acknowledged"
    assert r.extracted_by == "heuristic_fallback"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_extract.py -v`
Expected: FAIL — `No module named 'applypilot.outcome_extract'`.

- [ ] **Step 3: Create `src/applypilot/outcome_extract.py`**

```python
"""LLM extraction of application-outcome fields from a single email, with a
deterministic heuristic fallback. Pure given an injected `client` (a duck-typed
object with a .chat(messages, **kw) -> str method, e.g. applypilot.llm.LLMClient)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from applypilot.gmail_outcomes import classify_email_outcome

STAGES = ("acknowledged", "screen", "assessment", "interview", "offer", "rejected", "other")
_OUTCOMES = ("offer", "rejected")
_CONFIDENCE = ("high", "medium", "low")

# classify_email_outcome label -> our stage vocabulary
_HEURISTIC_STAGE = {
    "offer": "offer", "interview": "interview", "rejected": "rejected",
    "acknowledged": "acknowledged", "ambiguous": "other", "not_job": "other",
}

_SYSTEM = (
    "You read one recruiting email about a job application and return STRICT JSON only. "
    "Fields: stage (one of acknowledged, screen, assessment, interview, offer, rejected, other), "
    "outcome (offer, rejected, or null), reason (short plain-text reason for a rejection/offer or null), "
    "title (the job title or null), company (the company or null), confidence (high, medium, low). "
    "acknowledged = an application-received receipt. screen = recruiter screen. assessment = a test/HackerRank. "
    "Return ONLY the JSON object, no prose."
)


@dataclass
class ExtractedOutcome:
    stage: str
    outcome: str | None
    reason: str | None
    title: str | None
    company: str | None
    confidence: str
    extracted_by: str


def _parse_json(text: str) -> dict | None:
    """Parse a JSON object out of a model reply (tolerant of surrounding prose)."""
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except (json.JSONDecodeError, TypeError):
        pass
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        return None
    try:
        obj = json.loads(match.group(0))
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def _clean(value) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _heuristic(subject: str, body: str, sender: str) -> ExtractedOutcome:
    label, confidence, _ = classify_email_outcome(subject, body, sender)
    stage = _HEURISTIC_STAGE.get(label, "other")
    outcome = stage if stage in _OUTCOMES else None
    return ExtractedOutcome(
        stage=stage, outcome=outcome, reason=None, title=None, company=None,
        confidence=confidence if confidence in _CONFIDENCE else "low",
        extracted_by="heuristic_fallback",
    )


def extract_outcome(
    subject: str,
    body: str,
    sender: str = "",
    *,
    client=None,
) -> ExtractedOutcome:
    """Extract structured outcome fields from one email. Falls back to the
    deterministic heuristic classifier on any model/parse failure."""
    if client is None:
        try:
            from applypilot.llm import get_client
            client = get_client(stage="outcome_extract")
        except Exception:
            return _heuristic(subject, body, sender)

    user = f"SUBJECT: {subject}\nFROM: {sender}\nBODY:\n{body[:6000]}"
    try:
        reply = client.chat(
            messages=[{"role": "system", "content": _SYSTEM},
                      {"role": "user", "content": user}],
            temperature=0.0,
            max_tokens=400,
            stage="outcome_extract",
        )
    except Exception:
        return _heuristic(subject, body, sender)

    obj = _parse_json(reply)
    if obj is None:
        return _heuristic(subject, body, sender)

    stage = str(obj.get("stage") or "").strip().lower()
    if stage not in STAGES:
        stage = "other"
    outcome = str(obj.get("outcome") or "").strip().lower()
    outcome = outcome if outcome in _OUTCOMES else None
    confidence = str(obj.get("confidence") or "").strip().lower()
    if confidence not in _CONFIDENCE:
        confidence = "medium"

    return ExtractedOutcome(
        stage=stage,
        outcome=outcome,
        reason=_clean(obj.get("reason")),
        title=_clean(obj.get("title")),
        company=_clean(obj.get("company")),
        confidence=confidence,
        extracted_by="llm",
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_extract.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/outcome_extract.py tests/test_outcome_extract.py
git commit -m "feat(outcomes): LLM outcome extractor with heuristic fallback"
```

---

### Task 3: `outcome_scan` — build + upsert email events (idempotent)

**Files:**
- Create: `src/applypilot/outcome_scan.py`
- Test: `tests/test_outcome_scan.py`

**Interfaces:**
- Produces:
  - `build_email_event(msg: dict, applied_jobs: list[dict], *, client=None) -> dict` — `msg` keys: `message_id, thread_id, subject, sender, date, body`. Returns a full `email_events` row dict.
  - `upsert_email_event(conn, row: dict, *, reextract: bool = False) -> str` — returns `"inserted" | "skipped" | "updated"`; idempotent on `message_id`.
  - `scan_outcomes(days: int = 30, *, credentials_path=None, client=None, reextract: bool = False, conn=None, fetch_messages=None) -> dict` — counts dict. `fetch_messages` is an injectable callable `() -> list[dict]` (default fetches from Gmail) used to keep the network out of tests.
- Consumes: `gmail_outcomes.get_applied_jobs`, `gmail_outcomes.match_email_to_job`, `gmail_outcomes._extract_domain`, `outcome_extract.extract_outcome`, `database.get_connection`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outcome_scan.py
import applypilot.outcome_scan as S
from applypilot import database


class FakeClient:
    def __init__(self, reply): self._reply = reply
    def chat(self, messages, **kw): return self._reply


def _seed_applied_job(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, site, application_url, apply_status, applied_at) "
        "VALUES (?,?,?,?,?,?,?)",
        ("https://boards.greenhouse.io/acme/jobs/1", "Quant Analyst", "Acme", "Acme",
         "https://boards.greenhouse.io/acme/jobs/1", "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.commit()


def test_build_email_event_matches_job_and_extracts(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_applied_job(conn)
    applied = [dict(r) for r in conn.execute("SELECT * FROM jobs").fetchall()]
    msg = {
        "message_id": "m1", "thread_id": "t1",
        "subject": "Update on your application to Acme",
        "sender": "Acme Careers <careers@acme.com>",
        "date": "Wed, 03 Jun 2026 10:00:00 +0000",
        "body": "We went with another candidate.",
    }
    reply = '{"stage":"rejected","outcome":"rejected","reason":"chose another candidate","title":"Quant Analyst","company":"Acme","confidence":"high"}'
    row = S.build_email_event(msg, applied, client=FakeClient(reply))
    assert row["message_id"] == "m1"
    assert row["stage"] == "rejected"
    assert row["outcome"] == "rejected"
    assert row["reason"] == "chose another candidate"
    assert row["sender_domain"] == "acme.com"
    assert row["occurred_at"].startswith("2026-06-03")
    assert row["job_url"] == "https://boards.greenhouse.io/acme/jobs/1"


def test_upsert_is_idempotent(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    row = {
        "message_id": "m1", "thread_id": "t1", "job_url": None,
        "occurred_at": "2026-06-03T10:00:00+00:00", "sender": "x@y.com",
        "sender_domain": "y.com", "subject": "s", "stage": "acknowledged",
        "outcome": None, "reason": None, "title": None, "company": None,
        "match_method": None, "match_score": None, "confidence": "low",
        "body_text": "b", "snippet": "b", "extracted_by": "llm",
        "scanned_at": "2026-06-29T00:00:00+00:00",
    }
    assert S.upsert_email_event(conn, row) == "inserted"
    assert S.upsert_email_event(conn, row) == "skipped"
    assert conn.execute("SELECT COUNT(*) FROM email_events").fetchone()[0] == 1
    assert S.upsert_email_event(conn, {**row, "stage": "offer"}, reextract=True) == "updated"
    assert conn.execute("SELECT stage FROM email_events WHERE message_id='m1'").fetchone()[0] == "offer"


def test_scan_outcomes_uses_injected_fetch(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_applied_job(conn)
    monkeypatch.setattr(S, "get_connection", lambda: conn)
    messages = [{
        "message_id": "m1", "thread_id": "t1",
        "subject": "Interview invitation — Quant Analyst at Acme",
        "sender": "careers@acme.com",
        "date": "Wed, 03 Jun 2026 10:00:00 +0000",
        "body": "Please pick a time on the calendly link.",
    }]
    reply = '{"stage":"interview","outcome":null,"reason":null,"title":"Quant Analyst","company":"Acme","confidence":"high"}'
    counts = S.scan_outcomes(client=FakeClient(reply), fetch_messages=lambda: messages, conn=conn)
    assert counts["inserted"] == 1
    counts2 = S.scan_outcomes(client=FakeClient(reply), fetch_messages=lambda: messages, conn=conn)
    assert counts2["skipped"] == 1
    assert counts2["inserted"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_scan.py -v`
Expected: FAIL — `No module named 'applypilot.outcome_scan'`.

- [ ] **Step 3: Create `src/applypilot/outcome_scan.py`**

```python
"""Scan Gmail for application-outcome emails and persist them to email_events.

Reuses the read-only Gmail OAuth, job-matching, and email-parsing already in
gmail_outcomes; adds LLM extraction (outcome_extract) and an idempotent upsert.
The Gmail fetch is injectable (`fetch_messages`) so the pipeline is testable
without the network."""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from applypilot.database import get_connection
from applypilot.gmail_outcomes import (
    _extract_domain,
    get_applied_jobs,
    match_email_to_job,
)
from applypilot.outcome_extract import extract_outcome


def _occurred_at(date_header: str) -> str:
    """Parse an RFC-2822 Date header into ISO-8601 (UTC). Falls back to now."""
    try:
        dt = parsedate_to_datetime(date_header)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def build_email_event(msg: dict, applied_jobs: list[dict], *, client=None) -> dict:
    """Turn one fetched message into a full email_events row dict."""
    subject = msg.get("subject", "") or ""
    sender = msg.get("sender", "") or ""
    body = msg.get("body", "") or ""

    ex = extract_outcome(subject, body, sender, client=client)
    job, method, score = match_email_to_job(sender, subject, body, applied_jobs)

    return {
        "message_id": msg["message_id"],
        "thread_id": msg.get("thread_id"),
        "job_url": job.get("url") if job else None,
        "occurred_at": _occurred_at(msg.get("date", "")),
        "sender": sender,
        "sender_domain": _extract_domain(sender),
        "subject": subject,
        "stage": ex.stage,
        "outcome": ex.outcome,
        "reason": ex.reason,
        "title": ex.title or (job.get("title") if job else None),
        "company": ex.company or (job.get("company") if job else None),
        "match_method": method,
        "match_score": score,
        "confidence": ex.confidence,
        "body_text": body[:20000],
        "snippet": body[:300].replace("\n", " ").strip(),
        "extracted_by": ex.extracted_by,
        "scanned_at": datetime.now(timezone.utc).isoformat(),
    }


_COLUMNS = (
    "message_id", "thread_id", "job_url", "occurred_at", "sender", "sender_domain",
    "subject", "stage", "outcome", "reason", "title", "company", "match_method",
    "match_score", "confidence", "body_text", "snippet", "extracted_by", "scanned_at",
)


def upsert_email_event(conn, row: dict, *, reextract: bool = False) -> str:
    """Insert a new email_events row. If the message_id exists, skip (default)
    or overwrite (reextract=True). Returns 'inserted' | 'skipped' | 'updated'."""
    existing = conn.execute(
        "SELECT 1 FROM email_events WHERE message_id = ?", (row["message_id"],)
    ).fetchone()
    if existing and not reextract:
        return "skipped"

    values = [row.get(c) for c in _COLUMNS]
    if existing:
        assignments = ", ".join(f"{c} = ?" for c in _COLUMNS if c != "message_id")
        conn.execute(
            f"UPDATE email_events SET {assignments} WHERE message_id = ?",
            [row.get(c) for c in _COLUMNS if c != "message_id"] + [row["message_id"]],
        )
        conn.commit()
        return "updated"

    placeholders = ", ".join("?" for _ in _COLUMNS)
    conn.execute(
        f"INSERT INTO email_events ({', '.join(_COLUMNS)}) VALUES ({placeholders})",
        values,
    )
    conn.commit()
    return "inserted"


def _gmail_fetch(days: int, credentials_path: Path | None) -> Callable[[], list[dict]]:
    """Default fetch: pull candidate messages from Gmail (read-only). Returns a
    thunk so the network call is deferred until scan time."""
    def _fetch() -> list[dict]:
        from applypilot.gmail_outcomes import build_gmail_service, _get_text_body, _search_query
        service = build_gmail_service(credentials_path=credentials_path)
        query = _search_query(days)
        resp = service.users().messages().list(userId="me", q=query, maxResults=200).execute()
        out: list[dict] = []
        seen: set[str] = set()
        for ref in resp.get("messages", []):
            tid = ref.get("threadId", ref["id"])
            if tid in seen:
                continue
            seen.add(tid)
            full = service.users().messages().get(userId="me", id=ref["id"], format="full").execute()
            headers = {h["name"].lower(): h["value"] for h in full.get("payload", {}).get("headers", [])}
            out.append({
                "message_id": ref["id"],
                "thread_id": tid,
                "subject": headers.get("subject", ""),
                "sender": headers.get("from", ""),
                "date": headers.get("date", ""),
                "body": _get_text_body(full.get("payload", {})),
            })
        return out
    return _fetch


def scan_outcomes(
    days: int = 30,
    *,
    credentials_path: Path | None = None,
    client=None,
    reextract: bool = False,
    conn=None,
    fetch_messages: Callable[[], list[dict]] | None = None,
) -> dict[str, int]:
    """Scan candidate emails and upsert them into email_events. Returns counts."""
    if conn is None:
        conn = get_connection()
    fetch = fetch_messages or _gmail_fetch(days, credentials_path)

    applied_jobs = get_applied_jobs()
    counts = {"inserted": 0, "skipped": 0, "updated": 0, "errors": 0}
    for msg in fetch():
        try:
            row = build_email_event(msg, applied_jobs, client=client)
            counts[upsert_email_event(conn, row, reextract=reextract)] += 1
        except Exception:
            counts["errors"] += 1
    return counts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_scan.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/outcome_scan.py tests/test_outcome_scan.py
git commit -m "feat(outcomes): idempotent Gmail outcome scanner -> email_events"
```

---

### Task 4: `outcome_timeline` — per-application timeline + metrics (pure)

**Files:**
- Create: `src/applypilot/outcome_timeline.py`
- Test: `tests/test_outcome_timeline.py`

**Interfaces:**
- Produces: `build_timeline(applied_at: str | None, events: list[dict], now_iso: str) -> dict` with keys `ordered, responded, positive, current_stage, outcome, first_response_days, decision_days, decision_stage, silent_days, last_at`.
- `events` items use the `email_events` shape (need `occurred_at`, `stage`, `outcome`).
- Constants: `RESPONSE_STAGES = ("screen","assessment","interview","offer","rejected")`, `POSITIVE_STAGES = ("screen","assessment","interview","offer")`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outcome_timeline.py
from applypilot.outcome_timeline import build_timeline


def _ev(occurred_at, stage, outcome=None):
    return {"occurred_at": occurred_at, "stage": stage, "outcome": outcome}


def test_empty_events_is_silent():
    t = build_timeline("2026-06-01T00:00:00+00:00", [], now_iso="2026-06-20T00:00:00+00:00")
    assert t["responded"] is False
    assert t["current_stage"] == "applied"
    assert t["outcome"] is None
    assert t["silent_days"] == 19
    assert t["first_response_days"] is None


def test_acknowledged_only_is_not_a_response():
    t = build_timeline("2026-06-01T00:00:00+00:00",
                       [_ev("2026-06-02T00:00:00+00:00", "acknowledged")],
                       now_iso="2026-06-10T00:00:00+00:00")
    assert t["responded"] is False
    assert t["current_stage"] == "acknowledged"


def test_full_path_to_offer_computes_latencies():
    events = [
        _ev("2026-06-02T00:00:00+00:00", "acknowledged"),
        _ev("2026-06-05T00:00:00+00:00", "screen"),
        _ev("2026-06-12T00:00:00+00:00", "interview"),
        _ev("2026-06-16T00:00:00+00:00", "offer", outcome="offer"),
    ]
    t = build_timeline("2026-06-01T00:00:00+00:00", events, now_iso="2026-06-20T00:00:00+00:00")
    assert t["responded"] is True
    assert t["positive"] is True
    assert t["outcome"] == "offer"
    assert t["decision_stage"] == "offer"
    assert t["first_response_days"] == 4   # applied 06-01 -> first non-ack (screen) 06-05
    assert t["decision_days"] == 15        # applied 06-01 -> offer 06-16
    assert t["ordered"][0]["stage"] == "acknowledged"


def test_events_are_sorted_by_time():
    events = [
        _ev("2026-06-16T00:00:00+00:00", "rejected", outcome="rejected"),
        _ev("2026-06-02T00:00:00+00:00", "acknowledged"),
    ]
    t = build_timeline("2026-06-01T00:00:00+00:00", events, now_iso="2026-06-20T00:00:00+00:00")
    assert [e["stage"] for e in t["ordered"]] == ["acknowledged", "rejected"]
    assert t["outcome"] == "rejected"
    assert t["decision_stage"] == "rejected"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_timeline.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Create `src/applypilot/outcome_timeline.py`**

```python
"""Pure per-application timeline + latency metrics over email_events rows."""

from __future__ import annotations

from datetime import datetime

RESPONSE_STAGES = ("screen", "assessment", "interview", "offer", "rejected")
POSITIVE_STAGES = ("screen", "assessment", "interview", "offer")


def _dt(iso: str | None):
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _days(a: str | None, b: str | None) -> int | None:
    da, db = _dt(a), _dt(b)
    if da is None or db is None:
        return None
    return int((db - da).total_seconds() // 86400)


def build_timeline(applied_at: str | None, events: list[dict], now_iso: str) -> dict:
    """Build the ordered timeline and derived metrics for one application.

    responded = at least one event in RESPONSE_STAGES (a receipt alone does not count).
    positive  = reached any POSITIVE_STAGE or got an offer.
    """
    ordered = sorted(events, key=lambda e: e.get("occurred_at") or "")

    responses = [e for e in ordered if e.get("stage") in RESPONSE_STAGES]
    decisions = [e for e in ordered if e.get("outcome") in ("offer", "rejected")]
    positive = any(e.get("stage") in POSITIVE_STAGES for e in ordered) or \
        any(e.get("outcome") == "offer" for e in ordered)

    first_response_days = _days(applied_at, responses[0]["occurred_at"]) if responses else None
    decision = decisions[0] if decisions else None
    decision_days = _days(applied_at, decision["occurred_at"]) if decision else None

    current_stage = ordered[-1]["stage"] if ordered else "applied"
    last_at = ordered[-1]["occurred_at"] if ordered else applied_at
    silent_days = _days(last_at, now_iso)

    return {
        "ordered": ordered,
        "responded": bool(responses),
        "positive": positive,
        "current_stage": current_stage,
        "outcome": decision["outcome"] if decision else None,
        "decision_stage": decision["stage"] if decision else None,
        "first_response_days": first_response_days,
        "decision_days": decision_days,
        "silent_days": silent_days,
        "last_at": last_at,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_timeline.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/outcome_timeline.py tests/test_outcome_timeline.py
git commit -m "feat(outcomes): pure timeline + latency metrics"
```

---

### Task 5: `lane_insights` — segmentation + guarded warm/cold flags (pure)

**Files:**
- Create: `src/applypilot/lane_insights.py`
- Test: `tests/test_lane_insights.py`

**Interfaces:**
- Produces:
  - `derive_segments(job: dict) -> dict[str, str]` — keys `source_board, role_family, seniority, score_band, fit_gap_category, location_bucket, salary_band`.
  - `wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]`.
  - `compute_lane_insights(apps: list[dict], *, floor: int = 8) -> dict` — `apps` items: `{responded: bool, positive: bool, segments: dict}`. Returns `{baseline_response_rate, baseline_positive_rate, n, segments: [LaneStat...]}`. Each LaneStat: `{dimension, value, n_applied, n_responded, response_rate, ci_low, ci_high, n_positive, positive_rate, flag}` where `flag ∈ {"warm","cold","none","insufficient"}`.
- Flag rule: `insufficient` if `n_applied < floor`; else `warm` if `ci_low > baseline_response_rate`, `cold` if `ci_high < baseline_response_rate`, else `none`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_lane_insights.py
from applypilot.lane_insights import compute_lane_insights, derive_segments, wilson_interval


def test_derive_segments_buckets_score_and_title():
    seg = derive_segments({
        "source_board": "greenhouse", "title": "Senior Quant Analyst",
        "fit_score": 8, "audit_score": None, "location": "Remote",
        "salary": "$200,000", "fit_gap_category": "stretch",
    })
    assert seg["source_board"] == "greenhouse"
    assert seg["score_band"] == "8+"
    assert seg["seniority"] == "senior"
    assert seg["role_family"] == "quant"
    assert seg["location_bucket"] == "remote"


def test_wilson_interval_widens_for_small_n():
    lo_small, hi_small = wilson_interval(1, 2)
    lo_big, hi_big = wilson_interval(50, 100)
    assert (hi_small - lo_small) > (hi_big - lo_big)


def test_thin_segment_is_insufficient_not_flagged():
    apps = [{"responded": True, "positive": True, "segments": {"source_board": "tinyboard"}}]
    apps += [{"responded": False, "positive": False, "segments": {"source_board": "bulk"}} for _ in range(20)]
    out = compute_lane_insights(apps, floor=8)
    tiny = next(s for s in out["segments"] if s["value"] == "tinyboard")
    assert tiny["flag"] == "insufficient"


def test_strong_segment_flags_warm():
    # 12 responders on "greatboard", 0 elsewhere -> greatboard clearly above baseline.
    apps = [{"responded": True, "positive": True, "segments": {"source_board": "greatboard"}} for _ in range(12)]
    apps += [{"responded": False, "positive": False, "segments": {"source_board": "coldboard"}} for _ in range(40)]
    out = compute_lane_insights(apps, floor=8)
    great = next(s for s in out["segments"] if s["value"] == "greatboard")
    cold = next(s for s in out["segments"] if s["value"] == "coldboard")
    assert great["flag"] == "warm"
    assert cold["flag"] == "cold"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_lane_insights.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Create `src/applypilot/lane_insights.py`**

```python
"""Pure, transparent lane insights: response/positive rates by coarse segment vs
a baseline, with a sample-size floor and Wilson confidence intervals. NO learned
model -- every number is inspectable."""

from __future__ import annotations

import math
import re

_SENIORITY = [
    ("intern", ("intern", "internship")),
    ("lead", ("principal", "staff", "lead", "head of", "director", "vp", "chief")),
    ("senior", ("senior", "sr.", "sr ")),
    ("junior", ("junior", "jr.", "jr ", "associate", "entry")),
]
_ROLE_FAMILY = [
    ("quant", ("quant", "quantitative")),
    ("data", ("data scientist", "data analyst", "data engineer", "analytics")),
    ("software", ("software", "engineer", "developer", "swe")),
    ("research", ("research",)),
    ("product", ("product manager", "product owner")),
    ("trading", ("trader", "trading")),
    ("risk", ("risk",)),
]


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def _score_band(job: dict) -> str:
    score = job.get("audit_score")
    if score is None:
        score = job.get("fit_score")
    if score is None:
        return "unknown"
    score = float(score)
    if score >= 8:
        return "8+"
    if score >= 7:
        return "7"
    if score >= 5:
        return "5-6"
    return "<5"


def _seniority(title: str) -> str:
    for label, kws in _SENIORITY:
        if any(k in title for k in kws):
            return label
    return "mid"


def _role_family(title: str) -> str:
    for label, kws in _ROLE_FAMILY:
        if any(k in title for k in kws):
            return label
    return "other"


def _location_bucket(location: str) -> str:
    if not location:
        return "unknown"
    if "remote" in location:
        return "remote"
    return "onsite"


def _salary_band(job: dict) -> str:
    raw = _norm(job.get("salary"))
    if not raw:
        return "unknown"
    nums = [int(n.replace(",", "")) for n in re.findall(r"\$?\s*([\d,]{4,})", raw)]
    if not nums:
        return "unknown"
    top = max(nums)
    if top >= 200000:
        return "200k+"
    if top >= 150000:
        return "150-200k"
    if top >= 100000:
        return "100-150k"
    return "<100k"


def derive_segments(job: dict) -> dict[str, str]:
    """Coarse segment values for one applied job. High-cardinality fields
    (company, raw site) are intentionally excluded."""
    title = _norm(job.get("title"))
    return {
        "source_board": _norm(job.get("source_board")) or "unknown",
        "role_family": _role_family(title),
        "seniority": _seniority(title),
        "score_band": _score_band(job),
        "fit_gap_category": _norm(job.get("fit_gap_category")) or "unknown",
        "location_bucket": _location_bucket(_norm(job.get("location"))),
        "salary_band": _salary_band(job),
    }


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score 95% interval for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    phat = successes / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    margin = (z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def compute_lane_insights(apps: list[dict], *, floor: int = 8) -> dict:
    """Aggregate response/positive rates by segment vs the overall baseline."""
    n = len(apps)
    base_resp = (sum(1 for a in apps if a["responded"]) / n) if n else 0.0
    base_pos = (sum(1 for a in apps if a["positive"]) / n) if n else 0.0

    # dimension -> value -> [n_applied, n_responded, n_positive]
    cells: dict[tuple[str, str], list[int]] = {}
    for a in apps:
        for dim, val in a["segments"].items():
            key = (dim, val)
            c = cells.setdefault(key, [0, 0, 0])
            c[0] += 1
            c[1] += 1 if a["responded"] else 0
            c[2] += 1 if a["positive"] else 0

    segments = []
    for (dim, val), (n_applied, n_responded, n_positive) in sorted(cells.items()):
        rate = n_responded / n_applied if n_applied else 0.0
        lo, hi = wilson_interval(n_responded, n_applied)
        if n_applied < floor:
            flag = "insufficient"
        elif lo > base_resp:
            flag = "warm"
        elif hi < base_resp:
            flag = "cold"
        else:
            flag = "none"
        segments.append({
            "dimension": dim, "value": val,
            "n_applied": n_applied, "n_responded": n_responded,
            "response_rate": round(rate, 4),
            "ci_low": round(lo, 4), "ci_high": round(hi, 4),
            "n_positive": n_positive,
            "positive_rate": round(n_positive / n_applied, 4) if n_applied else 0.0,
            "flag": flag,
        })

    return {
        "n": n,
        "baseline_response_rate": round(base_resp, 4),
        "baseline_positive_rate": round(base_pos, 4),
        "segments": segments,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_lane_insights.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/lane_insights.py tests/test_lane_insights.py
git commit -m "feat(outcomes): transparent lane insights (Wilson-guarded segments)"
```

---

### Task 6: Dashboard data assembly + CSV (read models joined)

**Files:**
- Create: `src/applypilot/outcome_dashboard.py` (data functions only in this task; the HTTP server is Task 7)
- Test: `tests/test_outcome_dashboard.py`

**Interfaces:**
- Produces:
  - `get_tracked_universe(conn) -> list[dict]` — every job that is `apply_status='applied'` OR has an `applications` row, joined with attributes (`url, title, company, source_board, location, salary, fit_score, audit_score, fit_gap_category, applied_at`).
  - `build_application_rows(conn, *, now_iso: str) -> list[dict]` — each universe job + its `email_events` + timeline metrics + segments. Keys include `job_url, title, company, applied_at, current_stage, outcome, responded, positive, first_response_days, decision_days, silent_days, segments, events`.
  - `build_csv(rows: list[dict]) -> str`.
- Consumes: `outcome_timeline.build_timeline`, `lane_insights.derive_segments`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_outcome_dashboard.py
import csv
import io

from applypilot import database
import applypilot.outcome_scan as S
import applypilot.outcome_dashboard as D


def _seed(conn):
    conn.execute(
        "INSERT INTO jobs (url, title, company, source_board, location, salary, "
        "fit_score, apply_status, applied_at) VALUES (?,?,?,?,?,?,?,?,?)",
        ("https://acme/1", "Senior Quant Analyst", "Acme", "greenhouse", "Remote",
         "$210,000", 8, "applied", "2026-06-01T00:00:00+00:00"),
    )
    conn.commit()
    for row in [
        dict(message_id="m1", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-02T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Thanks for applying", stage="acknowledged",
             outcome=None, reason=None, title="Senior Quant Analyst", company="Acme",
             match_method="company_name", match_score=0.9, confidence="high",
             body_text="b", snippet="b", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
        dict(message_id="m2", thread_id="t1", job_url="https://acme/1",
             occurred_at="2026-06-10T00:00:00+00:00", sender="careers@acme.com",
             sender_domain="acme.com", subject="Next steps", stage="interview",
             outcome=None, reason=None, title="Senior Quant Analyst", company="Acme",
             match_method="company_name", match_score=0.9, confidence="high",
             body_text="b", snippet="b", extracted_by="llm", scanned_at="2026-06-29T00:00:00+00:00"),
    ]:
        S.upsert_email_event(conn, row)


def test_universe_includes_applied_jobs(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)
    uni = D.get_tracked_universe(conn)
    assert [u["url"] for u in uni] == ["https://acme/1"]


def test_application_rows_compute_timeline(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)
    rows = D.build_application_rows(conn, now_iso="2026-06-20T00:00:00+00:00")
    r = rows[0]
    assert r["responded"] is True
    assert r["current_stage"] == "interview"
    assert r["first_response_days"] == 9
    assert r["segments"]["score_band"] == "8+"
    assert len(r["events"]) == 2


def test_build_csv_has_header_and_row(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed(conn)
    rows = D.build_application_rows(conn, now_iso="2026-06-20T00:00:00+00:00")
    text = D.build_csv(rows)
    parsed = list(csv.DictReader(io.StringIO(text)))
    assert parsed[0]["company"] == "Acme"
    assert "first_response_days" in parsed[0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_dashboard.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Create `src/applypilot/outcome_dashboard.py` (data layer)**

```python
"""Read-only data assembly + CSV for the outcomes dashboard. The HTTP server
(serve / _Handler) is added in the next task; these functions are pure-ish reads."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from applypilot.lane_insights import compute_lane_insights, derive_segments
from applypilot.outcome_timeline import build_timeline

_UNIVERSE_SQL = """
    SELECT j.url, j.title, j.company, j.source_board, j.location, j.salary,
           j.fit_score, j.audit_score, j.fit_gap_category, j.applied_at
      FROM jobs j
      LEFT JOIN applications a ON a.job_url = j.url
     WHERE j.apply_status = 'applied' OR a.job_url IS NOT NULL
     ORDER BY COALESCE(j.applied_at, j.discovered_at) DESC
"""

_EVENT_COLS = (
    "message_id", "occurred_at", "stage", "outcome", "reason",
    "sender", "subject", "snippet", "body_text", "confidence", "extracted_by",
)

_CSV_FIELDS = (
    "job_url", "company", "title", "source_board", "applied_at", "current_stage",
    "outcome", "responded", "positive", "first_response_days", "decision_days",
    "silent_days",
)


def get_tracked_universe(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(_UNIVERSE_SQL).fetchall()]


def _events_for(conn, job_url: str) -> list[dict]:
    rows = conn.execute(
        f"SELECT {', '.join(_EVENT_COLS)} FROM email_events "
        "WHERE job_url = ? ORDER BY occurred_at",
        (job_url,),
    ).fetchall()
    return [dict(r) for r in rows]


def build_application_rows(conn, *, now_iso: str | None = None) -> list[dict]:
    now_iso = now_iso or datetime.now(timezone.utc).isoformat()
    rows = []
    for job in get_tracked_universe(conn):
        events = _events_for(conn, job["url"])
        tl = build_timeline(job.get("applied_at"), events, now_iso=now_iso)
        rows.append({
            "job_url": job["url"],
            "title": job.get("title"),
            "company": job.get("company"),
            "source_board": job.get("source_board"),
            "applied_at": job.get("applied_at"),
            "current_stage": tl["current_stage"],
            "outcome": tl["outcome"],
            "responded": tl["responded"],
            "positive": tl["positive"],
            "first_response_days": tl["first_response_days"],
            "decision_days": tl["decision_days"],
            "silent_days": tl["silent_days"],
            "segments": derive_segments(job),
            "events": tl["ordered"],
        })
    return rows


def build_insights(rows: list[dict], *, floor: int = 8) -> dict:
    apps = [{"responded": r["responded"], "positive": r["positive"],
             "segments": r["segments"]} for r in rows]
    return compute_lane_insights(apps, floor=floor)


def build_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k) for k in _CSV_FIELDS})
    return buf.getvalue()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_dashboard.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/outcome_dashboard.py tests/test_outcome_dashboard.py
git commit -m "feat(outcomes): dashboard data assembly + CSV export"
```

---

### Task 7: Dashboard HTTP server (stdlib, read-only, localhost)

**Files:**
- Modify: `src/applypilot/outcome_dashboard.py` (add `serve`, `_Handler`, `_read_only_conn`, `_PAGE`)
- Test: `tests/test_outcome_dashboard.py` (add a live-server smoke test)

**Interfaces:**
- Produces: `serve(host: str = "127.0.0.1", port: int = 8765, db_path=None) -> None`; `_read_only_conn(db_path) -> sqlite3.Connection`. Endpoints: `GET /` (HTML), `GET /api/data` (JSON: `{rows, insights}`), `GET /export.csv`.
- Consumes: `build_application_rows`, `build_insights`, `build_csv`, `applypilot.config.DB_PATH`.

- [ ] **Step 1: Write the failing test (live smoke test on an ephemeral port)**

```python
# tests/test_outcome_dashboard.py  (append)
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import applypilot.outcome_dashboard as D
from applypilot import database


def test_server_serves_json_and_csv(tmp_path):
    db = tmp_path / "applypilot.db"
    conn = database.init_db(db)
    _seed(conn)
    conn.commit()

    handler = D._make_handler(str(db))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/data") as resp:
            data = json.loads(resp.read())
        assert data["rows"][0]["company"] == "Acme"
        assert "insights" in data
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/export.csv") as resp:
            body = resp.read().decode()
        assert "company" in body.splitlines()[0]
    finally:
        server.shutdown()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_dashboard.py::test_server_serves_json_and_csv -v`
Expected: FAIL — `_make_handler` not defined.

- [ ] **Step 3: Append the server to `src/applypilot/outcome_dashboard.py`**

```python
import ipaddress
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from applypilot.config import DB_PATH

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>ApplyPilot Outcomes</title>
<style>
 body{font:14px system-ui;margin:1.5rem;color:#111}
 table{border-collapse:collapse;width:100%}th,td{border-bottom:1px solid #ddd;padding:6px 8px;text-align:left}
 th{cursor:pointer;background:#f6f6f6}.warm{color:#0a7a0a;font-weight:600}.cold{color:#b00}.pill{font-size:12px;padding:1px 6px;border-radius:8px;background:#eee}
 tr.detail td{background:#fafafa;font-size:13px}details{margin:2px 0}
</style></head><body>
<h2>ApplyPilot Outcomes</h2>
<div id="summary"></div>
<h3>Applications</h3>
<table id="apps"><thead><tr>
 <th>Company</th><th>Title</th><th>Board</th><th>Applied</th><th>Stage</th>
 <th>Outcome</th><th>1st reply (d)</th><th>Decision (d)</th><th>Silent (d)</th>
</tr></thead><tbody></tbody></table>
<h3>Lane insights</h3><div id="insights"></div>
<p><a href="/export.csv">Download CSV</a></p>
<script>
async function load(){
 const d = await (await fetch('/api/data')).json();
 const tb = document.querySelector('#apps tbody');
 for(const r of d.rows){
  const tr=document.createElement('tr');
  tr.innerHTML=`<td>${r.company||''}</td><td>${r.title||''}</td><td>${r.source_board||''}</td>
   <td>${(r.applied_at||'').slice(0,10)}</td><td>${r.current_stage}</td><td>${r.outcome||''}</td>
   <td>${r.first_response_days??''}</td><td>${r.decision_days??''}</td><td>${r.silent_days??''}</td>`;
  tb.appendChild(tr);
  if(r.events.length){
   const dt=document.createElement('tr');dt.className='detail';
   const td=document.createElement('td');td.colSpan=9;
   td.innerHTML=r.events.map(e=>`<details><summary>${(e.occurred_at||'').slice(0,10)} · ${e.stage} · ${e.subject||''}</summary>
     <div><b>Reason:</b> ${e.reason||'—'}</div><pre style="white-space:pre-wrap">${(e.body_text||'').slice(0,2000)}</pre></details>`).join('');
   dt.appendChild(td);tb.appendChild(dt);
  }
 }
 const ins=d.insights;
 document.querySelector('#summary').innerHTML=
   `<span class="pill">${ins.n} applications</span>
    <span class="pill">baseline reply rate ${(ins.baseline_response_rate*100).toFixed(0)}%</span>`;
 const warm=ins.segments.filter(s=>s.flag==='warm'), cold=ins.segments.filter(s=>s.flag==='cold');
 const fmt=s=>`<li><span class="${s.flag}">${s.dimension}=${s.value}</span> — ${(s.response_rate*100).toFixed(0)}% reply (${s.n_responded}/${s.n_applied}, CI ${(s.ci_low*100).toFixed(0)}–${(s.ci_high*100).toFixed(0)}%)</li>`;
 document.querySelector('#insights').innerHTML=
   `<b>Warm lanes</b><ul>${warm.map(fmt).join('')||'<li>none yet</li>'}</ul>
    <b>Cold lanes</b><ul>${cold.map(fmt).join('')||'<li>none yet</li>'}</ul>`;
}
load();
</script></body></html>"""


def _read_only_conn(db_path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _make_handler(db_path: str):
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # silence default stderr logging
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/" or self.path.startswith("/index"):
                return self._send(200, _PAGE.encode(), "text/html; charset=utf-8")
            conn = _read_only_conn(db_path)
            try:
                rows = build_application_rows(conn)
                if self.path.startswith("/api/data"):
                    payload = {"rows": rows, "insights": build_insights(rows)}
                    return self._send(200, json.dumps(payload, default=str).encode(),
                                      "application/json")
                if self.path.startswith("/export.csv"):
                    return self._send(200, build_csv(rows).encode(),
                                      "text/csv; charset=utf-8")
                return self._send(404, b"not found", "text/plain")
            finally:
                conn.close()
    return _Handler


def serve(host: str = "127.0.0.1", port: int = 8765, db_path=None) -> None:
    """Serve the read-only outcomes dashboard. Binds loopback/private IPs only."""
    ip = ipaddress.ip_address(host)
    if not (ip.is_loopback or ip.is_private):
        raise ValueError(f"refusing to bind non-private host {host}")
    db_path = str(db_path or DB_PATH)
    server = ThreadingHTTPServer((host, port), _make_handler(db_path))
    print(f"Outcomes dashboard: http://{host}:{port}  (Ctrl-C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()
```

- [ ] **Step 4: Run the full dashboard test file to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_dashboard.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/applypilot/outcome_dashboard.py tests/test_outcome_dashboard.py
git commit -m "feat(outcomes): stdlib read-only dashboard server (localhost) + insights"
```

---

### Task 8: CLI wiring — `outcomes-scan` and `outcomes-dashboard`

**Files:**
- Modify: `src/applypilot/cli.py` (add two flat commands after `scan-gmail`, ~line 1577)
- Test: `tests/test_outcomes_cli.py`

**Interfaces:**
- Consumes: `outcome_scan.scan_outcomes`, `outcome_dashboard.serve`, the existing `app`, `console`, `_bootstrap`, Rich `Table`.
- Produces: CLI commands `applypilot outcomes-scan` and `applypilot outcomes-dashboard`.

- [ ] **Step 1: Write the failing test (Typer CliRunner, scan mocked)**

```python
# tests/test_outcomes_cli.py
from typer.testing import CliRunner

import applypilot.cli as cli

runner = CliRunner()


def test_outcomes_scan_renders_counts(monkeypatch):
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    import applypilot.outcome_scan as S
    monkeypatch.setattr(S, "scan_outcomes",
                        lambda **kw: {"inserted": 2, "skipped": 1, "updated": 0, "errors": 0})
    result = runner.invoke(cli.app, ["outcomes-scan", "--days", "10"])
    assert result.exit_code == 0
    assert "2" in result.stdout


def test_outcomes_dashboard_invokes_serve(monkeypatch):
    monkeypatch.setattr(cli, "_bootstrap", lambda: None)
    called = {}
    import applypilot.outcome_dashboard as D
    monkeypatch.setattr(D, "serve", lambda **kw: called.update(kw))
    result = runner.invoke(cli.app, ["outcomes-dashboard", "--port", "9999", "--no-open"])
    assert result.exit_code == 0
    assert called.get("port") == 9999
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.conda-env/python.exe -m pytest tests/test_outcomes_cli.py -v`
Expected: FAIL — no such command `outcomes-scan`.

- [ ] **Step 3: Add the commands to `src/applypilot/cli.py` (after the `scan-gmail` command, ~line 1577)**

```python
@app.command("outcomes-scan")
def outcomes_scan_command(
    days: int = typer.Option(30, "--days", "-d", help="How many days back to search."),
    reextract: bool = typer.Option(False, "--reextract", help="Re-run LLM extraction on already-seen emails."),
    credentials: Optional[Path] = typer.Option(None, "--credentials", help="Path to gmail_credentials.json."),
) -> None:
    """Scan Gmail and populate the email_events outcome timeline (LLM extraction)."""
    _bootstrap()
    from applypilot.outcome_scan import scan_outcomes
    try:
        counts = scan_outcomes(days=days, credentials_path=credentials, reextract=reextract)
    except FileNotFoundError as exc:
        console.print(f"[red]Setup required:[/red]\n{exc}")
        raise typer.Exit(1)
    except ImportError as exc:
        console.print(f"[red]Missing dependencies:[/red] {exc}")
        raise typer.Exit(1)

    table = Table(title="Outcome scan", show_header=True, header_style="bold")
    table.add_column("Result", style="bold")
    table.add_column("Count", justify="right")
    for k in ("inserted", "updated", "skipped", "errors"):
        table.add_row(k, str(counts.get(k, 0)))
    console.print(table)


@app.command("outcomes-dashboard")
def outcomes_dashboard_command(
    port: int = typer.Option(8765, "--port", "-p", help="Port to serve on (localhost)."),
    host: str = typer.Option("127.0.0.1", "--host", help="Host to bind (loopback/private only)."),
    open_browser: bool = typer.Option(True, "--open/--no-open", help="Open the browser."),
) -> None:
    """Serve the local read-only outcomes dashboard (timeline, analytics, lanes)."""
    _bootstrap()
    from applypilot.outcome_dashboard import serve
    if open_browser:
        import webbrowser
        webbrowser.open(f"http://{host}:{port}")
    serve(host=host, port=port)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.conda-env/python.exe -m pytest tests/test_outcomes_cli.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the full new test suite**

Run: `.conda-env/python.exe -m pytest tests/test_outcome_schema.py tests/test_outcome_extract.py tests/test_outcome_scan.py tests/test_outcome_timeline.py tests/test_lane_insights.py tests/test_outcome_dashboard.py tests/test_outcomes_cli.py -v`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add src/applypilot/cli.py tests/test_outcomes_cli.py
git commit -m "feat(outcomes): outcomes-scan + outcomes-dashboard CLI commands"
```

---

## Manual verification (after all tasks)

1. Backfill + scan your real inbox (dry-run on the tracker; this only writes `email_events`):
   `applypilot outcomes-scan --days 365`
   (Run with `APPLYPILOT_DIR` unset so creds resolve to `~/.applypilot`, per the scan-gmail invocation gotcha.)
2. Open the dashboard: `applypilot outcomes-dashboard` → browse applications, expand a row to read each email + reason, check the warm/cold lanes and the CSV export.
3. Spot-check extraction quality on ~10 known emails; if a model is misclassifying, set `LLM_OUTCOME_EXTRACT_MODEL` / `LLM_OUTCOME_EXTRACT_PROVIDER` (or point them at a local endpoint for fully-local extraction).

## Deferred (NOT in this plan)

- **Promotion** of confident `offer`/`rejected` email_events into the `applications` tracker (a `--apply`-style flag) — keep using the existing `scan-gmail --apply` until this is specced.
- **Denominator reconciliation** beyond the `apply_status='applied' ∪ applications` universe (pulling the fleet Postgres `applied_set` back into the brain).
- **v1.1 prescriptive discovery feedback** (warm lanes → `searches.yaml`) — separate spec.

---

## Self-Review (completed inline)

- **Spec coverage:** read each email + reason/title/timestamp → Tasks 2–3 (`email_events.reason/title/occurred_at`); full timeline + latencies → Task 4; dashboard (table, read-each-email, worklist via `silent_days`, CSV) → Tasks 6–7; descriptive lane insights with sample-floor + Wilson → Task 5; migration on the brain → Task 1; CLI → Task 8; privacy gate + fallback → Task 2; idempotency → Task 3. Promotion + denominator reconciliation + v1.1 are explicitly deferred (match spec §3/§9/§13).
- **Placeholder scan:** none — every code/test step is complete and runnable.
- **Type consistency:** `email_events` column tuple in Task 3 (`_COLUMNS`) matches the Task 1 DDL; `build_timeline` keys consumed in Task 6 match Task 4's return; `derive_segments`/`compute_lane_insights` shapes consumed in Task 6/7 match Task 5; `scan_outcomes` counts keys (`inserted/updated/skipped/errors`) match the Task 8 CLI render.
