from __future__ import annotations

from pathlib import Path

from applypilot import database


def test_jobs_schema_tracks_company_source_and_retryable_score_errors(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
    assert {"company", "source_board", "score_error", "score_error_at", "score_attempts"} <= columns

    indexes = {row[1] for row in conn.execute("PRAGMA index_list(jobs)").fetchall()}
    assert {
        "idx_jobs_company",
        "idx_jobs_source_board",
        "idx_jobs_pending_score",
        "idx_jobs_pending_tailor",
        "idx_jobs_discovered_at",
    } <= indexes


def test_legacy_llm_error_scores_are_migrated_back_to_pending(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, site, description, full_description,
            fit_score, score_reasoning, scored_at, audit_score, audit_label, audited_at
        )
        VALUES (
            'https://example.com/error', 'Chief of Staff', 'ExampleCo',
            'short', 'full description', 0, 'keywords line\nLLM error: HTTP 503',
            '2026-05-04T00:00:00+00:00', 3.0, 'exclude', '2026-05-04T00:01:00+00:00'
        )
        """
    )
    conn.commit()

    database.repair_retryable_score_errors(conn)

    row = conn.execute(
        """
        SELECT fit_score, score_error, score_error_at, scored_at,
               audit_score, audit_label, audited_at
          FROM jobs
         WHERE url = 'https://example.com/error'
        """
    ).fetchone()
    assert row["fit_score"] is None
    assert row["score_error"] == "keywords line\nLLM error: HTTP 503"
    assert row["score_error_at"] == "2026-05-04T00:00:00+00:00"
    assert row["scored_at"] is None
    assert row["audit_score"] is None
    assert row["audit_label"] is None
    assert row["audited_at"] is None
