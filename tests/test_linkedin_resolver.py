from __future__ import annotations

from datetime import datetime, timezone

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
