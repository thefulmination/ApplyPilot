from __future__ import annotations

import datetime as _dt

from applypilot import database
from applypilot.apply import liveness


def _seed_job(
    conn,
    url: str,
    *,
    last_verified_live: str | None,
    audit_label: str = "priority",
    discovered_at: str | None = None,
    fit_score: int = 8,
    audit_score: float = 9.0,
    posted_at: str | None = None,
    valid_through: str | None = None,
    liveness_status: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO jobs (url, title, site, company, tailored_resume_path, fit_score, audit_score, "
        "audit_label, last_verified_live, discovered_at, posted_at, valid_through, liveness_status) "
        "VALUES (?, 'Job', 'TestCo', 'Acme', 'x', ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            url, fit_score, audit_score, audit_label, last_verified_live, discovered_at,
            posted_at, valid_through, liveness_status,
        ),
    )
    conn.commit()


def test_verify_jobs_uses_oldest_verdict_first(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    now = _dt.datetime.now(_dt.timezone.utc)
    _seed_job(conn, "https://jobs.example.com/newest", last_verified_live=None)
    _seed_job(
        conn,
        "https://jobs.example.com/oldest",
        last_verified_live=(now - _dt.timedelta(days=10)).isoformat(),
    )
    _seed_job(
        conn,
        "https://jobs.example.com/middle",
        last_verified_live=(now - _dt.timedelta(days=5)).isoformat(),
    )

    seen = []

    def _fake_probe(url: str, meta: dict | None = None):
        seen.append(url)
        return "live", "ok"

    monkeypatch.setattr(liveness, "probe_url", _fake_probe)
    r = liveness.verify_jobs(conn, tiers=("priority",), max_age_days=0, workers=1)

    assert r["checked"] == 3
    assert seen == [
        "https://jobs.example.com/newest",
        "https://jobs.example.com/oldest",
        "https://jobs.example.com/middle",
    ]


def test_verify_jobs_backfills_posted_at_and_valid_through(tmp_path, monkeypatch):
    conn = database.init_db(tmp_path / "applypilot.db")
    _seed_job(conn, "https://jobs.example.com/posted", last_verified_live=None, discovered_at=_dt.datetime.now(_dt.timezone.utc).isoformat())

    body = '''
    <html><head><script type="application/ld+json">
    {"datePosted":"2026-06-10","validThrough":"2026-07-10"}
    </script></head><body>Job details</body></html>
    '''

    def _fake_fetch(url: str, accept: str | None = None):
        return 200, url, body

    monkeypatch.setattr(liveness, "_fetch", _fake_fetch)

    r = liveness.verify_jobs(conn, tiers=("priority",), max_age_days=0, workers=1)
    assert r["checked"] == 1
    row = conn.execute("SELECT posted_at, valid_through FROM jobs WHERE url='https://jobs.example.com/posted'").fetchone()
    assert row["posted_at"] == "2026-06-10"
    assert row["valid_through"] == "2026-07-10"

    # Existing non-null values are never overwritten.
    conn.execute(
        "UPDATE jobs SET posted_at='1999-01-01', valid_through='1999-12-31' WHERE url='https://jobs.example.com/posted'"
    )
    conn.commit()
    r = liveness.verify_jobs(conn, tiers=("priority",), max_age_days=0, workers=1)
    assert r["checked"] == 1
    row = conn.execute("SELECT posted_at, valid_through FROM jobs WHERE url='https://jobs.example.com/posted'").fetchone()
    assert row["posted_at"] == "1999-01-01"
    assert row["valid_through"] == "1999-12-31"
