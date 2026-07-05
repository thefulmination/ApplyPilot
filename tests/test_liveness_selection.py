from __future__ import annotations

from applypilot import database
from applypilot.apply import liveness


def _insert(conn, url, *, audit_label=None, fit_score=7, audit_score=None, duplicate_of_url=None):
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, fit_score, audit_score, "
        "application_url, audit_label, duplicate_of_url) VALUES (?,?,?,?,?,?,?,?,?)",
        (url, "Job", "x", "desc", fit_score, audit_score, url, audit_label, duplicate_of_url),
    )


def test_verify_jobs_includes_approved_external_and_floor_candidates(monkeypatch, tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _insert(conn, "https://x.io/approved_external", audit_label="approved_external", fit_score=1, audit_score=6)
    _insert(conn, "https://x.io/floor", audit_label=None, fit_score=7)
    _insert(conn, "https://x.io/too_low", audit_label=None, fit_score=4)
    monkeypatch.setattr(liveness, "probe_url", lambda url, **kwargs: ("live", "ok"))

    r = liveness.verify_jobs(conn, score_floor=6, tiers=("priority", "recommended"), workers=1)
    assert r["checked"] == 2
    assert r["by_status"]["live"] == 2


def test_verify_jobs_score_floor_none_keeps_tier_only_behavior(monkeypatch, tmp_path):
    conn = database.init_db(tmp_path / "applypilot.db")
    _insert(conn, "https://x.io/approved_external", audit_label="approved_external", fit_score=7)
    _insert(conn, "https://x.io/priority", audit_label="priority", fit_score=1)
    monkeypatch.setattr(liveness, "probe_url", lambda url, **kwargs: ("live", "ok"))

    r = liveness.verify_jobs(conn, score_floor=None, tiers=("priority", "recommended"), workers=1)
    assert r["checked"] == 1
    assert r["by_status"]["live"] == 1
