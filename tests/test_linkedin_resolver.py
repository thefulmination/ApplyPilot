from __future__ import annotations

import sqlite3

from applypilot import database


def test_schema_adds_linkedin_resolver_columns(tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}

    assert {
        "linkedin_resolved_at",
        "linkedin_resolve_status",
        "linkedin_resolve_error",
        "linkedin_resolve_attempts",
        "linkedin_resolve_final_url",
    }.issubset(columns)


def test_schema_migrates_legacy_jobs_table(tmp_path):
    db_path = tmp_path / "applypilot.db"

    # Simulate a pre-task schema without the resolver columns.
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE jobs (
                url TEXT PRIMARY KEY,
                title TEXT,
                discovered_at TEXT
            )
            """
        )
        conn.commit()

    conn = database.init_db(db_path)

    columns = conn.execute("PRAGMA table_info(jobs)").fetchall()
    column_names = {row[1] for row in columns}
    defaults = {row[1]: row[4] for row in columns}

    assert {
        "linkedin_resolved_at",
        "linkedin_resolve_status",
        "linkedin_resolve_error",
        "linkedin_resolve_attempts",
        "linkedin_resolve_final_url",
    }.issubset(column_names)
    assert defaults["linkedin_resolve_attempts"] == "0"
