from __future__ import annotations


def test_hiringcafe_stubs_are_not_marked_detail_scraped(tmp_path):
    from applypilot import database
    from applypilot.discovery import hiringcafe

    conn = database.init_db(tmp_path / "h.db")
    jobs = [{
        "url": "https://hiring.cafe/viewjob/1",
        "title": "Ops",
        "company": "RealCo",
        "full_description": "Requirements Summary:\nshort board summary",
        "application_url": "https://realco.example/jobs/1",
    }]

    new, existing = hiringcafe._store_jobs(conn, jobs)

    assert (new, existing) == (1, 0)
    row = conn.execute(
        "SELECT detail_scraped_at, source_board FROM jobs WHERE url = ?",
        ("https://hiring.cafe/viewjob/1",),
    ).fetchone()
    assert row["detail_scraped_at"] is None
    assert row["source_board"] == "hiringcafe"
    pending = database.get_jobs_by_stage(conn, "pending_detail", limit=10)
    assert [j["url"] for j in pending] == ["https://hiring.cafe/viewjob/1"]
