"""Persistent review queue for application questions that cannot be answered safely."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_question(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(value or "").lower()).strip()


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS application_answer_exceptions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            host TEXT NOT NULL,
            question_key TEXT NOT NULL,
            question TEXT NOT NULL,
            field_key TEXT NOT NULL DEFAULT '',
            options_json TEXT NOT NULL DEFAULT '[]',
            last_job_url TEXT NOT NULL DEFAULT '',
            occurrence_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            approved_answer TEXT,
            resolution_source TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(host, question_key)
        )
    """)
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(application_answer_exceptions)"
    ).fetchall()}
    if "resolution_source" not in columns:
        conn.execute(
            "ALTER TABLE application_answer_exceptions ADD COLUMN resolution_source TEXT"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_answer_exceptions_status "
        "ON application_answer_exceptions(status, updated_at)"
    )
    conn.commit()


def record_exceptions(conn: sqlite3.Connection, fields, *, host: str, job_url: str) -> list[int]:
    ensure_table(conn)
    ids: list[int] = []
    for raw in fields:
        label = str(raw.get("label") if isinstance(raw, dict) else raw.label).strip()
        key = str(raw.get("key", "") if isinstance(raw, dict) else raw.key)
        options = list(raw.get("options") or [] if isinstance(raw, dict) else raw.options)
        question_key = normalize_question(label)
        if not question_key:
            continue
        now = _now()
        conn.execute("""
            INSERT INTO application_answer_exceptions
                (host,question_key,question,field_key,options_json,last_job_url,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?)
            ON CONFLICT(host,question_key) DO UPDATE SET
                question=excluded.question,
                field_key=excluded.field_key,
                options_json=excluded.options_json,
                last_job_url=excluded.last_job_url,
                occurrence_count=application_answer_exceptions.occurrence_count+1,
                status=CASE WHEN application_answer_exceptions.status='approved'
                            THEN 'approved' ELSE 'pending' END,
                approved_answer=CASE WHEN application_answer_exceptions.status='approved'
                                     THEN application_answer_exceptions.approved_answer ELSE NULL END,
                resolution_source=CASE WHEN application_answer_exceptions.status='approved'
                                       THEN application_answer_exceptions.resolution_source ELSE NULL END,
                updated_at=excluded.updated_at
        """, (host.lower(), question_key, label, key, json.dumps(options), job_url, now, now))
        row = conn.execute(
            "SELECT id FROM application_answer_exceptions WHERE host=? AND question_key=?",
            (host.lower(), question_key),
        ).fetchone()
        ids.append(int(row[0]))
    conn.commit()
    return ids


def resolve_approved_answer(conn: sqlite3.Connection, question: str, *, host: str,
                            field_key: str | None = None) -> str | None:
    ensure_table(conn)
    normalized_question = normalize_question(question)
    if field_key:
        row = conn.execute("""
            SELECT approved_answer FROM application_answer_exceptions
             WHERE host=? AND question_key=? AND status='approved'
               AND (field_key=? OR field_key='')
             ORDER BY CASE WHEN field_key=? THEN 0 ELSE 1 END
             LIMIT 1
        """, (host.lower(), normalized_question, str(field_key), str(field_key))).fetchone()
    else:
        row = conn.execute("""
            SELECT approved_answer FROM application_answer_exceptions
             WHERE host=? AND question_key=? AND status='approved'
        """, (host.lower(), normalized_question)).fetchone()
    return str(row[0]) if row and row[0] is not None else None


def approve_exception(conn: sqlite3.Connection, exception_id: int, answer: str) -> None:
    ensure_table(conn)
    row = conn.execute(
        "SELECT options_json FROM application_answer_exceptions WHERE id=?", (int(exception_id),)
    ).fetchone()
    if row is None:
        raise KeyError("exception_not_found")
    options = json.loads(row[0] or "[]")
    if options and normalize_question(answer) not in {normalize_question(option) for option in options}:
        raise ValueError("answer_not_in_options")
    if not str(answer).strip():
        raise ValueError("answer_empty")
    conn.execute("""
        UPDATE application_answer_exceptions
           SET status='approved', approved_answer=?, resolution_source='operator', updated_at=?
         WHERE id=?
    """, (str(answer).strip(), _now(), int(exception_id)))
    conn.commit()


def reconcile_resolved_exceptions(conn: sqlite3.Connection, *, host: str,
                                  resolved_questions, resolution_source: str) -> int:
    """Close stale pending rows when current deterministic evidence resolves them."""
    ensure_table(conn)
    keys = tuple(dict.fromkeys(
        normalize_question(question) for question in resolved_questions
        if normalize_question(question)
    ))
    if not keys:
        return 0
    placeholders = ",".join("?" for _ in keys)
    cursor = conn.execute(f"""
        UPDATE application_answer_exceptions
           SET status='resolved', resolution_source=?, updated_at=?
         WHERE host=? AND status='pending' AND question_key IN ({placeholders})
    """, (resolution_source, _now(), host.lower(), *keys))
    conn.commit()
    return int(cursor.rowcount or 0)


def list_exceptions(conn: sqlite3.Connection, *, status: str | None = "pending") -> list[dict]:
    ensure_table(conn)
    sql = "SELECT * FROM application_answer_exceptions"
    params: tuple = ()
    if status is not None:
        sql += " WHERE status=?"
        params = (status,)
    # Keep the operator queue stable when reconciliation updates a row. Sorting by
    # updated_at would move a resolved older question behind newer pending rows.
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    return [{
        **dict(row),
        "options": json.loads(row["options_json"] or "[]"),
    } for row in rows]
