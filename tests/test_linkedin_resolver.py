from __future__ import annotations

import sqlite3
import pytest

from applypilot import database
from applypilot import linkedin_resolver


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


def test_url_classification_distinguishes_linkedin_and_offsite():
    assert linkedin_resolver.is_linkedin_url("https://www.linkedin.com/jobs/view/123") is True
    assert linkedin_resolver.is_linkedin_url("https://linkedin.com/jobs/view/123") is True
    assert linkedin_resolver.is_linkedin_url("https://jobs.lever.co/acme/123") is False
    assert linkedin_resolver.is_external_apply_url("https://jobs.lever.co/acme/123") is True
    assert linkedin_resolver.is_external_apply_url("https://www.linkedin.com/jobs/view/123") is False
    assert linkedin_resolver.is_external_apply_url("") is False
    assert linkedin_resolver.is_external_apply_url(None) is False


def test_classify_snapshot_stops_on_linkedin_challenge():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/checkpoint/challenge",
        text="Quick security check. Verify it's you.",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "challenge_required"
    assert decision.stop_run is True
    assert decision.final_url is None


def test_classify_snapshot_stops_on_login_wall():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/login",
        text="Sign in to view this job",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "login_required"
    assert decision.stop_run is True


def test_classify_snapshot_stops_on_restricted_account_text():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/search",
        text="We've restricted your account due to unusual activity.",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "challenge_required"
    assert decision.stop_run is True


def test_classify_snapshot_stops_for_email_or_phone_login_prompt():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/login",
        text="Please enter email or phone to continue",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "login_required"
    assert decision.stop_run is True


def test_classify_snapshot_detects_unavailable_job():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/404",
        text="This job is no longer accepting applications",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "unavailable"
    assert decision.stop_run is False


def test_classify_snapshot_detects_expired_job():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/404",
        text="This job has expired and is no longer open.",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "unavailable"


def test_classify_snapshot_detects_easy_apply():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Chief of Staff",
        controls=(
            linkedin_resolver.ApplyControl(
                text="Easy Apply",
                href=None,
                selector="button",
            ),
        ),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "easy_apply"
    assert decision.stop_run is False


def test_classify_snapshot_detects_external_apply_href():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Chief of Staff",
        controls=(
            linkedin_resolver.ApplyControl(
                text="Apply",
                href="https://jobs.lever.co/acme/123",
                selector="a[href='https://jobs.lever.co/acme/123']",
            ),
        ),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "resolved_offsite"
    assert decision.final_url == "https://jobs.lever.co/acme/123"
    assert decision.control is not None


def test_classify_snapshot_keeps_generic_apply_control_for_click():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Chief of Staff",
        controls=(
            linkedin_resolver.ApplyControl(
                text="Apply",
                href="https://www.linkedin.com/jobs/view/123?trk=public_jobs_apply-link-offsite",
                selector="button",
            ),
        ),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "needs_click"
    assert decision.control is not None


def test_classify_snapshot_reports_missing_apply_control():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Chief of Staff",
        controls=(),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "no_apply_button"


