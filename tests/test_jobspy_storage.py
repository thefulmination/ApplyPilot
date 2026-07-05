from __future__ import annotations

from pathlib import Path

import pandas as pd

from applypilot import database
from applypilot.discovery.jobspy import store_jobspy_results


def test_jobspy_storage_uses_company_as_display_site(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    df = pd.DataFrame(
        [
            {
                "job_url": "https://www.linkedin.com/jobs/view/123",
                "job_url_direct": "https://company.example/jobs/123",
                "title": "Chief of Staff",
                "company": "ExampleCo",
                "location": "New York, NY",
                "site": "linkedin",
                "description": "A short job description",
                "is_remote": False,
            }
        ]
    )

    new, existing = store_jobspy_results(conn, df, "Chief of Staff")

    row = conn.execute(
        "SELECT site, company, source_board FROM jobs WHERE url = ?",
        ("https://www.linkedin.com/jobs/view/123",),
    ).fetchone()
    assert (new, existing) == (1, 0)
    assert row["site"] == "ExampleCo"
    assert row["company"] == "ExampleCo"
    assert row["source_board"] == "linkedin"


def test_jobspy_storage_marks_same_job_from_different_boards_as_duplicate(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    df = pd.DataFrame(
        [
            {
                "job_url": "https://www.linkedin.com/jobs/view/123",
                "job_url_direct": "https://company.example/jobs/123",
                "title": "Chief of Staff",
                "company": "ExampleCo Inc.",
                "location": "New York, NY",
                "site": "linkedin",
                "description": "A short job description " * 20,
                "is_remote": False,
            },
            {
                "job_url": "https://www.indeed.com/viewjob?jk=456",
                "job_url_direct": "https://company.example/jobs/123",
                "title": "Chief of Staff",
                "company": "ExampleCo",
                "location": "New York City",
                "site": "indeed",
                "description": "A short job description " * 20,
                "is_remote": False,
            },
        ]
    )

    new, existing = store_jobspy_results(conn, df, "Chief of Staff")

    assert (new, existing) == (1, 1)
    duplicate = conn.execute(
        """
        SELECT duplicate_of_url, duplicate_reason
          FROM jobs
         WHERE url = 'https://www.indeed.com/viewjob?jk=456'
        """
    ).fetchone()
    assert duplicate["duplicate_of_url"] == "https://www.linkedin.com/jobs/view/123"
    assert duplicate["duplicate_reason"] == "same_company_title_location"


def test_jobspy_storage_persists_date_posted(tmp_path: Path) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    df = pd.DataFrame(
        [
            {
                "job_url": "https://www.linkedin.com/jobs/view/333",
                "job_url_direct": "https://company.example/jobs/333",
                "title": "Chief of Staff",
                "company": "ExampleCo",
                "location": "New York, NY",
                "site": "linkedin",
                "description": "A short job description",
                "is_remote": False,
                "date_posted": "2026-06-10",
            }
        ]
    )

    new, existing = store_jobspy_results(conn, df, "Chief of Staff")
    row = conn.execute(
        "SELECT posted_at, valid_through FROM jobs WHERE url = ?",
        ("https://www.linkedin.com/jobs/view/333",),
    ).fetchone()

    assert (new, existing) == (1, 0)
    assert row["posted_at"] == "2026-06-10"
    assert row["valid_through"] is None
