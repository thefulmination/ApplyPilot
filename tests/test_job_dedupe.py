from __future__ import annotations

from pathlib import Path

from applypilot import database


def test_discovery_insert_marks_same_job_from_different_urls_as_duplicate(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")

    first = {
        "url": "https://www.linkedin.com/jobs/view/123",
        "title": "Chief of Staff",
        "company": "Acme, Inc.",
        "location": "San Francisco, CA",
        "description": "Short listing",
        "full_description": "Full job description " * 20,
        "application_url": "https://acme.example/jobs/chief-of-staff",
    }
    second = {
        "url": "https://acme.wd1.myworkdayjobs.com/acme/job/456",
        "title": "Chief of Staff",
        "company": "Acme Inc",
        "location": "San Francisco California",
        "description": "Same role from the corporate ATS",
        "full_description": "Full job description " * 20,
        "application_url": "https://acme.wd1.myworkdayjobs.com/acme/job/456",
    }

    assert database.insert_discovered_job(
        conn, first, site="Acme, Inc.", strategy="jobspy", source_board="linkedin"
    ) == "new"
    assert database.insert_discovered_job(
        conn, second, site="Acme Inc", strategy="workday_api", source_board="workday"
    ) == "duplicate"

    rows = conn.execute(
        """
        SELECT url, dedupe_key, duplicate_of_url, duplicate_reason, duplicate_detected_at
          FROM jobs
        """
    ).fetchall()
    by_url = {row["url"]: row for row in rows}
    canonical = by_url["https://www.linkedin.com/jobs/view/123"]
    duplicate = by_url["https://acme.wd1.myworkdayjobs.com/acme/job/456"]

    assert canonical["dedupe_key"]
    assert canonical["duplicate_of_url"] is None
    assert duplicate["dedupe_key"] == canonical["dedupe_key"]
    assert duplicate["duplicate_of_url"] == canonical["url"]
    assert duplicate["duplicate_reason"] == "same_company_title_location"
    assert duplicate["duplicate_detected_at"] is not None

    pending_score = database.get_jobs_by_stage(conn, "pending_score", limit=10)
    assert [job["url"] for job in pending_score] == [canonical["url"]]


def test_dedupe_existing_jobs_supports_dry_run_and_apply(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    conn.execute(
        """
        INSERT INTO jobs (url, title, company, site, location, full_description, discovered_at)
        VALUES
        (
            'https://jobs.example.com/listing/one',
            'Strategy and Operations Manager',
            'Example LLC',
            'Example LLC',
            'New York, NY',
            'full description',
            '2026-05-01T00:00:00+00:00'
        ),
        (
            'https://ats.example.com/jobs/two',
            'Strategy & Operations Manager',
            'Example',
            'Example',
            'New York City',
            'full description',
            '2026-05-02T00:00:00+00:00'
        )
        """
    )
    conn.commit()

    dry = database.dedupe_existing_jobs(conn, dry_run=True)
    assert dry["duplicates"] == 1
    assert dry["groups"] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE duplicate_of_url IS NOT NULL"
    ).fetchone()[0] == 0

    applied = database.dedupe_existing_jobs(conn, dry_run=False)
    assert applied["duplicates"] == 1
    assert applied["groups"] == 1

    duplicate = conn.execute(
        """
        SELECT duplicate_of_url, duplicate_reason, duplicate_detected_at
          FROM jobs
         WHERE url = 'https://ats.example.com/jobs/two'
        """
    ).fetchone()
    assert duplicate["duplicate_of_url"] == "https://jobs.example.com/listing/one"
    assert duplicate["duplicate_reason"] == "same_company_title_location"
    assert duplicate["duplicate_detected_at"] is not None


def test_dedupe_key_keeps_distinct_roles_separate() -> None:
    chief_of_staff = database.build_job_dedupe_key(
        {
            "title": "Chief of Staff to CEO",
            "company": "Example Inc.",
            "location": "New York, NY",
        }
    )
    business_ops = database.build_job_dedupe_key(
        {
            "title": "Chief of Staff",
            "company": "Example Inc.",
            "location": "New York, NY",
        }
    )

    assert chief_of_staff != business_ops
