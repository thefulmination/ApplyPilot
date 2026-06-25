# Application Question Bank Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a SQLite-backed application question bank that imports pasted application-question dumps, deduplicates canonical questions, creates pending or suggested answer records, and exposes CLI commands to list, answer, and export the queue.

**Architecture:** Add a focused `applypilot.question_bank` module for normalization, classification, persistence, import, answer updates, list, and export behavior. Add idempotent schema creation in `database.py`, one export directory constant in `config.py`, and thin Typer commands in `cli.py` that delegate to the module.

**Tech Stack:** Python 3.11+, SQLite via `sqlite3`, Typer CLI, Rich tables, standard `csv`/`json`, existing ApplyPilot `database.get_connection()`, `config.load_profile()`, and pytest.

---

## File Structure

- Create `src/applypilot/question_bank.py`
  - Owns question text normalization, question detection, deterministic classification, import parsing, SQLite persistence, list/filter helpers, answer updates, and export.
- Modify `src/applypilot/database.py`
  - Add `ensure_question_bank_tables(conn)` and call it from `init_db()`.
- Modify `src/applypilot/config.py`
  - Add `APPLICATION_QUESTION_EXPORT_DIR = APP_DIR / "application_question_exports"` and include it in `ensure_dirs()`.
- Modify `src/applypilot/cli.py`
  - Add `import-questions`, `list-questions`, `answer-question`, and `export-questions` commands. Keep each command thin.
- Create `tests/test_application_question_bank.py`
  - Unit and persistence coverage for normalization, classification, import, idempotency, answer update, and export.
- Create `tests/test_application_question_cli.py`
  - CLI coverage for import/list/answer/export command wiring.

## Task 1: Add Question Bank Schema

**Files:**
- Modify: `src/applypilot/database.py`
- Test: `tests/test_application_question_bank.py`

- [ ] **Step 1: Write failing schema test**

Add this test file:

```python
from __future__ import annotations

from pathlib import Path

from applypilot import database


def test_init_db_creates_application_question_tables(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }

    assert {
        "application_questions",
        "application_question_instances",
        "application_question_answers",
    } <= tables


def test_application_question_schema_indexes(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")

    question_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(application_questions)").fetchall()
    }
    instance_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(application_question_instances)").fetchall()
    }
    answer_indexes = {
        row[1]
        for row in conn.execute("PRAGMA index_list(application_question_answers)").fetchall()
    }

    assert "idx_application_questions_hash" in question_indexes
    assert "idx_application_question_instances_question_id" in instance_indexes
    assert "idx_application_question_instances_company" in instance_indexes
    assert "idx_application_question_answers_question_id" in answer_indexes
```