def test_classify_snapshot_handles_missing_url_and_controls():
    snapshot = linkedin_resolver.PageSnapshot(
        url=None,  # type: ignore[arg-type]
        text="Chief of Staff",
        controls=None,  # type: ignore[arg-type]
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "no_apply_button"


def test_classify_snapshot_handles_control_without_text():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Chief of Staff",
        controls=(
            linkedin_resolver.ApplyControl(
                text=None,  # type: ignore[arg-type]
                href=None,
                selector="button",
            ),
        ),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "no_apply_button"


def test_classify_snapshot_does_not_treat_generic_sign_in_text_as_login_wall():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Sign in to learn more about Acme benefits.",
        controls=(
            linkedin_resolver.ApplyControl(
                text="Easy Apply",
                href=None,
                selector="button",
            ),
        ),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "easy_apply"


def test_classify_snapshot_does_not_treat_generic_checkpoint_text_as_challenge():
    snapshot = linkedin_resolver.PageSnapshot(
        url="https://www.linkedin.com/jobs/view/123",
        text="Own the checkpoint planning process for quarterly initiatives.",
        controls=(
            linkedin_resolver.ApplyControl(
                text="Apply",
                href="https://jobs.lever.co/acme/123",
                selector="a[href='https://jobs.lever.co/acme/123']",
            ),
        ),
    )

    decision = linkedin_resolver.classify_snapshot(snapshot)

    assert decision.status == "resolved_offsite"


def _insert_job(
    conn,
    *,
    url: str,
    title: str = "Chief of Staff",
    site: str = "linkedin",
    application_url: str | None = None,
    audit_label: str | None = "recommended",
    audit_score: float | None = 8.5,
    fit_score: int | None = 8,
    duplicate_of_url: str | None = None,
    liveness_status: str | None = None,
    applied_at: str | None = None,
    linkedin_resolve_status: str | None = None,
    discovered_at: str = "2026-06-20T00:00:00+00:00",
):
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, site, company, application_url, audit_label, audit_score,
            fit_score, duplicate_of_url, liveness_status, applied_at,
            linkedin_resolve_status, discovered_at
        )
        VALUES (?, ?, ?, 'Acme', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            url,
            title,
            site,
            application_url,
            audit_label,
            audit_score,
            fit_score,
            duplicate_of_url,
            liveness_status,
            applied_at,
            linkedin_resolve_status,
            discovered_at,
        ),
    )
    conn.commit()


def test_fetch_candidates_prioritizes_recommended_unresolved_linkedin_rows(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)

    _insert_job(conn, url="https://www.linkedin.com/jobs/view/low", audit_label="low", audit_score=9.9)
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/dupe", duplicate_of_url="https://x")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/dead", liveness_status="dead")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/applied", applied_at="2026-06-20T01:00:00+00:00")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/offsite", application_url="https://jobs.lever.co/acme/1")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/easy", linkedin_resolve_status="easy_apply")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/priority", audit_label="priority", audit_score=7.0)
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/recommended", audit_label="recommended", audit_score=9.0)

    rows = linkedin_resolver.fetch_candidates(limit=10, tiers=("priority", "recommended"))

    assert [row.url for row in rows] == [
        "https://www.linkedin.com/jobs/view/priority",
        "https://www.linkedin.com/jobs/view/recommended",
    ]


def test_fetch_candidates_can_include_low_and_refresh_completed_statuses(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/low", audit_label="low")
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/easy", linkedin_resolve_status="easy_apply")

    rows = linkedin_resolver.fetch_candidates(
        limit=10,
        tiers=("priority", "recommended"),
        include_low=True,
        refresh=True,
    )

    assert {row.url for row in rows} == {
        "https://www.linkedin.com/jobs/view/low",
        "https://www.linkedin.com/jobs/view/easy",
    }


def test_fetch_candidates_filters_external_application_urls_host_aware(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)

    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/query-string",
        audit_label="priority",
        application_url="https://jobs.lever.co/acme/1?utm_source=linkedin.com",
    )
    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/linkedin-host",
        audit_label="priority",
        application_url="https://www.linkedin.com/jobs/view/123",
    )
    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/blank-host",
        audit_label="priority",
    )

    rows = linkedin_resolver.fetch_candidates(limit=10, tiers=("priority",))

    assert [row.url for row in rows] == [
        "https://www.linkedin.com/jobs/view/blank-host",
        "https://www.linkedin.com/jobs/view/linkedin-host",
    ]


def test_fetch_candidates_limit_zero_returns_empty(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)

    _insert_job(conn, url="https://www.linkedin.com/jobs/view/priority", audit_label="priority")

    assert linkedin_resolver.fetch_candidates(limit=0, tiers=("priority",)) == []


