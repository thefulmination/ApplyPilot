"""PG-backed tests for fleet.sync.push_linkedin_eligible -- the LinkedIn lane's
home->PG push, and the issues that make its eligible set optimistic:

  - Issue 1 (liveness): LinkedIn can't be network-probed (blocked host), so the
    only safe pre-filter is recency. ``max_age_days`` drops stale postings.
  - Issue 2 (over-dedup / unapplyable): a job with no company or title can't be
    applied to and collapses on the (company,title) dedup_key -- exclude it.
  - Issue 3 (unscored backlog): unscored LinkedIn jobs are correctly excluded
    from apply, but must be *countable* (not silently lost) via a helper.
  - Issue 4 (duplicates): duplicate_of_url rows stay excluded (regression lock).

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import sync


_JOBS_DDL = """
CREATE TABLE jobs (
    url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
    audit_score REAL, fit_score INTEGER, full_description TEXT, liveness_status TEXT,
    apply_status TEXT, apply_error TEXT, duplicate_of_url TEXT,
    applied_at TEXT, discovered_at TEXT
);
"""


def _home_sqlite(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "home.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(_JOBS_DDL)
    return conn


def _iso(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%S")


def _add_li(conn, url, **kw):
    """Insert a LinkedIn job (effective host linkedin.com) with sane defaults."""
    cols = {"url": url, "application_url": url, "company": "Acme", "title": "Chief of Staff",
            "audit_score": 9.0, "liveness_status": "uncertain", "discovered_at": _iso(1)}
    cols.update(kw)
    conn.execute(f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 list(cols.values()))
    conn.commit()


def _pushed_urls(pg) -> set[str]:
    with pg.cursor() as cur:
        cur.execute("SELECT url FROM linkedin_queue")
        return {r["url"] for r in cur.fetchall()}


# --- baseline ---------------------------------------------------------------

def test_push_linkedin_pushes_clean_eligible(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/1", company="Acme", title="COS")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        assert _pushed_urls(pg) == {"https://www.linkedin.com/jobs/view/1"}


# --- Issue 2: null/empty company or title is unapplyable + over-dedups ------

def test_push_linkedin_excludes_null_or_empty_company_or_title(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/ok", company="Acme", title="COS")
    _add_li(sq, "https://www.linkedin.com/jobs/view/nocompany", company=None, title="COS")
    _add_li(sq, "https://www.linkedin.com/jobs/view/emptycompany", company="   ", title="COS")
    _add_li(sq, "https://www.linkedin.com/jobs/view/notitle", company="Acme", title=None)
    _add_li(sq, "https://www.linkedin.com/jobs/view/emptytitle", company="Acme", title="")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        assert _pushed_urls(pg) == {"https://www.linkedin.com/jobs/view/ok"}


# --- Issue 4: duplicates stay excluded (regression lock) --------------------

def test_push_linkedin_excludes_duplicates(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/keep", company="Acme", title="COS")
    _add_li(sq, "https://www.linkedin.com/jobs/view/dup", company="Acme", title="COS",
            duplicate_of_url="https://www.linkedin.com/jobs/view/keep")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        assert _pushed_urls(pg) == {"https://www.linkedin.com/jobs/view/keep"}


# --- Issue 1: recency filter (the only safe liveness proxy for LinkedIn) ----

def test_push_linkedin_max_age_days_filters_stale(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/fresh", company="Acme", title="COS",
            discovered_at=_iso(3))
    _add_li(sq, "https://www.linkedin.com/jobs/view/stale", company="Beta", title="COS",
            discovered_at=_iso(45))
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7, max_age_days=21)
        assert n == 1
        assert _pushed_urls(pg) == {"https://www.linkedin.com/jobs/view/fresh"}


def test_push_linkedin_max_age_days_none_keeps_stale(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/fresh", company="Acme", title="COS",
            discovered_at=_iso(3))
    _add_li(sq, "https://www.linkedin.com/jobs/view/stale", company="Beta", title="COS",
            discovered_at=_iso(45))
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7, max_age_days=None)
        assert n == 2


# --- Bug: LinkedIn link in `url` only (application_url NULL) must still push --

def test_push_linkedin_uses_url_when_application_url_null(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    # Common from scraping: the linkedin link is the primary `url`, application_url is NULL.
    # Eligibility matches (effective host = url), but linkedin_queue.application_url is NOT NULL,
    # so the push must stage the EFFECTIVE url, not the raw NULL.
    sq.execute("INSERT INTO jobs (url, application_url, company, title, audit_score, discovered_at) "
               "VALUES (?,?,?,?,?,?)",
               ("https://www.linkedin.com/jobs/view/999", None, "Acme", "COS", 9.0, _iso(1)))
    sq.commit()
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT application_url FROM linkedin_queue WHERE url='https://www.linkedin.com/jobs/view/999'")
            assert cur.fetchone()["application_url"] == "https://www.linkedin.com/jobs/view/999"


# --- Issue 3: unscored backlog is countable, not silently lost --------------

def test_count_linkedin_unscored(tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/scored", company="Acme", title="COS",
            audit_score=8.0)
    _add_li(sq, "https://www.linkedin.com/jobs/view/unscored", company="Beta", title="COS",
            audit_score=None, fit_score=None)
    _add_li(sq, "https://www.linkedin.com/jobs/view/unscored_dead", company="Gamma", title="COS",
            audit_score=None, fit_score=None, liveness_status="dead")  # dead -> not a candidate
    _add_li(sq, "https://www.linkedin.com/jobs/view/unscored_noco", company=None, title="COS",
            audit_score=None, fit_score=None)  # unapplyable -> not a candidate
    assert sync.count_linkedin_unscored(sq) == 1