- [ ] **Step 2: Run test and verify failure**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_bank.py::test_init_db_creates_application_question_tables -v
```

Expected: fails because the tables do not exist.

- [ ] **Step 3: Implement schema helper**

In `src/applypilot/database.py`, add this function after `ensure_application_tables` and before `ensure_inbox_auth_tables`:

```python
def ensure_question_bank_tables(conn: sqlite3.Connection | None = None) -> None:
    """Create durable application question and answer bank tables."""
    if conn is None:
        conn = get_connection()

    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_questions (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            question_hash         TEXT NOT NULL UNIQUE,
            question_text         TEXT NOT NULL,
            normalized_text       TEXT NOT NULL,
            category              TEXT NOT NULL DEFAULT 'other',
            risk_level            TEXT NOT NULL DEFAULT 'sensitive',
            answer_type           TEXT NOT NULL DEFAULT 'unknown',
            first_seen_at         TEXT NOT NULL,
            last_seen_at          TEXT NOT NULL,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_question_instances (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id           INTEGER NOT NULL,
            job_url               TEXT,
            application_url       TEXT,
            company               TEXT,
            job_title             TEXT,
            source                TEXT,
            source_file           TEXT,
            raw_text              TEXT NOT NULL,
            required              INTEGER NOT NULL DEFAULT 0,
            options_json          TEXT,
            seen_at               TEXT NOT NULL,
            created_at            TEXT NOT NULL,
            FOREIGN KEY(question_id) REFERENCES application_questions(id)
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_question_answers (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            question_id           INTEGER NOT NULL UNIQUE,
            answer_text           TEXT,
            answer_status         TEXT NOT NULL DEFAULT 'pending',
            answer_source         TEXT NOT NULL DEFAULT 'unknown',
            confidence            TEXT NOT NULL DEFAULT 'low',
            auto_submit_allowed   INTEGER NOT NULL DEFAULT 0,
            notes                 TEXT,
            approved_at           TEXT,
            approved_by           TEXT,
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            FOREIGN KEY(question_id) REFERENCES application_questions(id)
        )
    """)
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_application_questions_hash ON application_questions(question_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_questions_category ON application_questions(category)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_questions_risk ON application_questions(risk_level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_question_instances_question_id ON application_question_instances(question_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_question_instances_company ON application_question_instances(company)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_question_answers_question_id ON application_question_answers(question_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_application_question_answers_status ON application_question_answers(answer_status)")
    conn.commit()
```

In `init_db()`, after `ensure_application_tables(conn)`, add:

```python
    ensure_question_bank_tables(conn)
```

- [ ] **Step 4: Run schema tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_bank.py::test_init_db_creates_application_question_tables tests/test_application_question_bank.py::test_application_question_schema_indexes -v
```

Expected: both tests pass.

- [ ] **Step 5: Commit schema**

Run:

```powershell
git add src/applypilot/database.py tests/test_application_question_bank.py
git commit -m "Add application question bank schema"
```

## Task 2: Add Normalization and Classification

**Files:**
- Create: `src/applypilot/question_bank.py`
- Modify: `tests/test_application_question_bank.py`

- [ ] **Step 1: Add failing tests for normalization and classification**

Append these tests to `tests/test_application_question_bank.py`:

```python
from applypilot import question_bank


def test_normalize_question_text_dedupes_required_marker_and_spaces() -> None:
    first = "Are you legally able to work in the U.S. without visa sponsorship?\u2009*"
    second = "  Are you legally able to work in the U.S. without visa sponsorship? * "

    assert question_bank.normalize_question_text(first) == question_bank.normalize_question_text(second)
    assert question_bank.display_question_text(first) == "Are you legally able to work in the U.S. without visa sponsorship?"


def test_question_hash_is_stable_for_duplicate_labels() -> None:
    assert question_bank.question_hash("Gender\u2009*") == question_bank.question_hash("Gender *")


def test_classify_work_authorization_question() -> None:
    result = question_bank.classify_question(
        "Will you now, or in the future, require sponsorship for employment visa status?"
    )

    assert result.category == "work_authorization"
    assert result.risk_level == "safe"
    assert result.answer_type == "yes_no"


def test_classify_legal_attestation_question() -> None:
    result = question_bank.classify_question("Have you ever been convicted of a felony?")

    assert result.category == "legal_background"
    assert result.risk_level == "legal_attestation"
    assert result.answer_type == "yes_no"


def test_is_question_like_rejects_page_chrome_and_values() -> None:
    assert not question_bank.is_question_like("Company Logo: IDEA Public Schools")
    assert not question_bank.is_question_like("Step 8 of 10")
    assert not question_bank.is_question_like("Jonathan Stallone")
    assert question_bank.is_question_like("Have you ever been convicted of a felony?\u2009*")
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_bank.py -v
```

Expected: fails because `applypilot.question_bank` does not exist.

- [ ] **Step 3: Create minimal question bank module**

Create `src/applypilot/question_bank.py`:

```python
from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class QuestionClassification:
    category: str
    risk_level: str
    answer_type: str


LEGAL_KEYWORDS = (
    "felony",
    "convicted",
    "conviction",
    "nolo contendere",
    "no contest",
    "deferred adjudication",
    "child abuse",
    "investigation",
    "terminated",
    "non-renewed",
    "discharged",
    "revoked",
    "suspended",
    "do not hire registry",
    "truthful and accurate",
    "affidavit",
    "consent for release of records",
)

SENSITIVE_KEYWORDS = (
    "gender",
    "hispanic or latino",
    "race",
    "disability",
    "veteran",
    "related to",
    "relationship",
    "domestic partnership",
)

SAFE_KEYWORDS = (
    "work in the u.s.",
    "work in the us",
    "work authorization",
    "visa sponsorship",
    "sponsorship",
    "your name",
    "today's date",
    "date",
    "phone",
    "email",
    "linkedin",
)

PAGE_CHROME_PATTERNS = (
    re.compile(r"^company logo:", re.I),
    re.compile(r"^step\s+\d+\s+of\s+\d+$", re.I),
    re.compile(r"^[A-Z][a-z]+ [A-Z][a-z]+$"),
    re.compile(r"^[A-Z][a-z]+,\s+[A-Z][a-z]+,\s+United States$", re.I),
)


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def display_question_text(text: str) -> str:
    cleaned = unicodedata.normalize("NFKC", text or "")
    cleaned = cleaned.replace("\u2009", " ").replace("\u00a0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = re.sub(r"\s*\*\s*$", "", cleaned).strip()
    return cleaned


def normalize_question_text(text: str) -> str:
    cleaned = display_question_text(text)
    cleaned = cleaned.casefold()
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def question_hash(text: str) -> str:
    normalized = normalize_question_text(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def is_question_like(text: str) -> bool:
    display = display_question_text(text)
    if not display:
        return False
    for pattern in PAGE_CHROME_PATTERNS:
        if pattern.search(display):
            return False
    lower = display.casefold()
    if "?" in display:
        return True
    if text.rstrip().endswith("*"):
        return True
    if lower in {"gender", "your name", "today's date", "date", "name", "choose one"}:
        return True
    if lower.startswith("please select") or lower.startswith("please list") or lower.startswith("please check"):
        return True
    return False


def _contains_any(value: str, terms: tuple[str, ...]) -> bool:
    return any(term in value for term in terms)


def classify_question(text: str) -> QuestionClassification:
    normalized = normalize_question_text(text)

    if _contains_any(normalized, LEGAL_KEYWORDS):
        category = "education_affidavit" if "affidavit" in normalized or "tec " in normalized else "legal_background"
        answer_type = "signature" if "typing your name" in normalized or "truthful and accurate" in normalized else "yes_no"
        return QuestionClassification(category=category, risk_level="legal_attestation", answer_type=answer_type)

    if _contains_any(normalized, SENSITIVE_KEYWORDS):
        category = "eeo" if any(term in normalized for term in ("gender", "hispanic", "race", "disability", "veteran")) else "relationship_disclosure"
        answer_type = "multi_select" if "select all" in normalized else "yes_no"
        return QuestionClassification(category=category, risk_level="sensitive", answer_type=answer_type)

    if _contains_any(normalized, SAFE_KEYWORDS):
        if "date" in normalized:
            return QuestionClassification(category="date", risk_level="safe", answer_type="date")
        if "name" in normalized:
            return QuestionClassification(category="name", risk_level="safe", answer_type="text")
        return QuestionClassification(category="work_authorization", risk_level="safe", answer_type="yes_no")

    if normalized.startswith("why ") or "tell us" in normalized or "describe" in normalized:
        return QuestionClassification(category="narrative", risk_level="sensitive", answer_type="text")

    return QuestionClassification(category="other", risk_level="sensitive", answer_type="unknown")
```

- [ ] **Step 4: Run tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_bank.py -v
```

Expected: all tests in this file pass.

- [ ] **Step 5: Commit normalization and classification**

Run:

```powershell
git add src/applypilot/question_bank.py tests/test_application_question_bank.py
git commit -m "Add application question normalization"
```

## Task 3: Add Import Persistence and Profile-Backed Suggestions

**Files:**
- Modify: `src/applypilot/question_bank.py`
- Modify: `tests/test_application_question_bank.py`

- [ ] **Step 1: Add failing import tests**

Append these tests:

```python
def test_import_questions_dedupes_and_creates_answers(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    source = tmp_path / "pasted-text.txt"
    source.write_text(
        "\n".join(
            [
                "Company Logo: IDEA Public Schools",
                "Are you legally able to work in the U.S. without visa sponsorship?\u2009*",
                "Are you legally able to work in the U.S. without visa sponsorship?\u2009*",
                "Have you ever been convicted of a felony?\u2009*",
                "Have you ever been convicted of a felony?\u2009*",
                "Jonathan Stallone",
            ]
        ),
        encoding="utf-8",
    )
    profile = {
        "work_authorization": {
            "legally_authorized_to_work": "Yes",
            "require_sponsorship": "No",
        },
        "eeo_voluntary": {},
        "personal": {"full_name": "Jonathan Stallone"},
    }

    result = question_bank.import_questions_from_file(
        conn,
        source,
        company="IDEA Public Schools",
        job_title="Chief of Staff - Finance (26-27)",
        job_url="https://example.com/job",
        application_url="https://example.com/apply",
        profile=profile,
    )

    assert result["raw_lines"] == 6
    assert result["question_like_lines"] == 4
    assert result["canonical_created"] == 2
    assert result["duplicates_seen"] == 2
    assert result["answers_created"] == 2

    questions = conn.execute("SELECT question_text, category, risk_level FROM application_questions ORDER BY id").fetchall()
    assert [row["question_text"] for row in questions] == [
        "Are you legally able to work in the U.S. without visa sponsorship?",
        "Have you ever been convicted of a felony?",
    ]

    answers = conn.execute(
        """
        SELECT q.question_text, a.answer_text, a.answer_status, a.answer_source, a.auto_submit_allowed
          FROM application_question_answers a
          JOIN application_questions q ON q.id = a.question_id
         ORDER BY q.id
        """
    ).fetchall()
    assert answers[0]["answer_text"] == "Yes"
    assert answers[0]["answer_status"] == "suggested"
    assert answers[0]["answer_source"] == "profile"
    assert answers[0]["auto_submit_allowed"] == 0
    assert answers[1]["answer_text"] is None
    assert answers[1]["answer_status"] == "manual_only"


def test_import_questions_is_idempotent_for_canonical_questions(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    source = tmp_path / "questions.txt"
    source.write_text("Will you now, or in the future, require sponsorship for employment visa status?\u2009*\n", encoding="utf-8")

    question_bank.import_questions_from_file(conn, source, company="A", profile={"work_authorization": {"require_sponsorship": "No"}})
    question_bank.import_questions_from_file(conn, source, company="A", profile={"work_authorization": {"require_sponsorship": "No"}})

    assert conn.execute("SELECT COUNT(*) FROM application_questions").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM application_question_answers").fetchone()[0] == 1
```

- [ ] **Step 2: Run import tests and verify failure**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_bank.py::test_import_questions_dedupes_and_creates_answers -v
```

Expected: fails because `import_questions_from_file` does not exist.

- [ ] **Step 3: Add import dataclass and suggestion helpers**

Add these imports at the top of `src/applypilot/question_bank.py`:

```python
import json
import sqlite3
from pathlib import Path
from typing import Any
```

Add these functions below `classify_question`:

```python
def _profile_suggestion(question_text: str, classification: QuestionClassification, profile: dict[str, Any] | None) -> tuple[str | None, str, str, int]:
    normalized = normalize_question_text(question_text)
    profile = profile or {}
    work_auth = profile.get("work_authorization", {}) if isinstance(profile, dict) else {}
    eeo = profile.get("eeo_voluntary", {}) if isinstance(profile, dict) else {}
    personal = profile.get("personal", {}) if isinstance(profile, dict) else {}

    if classification.risk_level == "legal_attestation":
        return None, "manual_only", "manual", 0

    if classification.category == "work_authorization":
        if "require sponsorship" in normalized or "sponsorship for employment visa" in normalized:
            value = work_auth.get("require_sponsorship")
        else:
            value = work_auth.get("legally_authorized_to_work")
        if value:
            return str(value), "suggested", "profile", 0

    if classification.category == "name":
        value = personal.get("full_name") or personal.get("preferred_name")
        if value:
            return str(value), "suggested", "profile", 0

    if classification.category == "eeo":
        if "gender" in normalized:
            value = eeo.get("gender")
        elif "hispanic" in normalized:
            value = eeo.get("race_ethnicity")
        elif "race" in normalized:
            value = eeo.get("race_ethnicity")
        elif "veteran" in normalized:
            value = eeo.get("veteran_status")
        elif "disability" in normalized:
            value = eeo.get("disability_status")
        else:
            value = None
        if value:
            return str(value), "suggested", "profile", 0

    return None, "pending", "unknown", 0


def _insert_or_update_question(conn: sqlite3.Connection, raw_text: str, seen_at: str) -> tuple[int, bool]:
    question_text = display_question_text(raw_text)
    normalized = normalize_question_text(raw_text)
    q_hash = question_hash(raw_text)
    classification = classify_question(raw_text)
    existing = conn.execute(
        "SELECT id FROM application_questions WHERE question_hash = ?",
        (q_hash,),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE application_questions SET last_seen_at = ?, updated_at = ? WHERE id = ?",
            (seen_at, seen_at, existing["id"]),
        )
        return int(existing["id"]), False

    cursor = conn.execute(
        """
        INSERT INTO application_questions (
            question_hash, question_text, normalized_text, category, risk_level,
            answer_type, first_seen_at, last_seen_at, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            q_hash,
            question_text,
            normalized,
            classification.category,
            classification.risk_level,
            classification.answer_type,
            seen_at,
            seen_at,
            seen_at,
            seen_at,
        ),
    )
    return int(cursor.lastrowid), True


def _ensure_answer_row(
    conn: sqlite3.Connection,
    question_id: int,
    question_text: str,
    profile: dict[str, Any] | None,
    now: str,
) -> bool:
    existing = conn.execute(
        "SELECT id FROM application_question_answers WHERE question_id = ?",
        (question_id,),
    ).fetchone()
    if existing:
        return False

    classification = classify_question(question_text)
    answer_text, status, source, auto_submit = _profile_suggestion(question_text, classification, profile)
    conn.execute(
        """
        INSERT INTO application_question_answers (
            question_id, answer_text, answer_status, answer_source, confidence,
            auto_submit_allowed, created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            question_id,
            answer_text,
            status,
            source,
            "medium" if source == "profile" else "low",
            auto_submit,
            now,
            now,
        ),
    )
    return True
```

- [ ] **Step 4: Add import function**

Add this function:

```python
def import_questions_from_file(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    company: str | None = None,
    job_title: str | None = None,
    job_url: str | None = None,
    application_url: str | None = None,
    source: str = "pasted_text",
    profile: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source_path = Path(path)
    if not source_path.exists():
        raise FileNotFoundError(f"Question import file not found: {source_path}")

    raw_lines = source_path.read_text(encoding="utf-8").splitlines()
    nonempty = [line for line in raw_lines if line.strip()]
    seen_keys: set[str] = set()
    stats = {
        "raw_lines": len(nonempty),
        "question_like_lines": 0,
        "canonical_created": 0,
        "canonical_reused": 0,
        "duplicates_seen": 0,
        "instances_created": 0,
        "answers_created": 0,
    }
    now = now_utc()

    try:
        for line in nonempty:
            if not is_question_like(line):
                continue
            stats["question_like_lines"] += 1
            key = normalize_question_text(line)
            duplicate_in_import = key in seen_keys
            if duplicate_in_import:
                stats["duplicates_seen"] += 1
            seen_keys.add(key)

            question_id, created = _insert_or_update_question(conn, line, now)
            if created:
                stats["canonical_created"] += 1
            else:
                stats["canonical_reused"] += 1

            conn.execute(
                """
                INSERT INTO application_question_instances (
                    question_id, job_url, application_url, company, job_title,
                    source, source_file, raw_text, required, options_json, seen_at, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    question_id,
                    job_url,
                    application_url,
                    company,
                    job_title,
                    source,
                    str(source_path),
                    line,
                    1 if line.rstrip().endswith("*") else 0,
                    json.dumps([], ensure_ascii=False),
                    now,
                    now,
                ),
            )
            stats["instances_created"] += 1

            if _ensure_answer_row(conn, question_id, line, profile, now):
                stats["answers_created"] += 1

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    return stats
```

- [ ] **Step 5: Run import tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_bank.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit import persistence**

Run:

```powershell
git add src/applypilot/question_bank.py tests/test_application_question_bank.py
git commit -m "Import application questions into answer bank"
```

## Task 4: Add List, Answer, and Export APIs

**Files:**
- Modify: `src/applypilot/config.py`
- Modify: `src/applypilot/question_bank.py`
- Modify: `tests/test_application_question_bank.py`

- [ ] **Step 1: Add failing API tests**

Append:

```python
def test_list_pending_questions_and_answer_question(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    source = tmp_path / "questions.txt"
    source.write_text("Have you ever been convicted of a felony?\u2009*\n", encoding="utf-8")
    question_bank.import_questions_from_file(conn, source, company="IDEA")

    pending = question_bank.list_questions(conn, answer_status="manual_only")
    assert len(pending) == 1
    question_id = pending[0]["id"]

    updated = question_bank.answer_question(
        conn,
        question_id,
        answer="No",
        approve=True,
        manual_only=False,
        notes="User-approved default.",
    )

    assert updated["answer_status"] == "approved"
    assert updated["answer_text"] == "No"
    assert updated["auto_submit_allowed"] == 0


def test_export_questions_writes_csv_jsonl_and_summary(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    source = tmp_path / "questions.txt"
    source.write_text("Are you legally able to work in the U.S. without visa sponsorship?\u2009*\n", encoding="utf-8")
    question_bank.import_questions_from_file(
        conn,
        source,
        company="IDEA",
        profile={"work_authorization": {"legally_authorized_to_work": "Yes"}},
    )

    result = question_bank.export_questions(conn, output_dir=tmp_path / "exports")

    assert result["questions_exported"] == 1
    assert Path(result["csv_path"]).exists()
    assert Path(result["jsonl_path"]).exists()
    assert Path(result["summary_path"]).exists()
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_bank.py::test_list_pending_questions_and_answer_question -v
```

Expected: fails because list/answer/export APIs do not exist.

- [ ] **Step 3: Add export directory constant**

In `src/applypilot/config.py`, add near `APPLICATION_EXPORT_DIR`:

```python
APPLICATION_QUESTION_EXPORT_DIR = APP_DIR / "application_question_exports"
```

In `ensure_dirs()`, add it after `APPLICATION_EXPORT_DIR`:

```python
        APPLICATION_QUESTION_EXPORT_DIR,
```

- [ ] **Step 4: Add list, answer, and export functions**

Add these imports to `src/applypilot/question_bank.py`:

```python
import csv
from applypilot.config import APPLICATION_QUESTION_EXPORT_DIR
```

Add these functions:

```python
QUESTION_EXPORT_COLUMNS = [
    "id",
    "question_text",
    "category",
    "risk_level",
    "answer_type",
    "answer_text",
    "answer_status",
    "answer_source",
    "auto_submit_allowed",
    "company",
    "job_title",
    "last_seen_at",
    "notes",
]


def list_questions(
    conn: sqlite3.Connection,
    *,
    answer_status: str | None = None,
    risk_level: str | None = None,
    company: str | None = None,
    limit: int = 0,
) -> list[dict[str, Any]]:
    clauses = ["1 = 1"]
    params: list[Any] = []
    if answer_status:
        clauses.append("a.answer_status = ?")
        params.append(answer_status)
    if risk_level:
        clauses.append("q.risk_level = ?")
        params.append(risk_level)
    if company:
        clauses.append("i.company = ?")
        params.append(company)

    sql = """
        SELECT q.id, q.question_text, q.category, q.risk_level, q.answer_type,
               q.last_seen_at, a.answer_text, a.answer_status, a.answer_source,
               a.auto_submit_allowed, a.notes,
               MAX(i.company) AS company, MAX(i.job_title) AS job_title,
               COUNT(i.id) AS instance_count
          FROM application_questions q
          JOIN application_question_answers a ON a.question_id = q.id
          LEFT JOIN application_question_instances i ON i.question_id = q.id
         WHERE """ + " AND ".join(clauses) + """
         GROUP BY q.id
         ORDER BY
              CASE a.answer_status
                   WHEN 'pending' THEN 0
                   WHEN 'manual_only' THEN 1
                   WHEN 'suggested' THEN 2
                   WHEN 'approved' THEN 3
                   ELSE 4
              END,
              q.last_seen_at DESC,
              q.id ASC
    """
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def answer_question(
    conn: sqlite3.Connection,
    question_id: int,
    *,
    answer: str | None = None,
    approve: bool = False,
    manual_only: bool = False,
    auto_submit_allowed: bool = False,
    notes: str | None = None,
    approved_by: str = "user",
) -> dict[str, Any]:
    existing = conn.execute(
        "SELECT id FROM application_question_answers WHERE question_id = ?",
        (question_id,),
    ).fetchone()
    if existing is None:
        raise ValueError(f"Question id not found or has no answer row: {question_id}")

    now = now_utc()
    if manual_only:
        status = "manual_only"
        approved_at = None
        allowed = 0
    elif approve:
        status = "approved"
        approved_at = now
        allowed = 1 if auto_submit_allowed else 0
    else:
        status = "pending"
        approved_at = None
        allowed = 0

    conn.execute(
        """
        UPDATE application_question_answers
           SET answer_text = ?,
               answer_status = ?,
               answer_source = 'user',
               confidence = ?,
               auto_submit_allowed = ?,
               notes = ?,
               approved_at = ?,
               approved_by = ?,
               updated_at = ?
         WHERE question_id = ?
        """,
        (
            answer,
            status,
            "high" if approve else "medium",
            allowed,
            notes,
            approved_at,
            approved_by if approve else None,
            now,
            question_id,
        ),
    )
    conn.commit()
    row = conn.execute(
        """
        SELECT q.id, q.question_text, a.answer_text, a.answer_status,
               a.answer_source, a.auto_submit_allowed, a.notes
          FROM application_questions q
          JOIN application_question_answers a ON a.question_id = q.id
         WHERE q.id = ?
        """,
        (question_id,),
    ).fetchone()
    return dict(row)


def export_questions(conn: sqlite3.Connection, output_dir: str | Path | None = None) -> dict[str, Any]:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = Path(output_dir) if output_dir else APPLICATION_QUESTION_EXPORT_DIR / timestamp
    destination.mkdir(parents=True, exist_ok=True)
    rows = list_questions(conn, limit=0)

    csv_path = destination / "application_questions.csv"
    jsonl_path = destination / "application_questions.jsonl"
    summary_path = destination / "summary.json"

    with csv_path.open("w", newline="", encoding="utf-8") as csv_file, jsonl_path.open("w", encoding="utf-8") as jsonl_file:
        writer = csv.DictWriter(csv_file, fieldnames=QUESTION_EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            exported = {key: row.get(key) for key in QUESTION_EXPORT_COLUMNS}
            writer.writerow(exported)
            jsonl_file.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "questions_exported": len(rows),
        "output_dir": str(destination),
        "csv_path": str(csv_path),
        "jsonl_path": str(jsonl_path),
        "summary_path": str(summary_path),
        "by_status": {},
        "by_risk": {},
    }
    for row in rows:
        summary["by_status"][row["answer_status"]] = summary["by_status"].get(row["answer_status"], 0) + 1
        summary["by_risk"][row["risk_level"]] = summary["by_risk"].get(row["risk_level"], 0) + 1
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary
```

- [ ] **Step 5: Run API tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_bank.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit APIs**

Run:

```powershell
git add src/applypilot/config.py src/applypilot/question_bank.py tests/test_application_question_bank.py
git commit -m "Add question bank list answer export APIs"
```

## Task 5: Add CLI Commands

**Files:**
- Modify: `src/applypilot/cli.py`
- Create: `tests/test_application_question_cli.py`

- [ ] **Step 1: Add failing CLI tests**

Create `tests/test_application_question_cli.py`:

```python
from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from applypilot import cli, database


def test_import_questions_command(monkeypatch, tmp_path: Path) -> None:
    db_path = tmp_path / "applypilot.db"
    source = tmp_path / "questions.txt"
    source.write_text(
        "Are you legally able to work in the U.S. without visa sponsorship?\u2009*\n"
        "Are you legally able to work in the U.S. without visa sponsorship?\u2009*\n",
        encoding="utf-8",
    )

    def bootstrap() -> None:
        database.init_db(db_path)

    monkeypatch.setattr(cli, "_bootstrap", bootstrap)
    monkeypatch.setattr(database, "get_connection", lambda: database.get_connection(db_path))

    result = CliRunner().invoke(
        cli.app,
        [
            "import-questions",
            str(source),
            "--company",
            "IDEA Public Schools",
            "--job-title",
            "Chief of Staff - Finance (26-27)",
        ],
    )

    assert result.exit_code == 0
    assert "Question import complete" in result.output
    assert "Canonical created: 1" in result.output
    assert "Duplicates seen:   1" in result.output


def test_answer_question_command(monkeypatch, tmp_path: Path) -> None:
    from applypilot import question_bank

    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    source = tmp_path / "questions.txt"
    source.write_text("Have you ever been convicted of a felony?\u2009*\n", encoding="utf-8")
    question_bank.import_questions_from_file(conn, source)

    def bootstrap() -> None:
        database.init_db(db_path)

    monkeypatch.setattr(cli, "_bootstrap", bootstrap)
    monkeypatch.setattr(database, "get_connection", lambda: database.get_connection(db_path))

    result = CliRunner().invoke(
        cli.app,
        ["answer-question", "1", "--answer", "No", "--approve"],
    )

    assert result.exit_code == 0
    assert "Answer updated" in result.output
    row = conn.execute("SELECT answer_text, answer_status FROM application_question_answers WHERE question_id = 1").fetchone()
    assert row["answer_text"] == "No"
    assert row["answer_status"] == "approved"
```

- [ ] **Step 2: Run CLI tests and verify failure**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_cli.py -v
```

Expected: fails because the CLI commands do not exist.

- [ ] **Step 3: Add CLI commands**

In `src/applypilot/cli.py`, add these commands near the other application tracker commands, after `export_applications_command`:

```python
@app.command("import-questions")
def import_questions_command(
    path: Path = typer.Argument(..., help="Pasted application text file to import."),
    company: Optional[str] = typer.Option(None, "--company", help="Company/employer name."),
    job_title: Optional[str] = typer.Option(None, "--job-title", help="Job title for this source."),
    job_url: Optional[str] = typer.Option(None, "--job-url", help="Job URL for this source."),
    application_url: Optional[str] = typer.Option(None, "--application-url", help="Application URL for this source."),
) -> None:
    """Import, dedupe, and queue application questions from a pasted text dump."""
    _bootstrap()

    from applypilot import config
    from applypilot.database import get_connection
    from applypilot.question_bank import import_questions_from_file

    try:
        profile = config.load_profile()
    except FileNotFoundError:
        profile = None

    result = import_questions_from_file(
        get_connection(),
        path,
        company=company,
        job_title=job_title,
        job_url=job_url,
        application_url=application_url,
        profile=profile,
    )
    console.print("\n[bold green]Question import complete[/bold green]")
    console.print(f"  Raw lines:         {result['raw_lines']}")
    console.print(f"  Question-like:     {result['question_like_lines']}")
    console.print(f"  Canonical created: {result['canonical_created']}")
    console.print(f"  Canonical reused:  {result['canonical_reused']}")
    console.print(f"  Duplicates seen:   {result['duplicates_seen']}")
    console.print(f"  Instances created: {result['instances_created']}")
    console.print(f"  Answers created:   {result['answers_created']}")


@app.command("list-questions")
def list_questions_command(
    pending: bool = typer.Option(False, "--pending", help="Show pending/manual-only/suggested questions."),
    risk: Optional[str] = typer.Option(None, "--risk", help="Filter by risk level."),
    status: Optional[str] = typer.Option(None, "--status", help="Filter by answer status."),
    company: Optional[str] = typer.Option(None, "--company", help="Filter by company."),
    limit: int = typer.Option(50, "--limit", "-l", help="Maximum rows to show. 0 = all."),
) -> None:
    """List reusable application questions and answer status."""
    _bootstrap()

    from applypilot.database import get_connection
    from applypilot.question_bank import list_questions

    answer_status = status
    rows = list_questions(get_connection(), answer_status=answer_status, risk_level=risk, company=company, limit=limit)
    if pending:
        rows = [row for row in rows if row.get("answer_status") in {"pending", "manual_only", "suggested"}]

    table = Table(title="Application Questions")
    table.add_column("ID", justify="right")
    table.add_column("Status")
    table.add_column("Risk")
    table.add_column("Category")
    table.add_column("Question")
    table.add_column("Answer")
    for row in rows:
        table.add_row(
            str(row["id"]),
            str(row.get("answer_status") or ""),
            str(row.get("risk_level") or ""),
            str(row.get("category") or ""),
            str(row.get("question_text") or "")[:90],
            str(row.get("answer_text") or "")[:50],
        )
    console.print(table)
    console.print(f"Rows: {len(rows)}")


@app.command("answer-question")
def answer_question_command(
    question_id: int = typer.Argument(..., help="Application question id."),
    answer: Optional[str] = typer.Option(None, "--answer", help="Answer text to store."),
    approve: bool = typer.Option(False, "--approve", help="Mark answer approved."),
    manual_only: bool = typer.Option(False, "--manual-only", help="Mark question manual-only."),
    auto_submit: bool = typer.Option(False, "--auto-submit", help="Allow future automated use. Use only for safe approved answers."),
    notes: Optional[str] = typer.Option(None, "--notes", help="Notes about this answer."),
) -> None:
    """Store or approve an answer for a canonical application question."""
    _bootstrap()

    from applypilot.database import get_connection
    from applypilot.question_bank import answer_question

    row = answer_question(
        get_connection(),
        question_id,
        answer=answer,
        approve=approve,
        manual_only=manual_only,
        auto_submit_allowed=auto_submit,
        notes=notes,
    )
    console.print("\n[bold green]Answer updated[/bold green]")
    console.print(f"  Question: {row['question_text']}")
    console.print(f"  Status:   {row['answer_status']}")
    console.print(f"  Answer:   {row.get('answer_text') or ''}")


@app.command("export-questions")
def export_questions_command(
    output: Optional[Path] = typer.Option(None, "--output", "-o", help="Destination folder. Defaults to application_question_exports timestamp folder."),
) -> None:
    """Export the reusable application question bank."""
    _bootstrap()

    from applypilot.database import get_connection
    from applypilot.question_bank import export_questions

    result = export_questions(get_connection(), output_dir=output)
    console.print("\n[bold green]Question export complete[/bold green]")
    console.print(f"  Questions exported: {result['questions_exported']}")
    console.print(f"  Folder:             {result['output_dir']}")
    console.print(f"  CSV:                {result['csv_path']}")
    console.print(f"  JSONL:              {result['jsonl_path']}")
```

- [ ] **Step 4: Run CLI tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_cli.py -v
```

Expected: both tests pass.

- [ ] **Step 5: Commit CLI commands**

Run:

```powershell
git add src/applypilot/cli.py tests/test_application_question_cli.py
git commit -m "Add application question bank CLI"
```

## Task 6: Import the Provided Example and Verify Usability

**Files:**
- No source-code edits expected unless tests expose a bug.

- [ ] **Step 1: Run focused test suite**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_bank.py tests/test_application_question_cli.py tests/test_database_schema.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Import the attached IDEA example**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m applypilot import-questions "C:\Users\JStal\.codex\attachments\722c6923-67dd-4a5d-b2d9-e4c1e615f3b2\pasted-text.txt" --company "IDEA Public Schools" --job-title "Chief of Staff - Finance (26-27)"
```

Expected output should report a duplicate count greater than zero and canonical questions fewer than question-like lines.

- [ ] **Step 3: Show pending/manual queue**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m applypilot list-questions --pending --limit 100
```

Expected: rows include work authorization suggestions, legal/background manual-only questions, EEO questions, and signature/date/name questions.

- [ ] **Step 4: Approve one safe answer manually**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m applypilot answer-question 1 --answer "Yes" --approve
```

Expected: command prints `Answer updated` and status `approved`.

- [ ] **Step 5: Export question bank**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m applypilot export-questions
```

Expected: command prints paths for CSV and JSONL under `.applypilot\application_question_exports\`.

- [ ] **Step 6: Check working tree and commit verification fixes if any**

Run:

```powershell
git status --short
```

Expected: only intended source/test files are changed. If Task 6 required code fixes, commit them with:

```powershell
git add src/applypilot tests
git commit -m "Verify application question import workflow"
```

## Task 7: Final Verification

**Files:**
- No source-code edits expected.

- [ ] **Step 1: Run focused tests**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m pytest tests/test_application_question_bank.py tests/test_application_question_cli.py tests/test_database_schema.py tests/test_cli_helpers.py -v
```

Expected: all tests pass.

- [ ] **Step 2: Run import/list/export smoke commands**

Run:

```powershell
$env:PYTHONPATH='src'
.\.venv\Scripts\python.exe -m applypilot list-questions --pending --limit 10
.\.venv\Scripts\python.exe -m applypilot export-questions
```

Expected: both commands complete without traceback.

- [ ] **Step 3: Report final state**

Collect:

```powershell
git status --short
git log --oneline -5
```

Expected: report the commits created, test command results, and the export path.