def test_fetch_candidates_returns_empty_when_no_qualifying_candidates(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)

    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/external-offsite",
        audit_label="priority",
        application_url="https://jobs.acme.com/abc",
    )

    rows = linkedin_resolver.fetch_candidates(limit=5, tiers=("priority",))
    assert rows == []


def test_fetch_candidates_uses_url_tie_break_for_deterministic_ordering(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)

    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/b-xyz",
        audit_label="priority",
        audit_score=8.8,
        fit_score=10,
        discovered_at="2026-06-20T00:00:00+00:00",
        application_url=None,
    )
    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/a-xyz",
        audit_label="priority",
        audit_score=8.8,
        fit_score=10,
        discovered_at="2026-06-20T00:00:00+00:00",
        application_url=None,
    )

    rows = linkedin_resolver.fetch_candidates(limit=2, tiers=("priority",))

    assert [row.url for row in rows] == [
        "https://www.linkedin.com/jobs/view/a-xyz",
        "https://www.linkedin.com/jobs/view/b-xyz",
    ]


def test_fetch_candidates_scans_past_host_filtered_top_rows_to_fill_limit(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)

    for index in range(1, 26):
        _insert_job(
            conn,
            url=f"https://www.linkedin.com/jobs/view/offsite-{index}",
            audit_label="priority",
            audit_score=9.9,
            application_url=f"https://jobs.lever.co/acme/{index}?utm_source=linkedin.com",
            discovered_at=f"2026-06-20T00:{index:02d}:00+00:00",
        )

    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/first-real",
        audit_label="priority",
        audit_score=9.0,
        discovered_at="2026-06-21T12:00:00+00:00",
    )
    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/second-real",
        audit_label="priority",
        audit_score=8.5,
        application_url="https://www.linkedin.com/jobs/view/999",
        discovered_at="2026-06-21T11:00:00+00:00",
    )

    rows = linkedin_resolver.fetch_candidates(limit=2, tiers=("priority",))

    assert [row.url for row in rows] == [
        "https://www.linkedin.com/jobs/view/first-real",
        "https://www.linkedin.com/jobs/view/second-real",
    ]


def test_fetch_candidates_obeys_max_scan_row_cap(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)

    for index in range(1, 21):
        _insert_job(
            conn,
            url=f"https://www.linkedin.com/jobs/view/offsite-{index}",
            audit_label="priority",
            application_url=f"https://jobs.lever.co/acme/{index}?utm_source=linkedin.com",
            audit_score=9.9,
            fit_score=9,
            discovered_at=f"2026-06-20T00:{index:02d}:00+00:00",
        )

    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/valid-after-scan-window",
        audit_label="priority",
        application_url=None,
        audit_score=5.0,
        fit_score=7,
        discovered_at="2026-06-21T00:00:00+00:00",
    )

    rows = linkedin_resolver.fetch_candidates(
        limit=1,
        tiers=("priority",),
        max_scan_rows=10,
    )

    assert rows == []


def test_record_resolution_works_with_plain_sqlite_connection(tmp_path):
    db_path = tmp_path / "applypilot.db"
    database.init_db(db_path)
    database.close_connection(db_path)

    with sqlite3.connect(db_path) as conn:
        _insert_job(
            conn,
            url="https://www.linkedin.com/jobs/view/plain-conn",
            application_url="https://linkedin.com/jobs/view/plain",
        )
        linkedin_resolver.record_resolution(
            "https://www.linkedin.com/jobs/view/plain-conn",
            status="resolved_offsite",
            final_url="https://jobs.ashbyhq.com/acme/plain",
            conn=conn,
        )
        row = conn.execute(
            """
            SELECT application_url, linkedin_resolve_status, linkedin_resolve_attempts,
                   linkedin_resolve_final_url
              FROM jobs
             WHERE url = ?
            """,
            ("https://www.linkedin.com/jobs/view/plain-conn",),
        ).fetchone()

    assert row is not None
    assert row[0] == "https://jobs.ashbyhq.com/acme/plain"
    assert row[1] == "resolved_offsite"
    assert row[2] == 1
    assert row[3] == "https://jobs.ashbyhq.com/acme/plain"


