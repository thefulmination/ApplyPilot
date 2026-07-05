from __future__ import annotations


def _insert(conn, url, desc, **kw):
    cols = {
        "url": url,
        "title": "T",
        "site": "Co",
        "company": "Co",
        "application_url": url,
        "full_description": desc,
    }
    cols.update(kw)
    conn.execute(
        f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        list(cols.values()),
    )


def test_pending_score_excludes_descriptions_under_200(tmp_path):
    from applypilot import database

    conn = database.init_db(tmp_path / "a.db")
    _insert(conn, "u_short", "x" * 199)
    _insert(conn, "u_ok", "x" * 200)
    conn.commit()

    urls = {j["url"] for j in database.get_jobs_by_stage(conn, "pending_score", limit=10)}

    assert urls == {"u_ok"}


def test_pending_tailor_and_apply_exclude_thin_descriptions(tmp_path):
    from applypilot import database

    conn = database.init_db(tmp_path / "b.db")
    _insert(conn, "u_tailor_thin", "x" * 499, fit_score=8)
    _insert(conn, "u_tailor_ok", "x" * 500, fit_score=8)
    _insert(conn, "u_apply_thin", "x" * 499, fit_score=8, tailored_resume_path="r.pdf")
    _insert(conn, "u_apply_ok", "x" * 500, fit_score=8, tailored_resume_path="r.pdf")
    conn.commit()

    tailor_urls = {j["url"] for j in database.get_jobs_by_stage(conn, "pending_tailor", min_score=7, limit=10)}
    apply_urls = {j["url"] for j in database.get_jobs_by_stage(conn, "pending_apply", limit=10)}

    assert "u_tailor_thin" not in tailor_urls
    assert "u_tailor_ok" in tailor_urls
    assert "u_apply_thin" not in apply_urls
    assert "u_apply_ok" in apply_urls


def test_llm_stage_selectors_exclude_dead_liveness(tmp_path):
    from applypilot import database

    conn = database.init_db(tmp_path / "live.db")
    _insert(conn, "u_score_dead", "x" * 200, liveness_status="dead")
    _insert(conn, "u_score_ok", "x" * 200, liveness_status="live")
    _insert(conn, "u_tailor_dead", "x" * 500, fit_score=8, liveness_status="dead")
    _insert(conn, "u_tailor_ok", "x" * 500, fit_score=8, liveness_status="live")
    _insert(conn, "u_apply_dead", "x" * 500, fit_score=8, tailored_resume_path="r.pdf", liveness_status="dead")
    _insert(conn, "u_apply_ok", "x" * 500, fit_score=8, tailored_resume_path="r.pdf", liveness_status="live")
    conn.commit()

    score_urls = {j["url"] for j in database.get_jobs_by_stage(conn, "pending_score", limit=10)}
    tailor_urls = {j["url"] for j in database.get_jobs_by_stage(conn, "pending_tailor", min_score=7, limit=10)}
    apply_urls = {j["url"] for j in database.get_jobs_by_stage(conn, "pending_apply", limit=10)}

    assert "u_score_dead" not in score_urls
    assert "u_score_ok" in score_urls
    assert "u_tailor_dead" not in tailor_urls
    assert "u_tailor_ok" in tailor_urls
    assert "u_apply_dead" not in apply_urls
    assert "u_apply_ok" in apply_urls


def test_llm_stage_selectors_hold_old_unverified_jobs_until_liveness_confirmed(tmp_path):
    from applypilot import database

    conn = database.init_db(tmp_path / "fresh.db")
    _insert(conn, "u_score_old_unchecked", "x" * 200, posted_at="2000-01-01")
    _insert(conn, "u_score_old_live", "x" * 200, posted_at="2000-01-01", liveness_status="live")
    _insert(conn, "u_tailor_old_unchecked", "x" * 500, fit_score=8, posted_at="2000-01-01")
    _insert(conn, "u_tailor_old_live", "x" * 500, fit_score=8, posted_at="2000-01-01", liveness_status="live")
    conn.commit()

    score_urls = {j["url"] for j in database.get_jobs_by_stage(conn, "pending_score", limit=10)}
    tailor_urls = {j["url"] for j in database.get_jobs_by_stage(conn, "pending_tailor", min_score=7, limit=10)}

    assert "u_score_old_unchecked" not in score_urls
    assert "u_score_old_live" in score_urls
    assert "u_tailor_old_unchecked" not in tailor_urls
    assert "u_tailor_old_live" in tailor_urls


def test_pipeline_pending_counts_exclude_dead_cover_candidates(tmp_path, monkeypatch):
    from applypilot import database, pipeline

    conn = database.init_db(tmp_path / "counts.db")
    _insert(conn, "u_cover_dead", "x" * 500, fit_score=8, tailored_resume_path="r.pdf", liveness_status="dead")
    _insert(conn, "u_cover_live", "x" * 500, fit_score=8, tailored_resume_path="r.pdf", liveness_status="live")
    conn.commit()
    monkeypatch.setattr(pipeline, "get_connection", lambda: conn)

    assert pipeline._count_pending("cover", min_score=7) == 1


def test_rescore_query_excludes_descriptions_under_200(tmp_path):
    from applypilot import database
    from applypilot.scoring import scorer

    conn = database.init_db(tmp_path / "c.db")
    _insert(conn, "u_short", "x" * 199, fit_score=8)
    _insert(conn, "u_ok", "x" * 200, fit_score=8)
    conn.commit()

    urls = {row["url"] for row in conn.execute(scorer._RESCORE_QUERY)}

    assert urls == {"u_ok"}
