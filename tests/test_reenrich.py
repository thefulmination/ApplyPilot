from __future__ import annotations


def _insert(conn, url, desc, *, site="Co", scraped="2026-06-01T00:00:00+00:00",
            attempts=0, dup=None, source_board=None, fit_score=None, audit_score=None,
            audit_label=None, scored_at=None, fit_verdict=None, audited_at=None):
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, detail_scraped_at, "
        "detail_attempts, duplicate_of_url, source_board, fit_score, audit_score, "
        "audit_label, scored_at, fit_verdict, audited_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (url, "T", site, desc, scraped, attempts, dup, source_board, fit_score,
         audit_score, audit_label, scored_at, fit_verdict, audited_at),
    )


def test_reenrich_selects_thin_and_reports_improved(tmp_path, monkeypatch):
    from applypilot import database
    from applypilot.enrichment import detail

    conn = database.init_db(tmp_path / "a.db")
    monkeypatch.setattr(detail, "init_db", lambda *a, **k: conn)

    _insert(conn, "u_thin", "short")                       # eligible (thin)
    _insert(conn, "u_empty", None)                         # eligible (missing)
    _insert(conn, "u_good", "x" * 300)                     # not thin -> skip
    _insert(conn, "u_exhausted", "short", attempts=3)      # attempts maxed -> skip
    _insert(conn, "u_unscraped", "short", scraped=None)    # not scraped yet -> skip
    _insert(conn, "u_dup", "short", dup="u_good")          # duplicate -> skip
    _insert(conn, "u_skip", "short", site="glassdoor")     # skip-detail site -> skip
    conn.commit()

    scraped_sites: list[str] = []

    def fake_batch(c, site, jobs, delay=2.0, max_jobs=None):
        # Simulate a successful re-scrape: fill a real description for exactly the
        # jobs passed (not a query over the whole table).
        scraped_sites.append(site)
        for url, _title in jobs:
            c.execute(
                "UPDATE jobs SET full_description = ?, detail_scraped_at = ? WHERE url = ?",
                ("y" * 400, "2026-06-02T00:00:00+00:00", url),
            )
        c.commit()
        return {"processed": len(jobs), "ok": len(jobs), "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    monkeypatch.setattr(detail, "scrape_site_batch", fake_batch)

    r = detail.reenrich_thin_descriptions(min_chars=200)

    assert r["eligible"] == 2          # only u_thin + u_empty
    assert r["reenriched"] == 2
    assert r["improved"] == 2
    assert r["still_thin"] == 0
    # only the two eligible jobs' site(s) were scraped -- not the whole queue
    assert all(s != "glassdoor" for s in scraped_sites)
    # the unrelated unscraped row must NOT have been touched by reenrich
    assert conn.execute("SELECT detail_scraped_at FROM jobs WHERE url='u_unscraped'").fetchone()[0] is None
    # untouched good row keeps its original attempt count
    assert conn.execute("SELECT detail_attempts FROM jobs WHERE url='u_good'").fetchone()[0] == 0
    # eligible rows had their attempts bumped (so they can't loop forever)
    assert conn.execute("SELECT detail_attempts FROM jobs WHERE url='u_thin'").fetchone()[0] == 1


def test_reenrich_nothing_eligible(tmp_path, monkeypatch):
    from applypilot import database
    from applypilot.enrichment import detail

    conn = database.init_db(tmp_path / "b.db")
    monkeypatch.setattr(detail, "init_db", lambda *a, **k: conn)
    _insert(conn, "u_good", "x" * 300)
    conn.commit()

    r = detail.reenrich_thin_descriptions(min_chars=200)
    assert r == {"eligible": 0, "reenriched": 0, "improved": 0, "still_thin": 0}


def test_reenrich_source_board_selects_hiringcafe_stubs_by_score_and_resets_stale_scores(tmp_path, monkeypatch):
    from applypilot import database
    from applypilot.enrichment import detail

    conn = database.init_db(tmp_path / "c.db")
    monkeypatch.setattr(detail, "init_db", lambda *a, **k: conn)
    _insert(conn, "u_high", "Requirements Summary:\nstub", source_board="hiringcafe",
            fit_score=9, audit_score=8, audit_label="recommended",
            scored_at="2026-06-01", fit_verdict="old", audited_at="2026-06-01")
    _insert(conn, "u_approved", "Requirements Summary:\nstub", source_board="hiringcafe",
            fit_score=8, audit_score=10, audit_label="approved_external",
            scored_at="2026-06-01", fit_verdict="old", audited_at="2026-06-01")
    _insert(conn, "u_low", "Requirements Summary:\nstub", source_board="hiringcafe", fit_score=3)
    _insert(conn, "u_other", "Requirements Summary:\nstub", source_board="other", fit_score=10)
    conn.commit()

    seen: list[str] = []

    def fake_batch(c, site, jobs, delay=2.0, max_jobs=None):
        for url, _title in jobs:
            seen.append(url)
            c.execute(
                "UPDATE jobs SET full_description = ?, detail_scraped_at = ? WHERE url = ?",
                ("real description " * 60, "2026-06-02T00:00:00+00:00", url),
            )
        c.commit()
        return {"processed": len(jobs), "ok": len(jobs), "partial": 0, "error": 0, "tiers": {1: 0, 2: 0, 3: 0}}

    monkeypatch.setattr(detail, "scrape_site_batch", fake_batch)

    r = detail.reenrich_thin_descriptions(min_chars=500, source_board="hiringcafe", limit=2)

    assert seen == ["u_approved", "u_high"]
    assert r["eligible"] == 3
    assert r["reenriched"] == 2
    assert r["improved"] == 2
    high = conn.execute(
        "SELECT fit_score, scored_at, fit_verdict, audit_score, audit_label, audited_at FROM jobs WHERE url='u_high'"
    ).fetchone()
    assert tuple(high) == (None, None, None, None, None, None)
    approved = conn.execute(
        "SELECT fit_score, scored_at, fit_verdict, audit_score, audit_label, audited_at FROM jobs WHERE url='u_approved'"
    ).fetchone()
    assert tuple(approved) == (None, None, None, 10, "approved_external", "2026-06-01")
    low = conn.execute("SELECT fit_score FROM jobs WHERE url='u_low'").fetchone()
    assert low[0] == 3