def test_fetch_candidates_max_scan_rows_zero_returns_empty(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)

    _insert_job(conn, url="https://www.linkedin.com/jobs/view/priority", audit_label="priority")

    assert linkedin_resolver.fetch_candidates(limit=5, tiers=("priority",), max_scan_rows=0) == []


def test_record_resolution_sets_offsite_application_url_and_attempt_metadata(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)
    _insert_job(conn, url="https://www.linkedin.com/jobs/view/resolve")

    linkedin_resolver.record_resolution(
        "https://www.linkedin.com/jobs/view/resolve",
        status="resolved_offsite",
        final_url="https://jobs.ashbyhq.com/acme/resolve",
    )

    row = conn.execute(
        """
        SELECT application_url, linkedin_resolve_status, linkedin_resolve_attempts,
               linkedin_resolve_final_url, linkedin_resolve_error, linkedin_resolved_at
        FROM jobs WHERE url = ?
        """,
        ("https://www.linkedin.com/jobs/view/resolve",),
    ).fetchone()
    assert row["application_url"] == "https://jobs.ashbyhq.com/acme/resolve"
    assert row["linkedin_resolve_status"] == "resolved_offsite"
    assert row["linkedin_resolve_attempts"] == 1
    assert row["linkedin_resolve_final_url"] == "https://jobs.ashbyhq.com/acme/resolve"
    assert row["linkedin_resolve_error"] is None
    assert row["linkedin_resolved_at"]


def test_record_resolution_does_not_overwrite_existing_offsite_without_refresh(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)
    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/already",
        application_url="https://boards.greenhouse.io/acme/jobs/old",
    )

    linkedin_resolver.record_resolution(
        "https://www.linkedin.com/jobs/view/already",
        status="resolved_offsite",
        final_url="https://jobs.lever.co/acme/new",
        refresh=False,
    )

    app_url = conn.execute(
        "SELECT application_url FROM jobs WHERE url = ?",
        ("https://www.linkedin.com/jobs/view/already",),
    ).fetchone()[0]
    assert app_url == "https://boards.greenhouse.io/acme/jobs/old"


def test_record_resolution_raises_when_job_missing(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)

    with pytest.raises(ValueError, match="Job not found: https://www.linkedin.com/jobs/view/missing"):
        linkedin_resolver.record_resolution("https://www.linkedin.com/jobs/view/missing", status="easy_apply")


def test_record_resolution_does_not_overwrite_with_invalid_or_internal_final_url(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(linkedin_resolver, "get_connection", lambda: conn)
    _insert_job(
        conn,
        url="https://www.linkedin.com/jobs/view/apply-url",
        application_url="https://boards.greenhouse.io/acme/jobs/old",
    )

    linkedin_resolver.record_resolution(
        "https://www.linkedin.com/jobs/view/apply-url",
        status="resolved_offsite",
        final_url="not a valid url",
        error="bad final URL",
    )

    row = conn.execute(
        """
        SELECT application_url, linkedin_resolve_status, linkedin_resolve_attempts,
               linkedin_resolve_final_url, linkedin_resolve_error
        FROM jobs WHERE url = ?
        """,
        ("https://www.linkedin.com/jobs/view/apply-url",),
    ).fetchone()

    assert row["application_url"] == "https://boards.greenhouse.io/acme/jobs/old"
    assert row["linkedin_resolve_status"] == "resolved_offsite"
    assert row["linkedin_resolve_attempts"] == 1
    assert row["linkedin_resolve_final_url"] == "not a valid url"
    assert row["linkedin_resolve_error"] == "bad final URL"


def test_should_stop_run_respects_stop_statuses():
    assert linkedin_resolver.should_stop_run("login_required") is True
    assert linkedin_resolver.should_stop_run("challenge_required") is True
    assert linkedin_resolver.should_stop_run("easy_apply") is False
