from __future__ import annotations

import sqlite3

import pytest

from applypilot import database


def test_init_db_creates_canonical_decision_schema(tmp_path) -> None:
    conn = database.init_db(tmp_path / "brain.db")

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"decision_policy_versions", "job_decisions", "reviewed_outcomes"} <= tables

    job_columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert {
        "canonical_decision_id",
        "canonical_policy_version",
        "canonical_action",
        "canonical_score",
        "canonical_decided_at",
    } <= job_columns

    conn.execute("INSERT INTO jobs(url) VALUES ('https://example.test/unscored')")
    projection = conn.execute(
        """
        SELECT canonical_decision_id, canonical_policy_version, canonical_action,
               canonical_score, canonical_decided_at
        FROM jobs
        WHERE url = 'https://example.test/unscored'
        """
    ).fetchone()
    assert tuple(projection) == (None, None, None, None, None)


def test_canonical_schema_constraints_reject_invalid_values(tmp_path) -> None:
    conn = database.init_db(tmp_path / "brain.db")
    conn.execute("INSERT INTO jobs(url) VALUES ('https://example.test/job')")
    conn.execute(
        """
        INSERT INTO email_events(message_id, occurred_at, stage, scanned_at)
        VALUES ('event-1', '2026-07-10T00:00:00Z', 'screen', '2026-07-10T00:00:00Z')
        """
    )
    conn.execute(
        """
        INSERT INTO decision_policy_versions(policy_version, lane, status, created_at)
        VALUES ('policy-v1', 'ats', 'draft', '2026-07-10T00:00:00Z')
        """
    )

    invalid_statements = (
        """
        INSERT INTO decision_policy_versions(policy_version, lane, status, created_at)
        VALUES ('bad-status', 'ats', 'wrong', '2026-07-10T00:00:00Z')
        """,
        """
        INSERT INTO decision_policy_versions(policy_version, lane, status, created_at)
        VALUES ('bad-lane', 'email', 'draft', '2026-07-10T00:00:00Z')
        """,
        """
        INSERT INTO job_decisions(
            decision_id, job_url, policy_version, lane, qualification_verdict,
            action, input_hash, created_at
        ) VALUES (
            'bad-decision-lane', 'https://example.test/job', 'policy-v1', 'email',
            'qualified', 'apply', 'hash-lane', '2026-07-10T00:00:00Z'
        )
        """,
        """
        INSERT INTO job_decisions(
            decision_id, job_url, policy_version, lane, qualification_verdict,
            action, input_hash, created_at
        ) VALUES (
            'bad-verdict', 'https://example.test/job', 'policy-v1', 'ats',
            'maybe', 'apply', 'hash-verdict', '2026-07-10T00:00:00Z'
        )
        """,
        """
        INSERT INTO job_decisions(
            decision_id, job_url, policy_version, lane, qualification_verdict,
            action, input_hash, created_at
        ) VALUES (
            'bad-action', 'https://example.test/job', 'policy-v1', 'ats',
            'qualified', 'queue', 'hash-action', '2026-07-10T00:00:00Z'
        )
        """,
        """
        INSERT INTO reviewed_outcomes(
            event_id, job_url, review_status, created_at
        ) VALUES (
            'event-1', 'https://example.test/job', 'pending',
            '2026-07-10T00:00:00Z'
        )
        """,
    )

    for statement in invalid_statements:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(statement)
