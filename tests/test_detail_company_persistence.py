from __future__ import annotations


def test_scrape_site_batch_persists_json_ld_company(tmp_path, monkeypatch):
    from applypilot import database
    from applypilot.enrichment import detail

    conn = database.init_db(tmp_path / "d.db")
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, full_description) VALUES (?, ?, ?, ?, ?)",
        ("u1", "Role", "linkedin", "linkedin", "old"),
    )
    conn.commit()

    monkeypatch.setattr(detail, "scrape_detail_page", lambda _page, _url: {
        "status": "ok",
        "full_description": "new full description",
        "application_url": "https://realco.example/jobs/1",
        "company": "RealCo",
        "tier_used": 1,
    })

    detail.scrape_site_batch(conn, "linkedin", [("u1", "Role")])

    row = conn.execute("SELECT company, full_description FROM jobs WHERE url = 'u1'").fetchone()
    assert row["company"] == "RealCo"
    assert row["full_description"] == "new full description"
