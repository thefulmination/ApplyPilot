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
from applypilot.fleet import queue
from applypilot.fleet import sync


_JOBS_DDL = """
CREATE TABLE jobs (
    url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
    audit_score REAL, fit_score INTEGER, research_fit_score REAL,
    full_description TEXT, liveness_status TEXT,
    apply_status TEXT, apply_error TEXT, duplicate_of_url TEXT,
    applied_at TEXT, discovered_at TEXT, decision_source TEXT,
    fit_gap_category TEXT, recommended_action TEXT, audit_flags TEXT,
    linkedin_resolve_status TEXT, linkedin_resolved_at TEXT,
    linkedin_resolve_error TEXT,
    linkedin_unresolved_kind TEXT, linkedin_next_action TEXT
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
            "audit_score": 9.0, "liveness_status": "uncertain", "discovered_at": _iso(1),
            "full_description": "x" * 600, "linkedin_resolve_status": "easy_apply",
            "linkedin_resolved_at": _iso(0)}
    cols.update(kw)
    conn.execute(f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 list(cols.values()))
    conn.commit()


def _pushed_urls(pg) -> set[str]:
    with pg.cursor() as cur:
        cur.execute("SELECT url FROM linkedin_queue")
        return {r["url"] for r in cur.fetchall()}


# --- baseline ---------------------------------------------------------------

def test_push_linkedin_jobs_carries_unresolved_metadata(fleet_db):
    with pgqueue.connect(fleet_db) as pg:
        n = queue.push_linkedin_jobs(
            pg,
            [
                {
                    "url": "https://linkedin.com/jobs/unresolved",
                    "company": "Acme",
                    "title": "Role",
                    "application_url": "https://linkedin.com/jobs/unresolved",
                    "score": 9.0,
                    "linkedin_resolve_status": "unresolved",
                    "linkedin_resolved_at": "2026-07-07T12:00:00",
                    "linkedin_resolve_error": "no_primary_apply_button",
                    "linkedin_unresolved_kind": "apply_button_missing",
                    "linkedin_next_action": "run_ats_reconstruction",
                }
            ],
        )
        assert n == 1
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT linkedin_unresolved_kind, linkedin_next_action
                  FROM linkedin_queue
                 WHERE url = 'https://linkedin.com/jobs/unresolved'
                """
            )
            row = cur.fetchone()

    assert row["linkedin_unresolved_kind"] == "apply_button_missing"
    assert row["linkedin_next_action"] == "run_ats_reconstruction"


def test_push_linkedin_pushes_clean_eligible(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/1", company="Acme", title="COS")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        assert _pushed_urls(pg) == {"https://www.linkedin.com/jobs/view/1"}


def test_push_linkedin_can_include_advisory_research_score(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    url = "https://www.linkedin.com/jobs/view/research-scored"
    _add_li(
        sq,
        url,
        company="Acme",
        title="Chief of Staff",
        audit_score=None,
        fit_score=None,
        research_fit_score=8.0,
    )

    with pgqueue.connect(fleet_db) as pg:
        assert sync.push_linkedin_eligible(
            sqlite_conn=sq,
            pg_conn=pg,
            score_floor=7,
        ) == 0
        assert sync.push_linkedin_eligible(
            sqlite_conn=sq,
            pg_conn=pg,
            score_floor=7,
            include_research=True,
        ) == 1
        assert _pushed_urls(pg) == {url}


def test_push_linkedin_requires_recent_positive_resolver_status(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(
        sq,
        "https://www.linkedin.com/jobs/view/fresh",
        company="Acme",
        title="COS",
        linkedin_resolve_status="easy_apply",
        linkedin_resolved_at=_iso(0),
    )
    _add_li(
        sq,
        "https://www.linkedin.com/jobs/view/unresolved",
        company="Beta",
        title="COS",
        linkedin_resolve_status=None,
        linkedin_resolved_at=None,
    )
    _add_li(
        sq,
        "https://www.linkedin.com/jobs/view/unavailable",
        company="Gamma",
        title="COS",
        linkedin_resolve_status="unavailable",
        linkedin_resolved_at=_iso(0),
    )
    _add_li(
        sq,
        "https://www.linkedin.com/jobs/view/stale-check",
        company="Delta",
        title="COS",
        linkedin_resolve_status="easy_apply",
        linkedin_resolved_at=_iso(10),
    )

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute(
                "SELECT url, linkedin_resolve_status, linkedin_resolved_at "
                "FROM linkedin_queue"
            )
            rows = cur.fetchall()

    assert [row["url"] for row in rows] == ["https://www.linkedin.com/jobs/view/fresh"]
    assert rows[0]["linkedin_resolve_status"] == "easy_apply"
    assert rows[0]["linkedin_resolved_at"] is not None


def test_push_linkedin_excludes_thin_description(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/ok", full_description="x" * 600)
    _add_li(sq, "https://www.linkedin.com/jobs/view/thin", full_description="x" * 499)
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        assert _pushed_urls(pg) == {"https://www.linkedin.com/jobs/view/ok"}


def test_push_linkedin_filters_off_lane_by_default(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/onlane", title="Chief of Staff")
    _add_li(sq, "https://www.linkedin.com/jobs/view/offlane",
            title="Enterprise Account Executive")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        assert _pushed_urls(pg) == {"https://www.linkedin.com/jobs/view/onlane"}


def test_push_linkedin_keeps_human_decision_off_lane_rows(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/human",
            title="Enterprise Account Executive", decision_source="human_review",
            fit_gap_category="wrong_role_lane", recommended_action="ignore")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        assert _pushed_urls(pg) == {"https://www.linkedin.com/jobs/view/human"}


def test_push_linkedin_can_disable_lane_filter(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/offlane",
            title="Enterprise Account Executive")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7,
                                        lane_filter=False)
        assert n == 1
        assert _pushed_urls(pg) == {"https://www.linkedin.com/jobs/view/offlane"}


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


def test_push_linkedin_max_age_days_retires_existing_stale_queue_rows(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    stale_url = "https://www.linkedin.com/jobs/view/stale-existing"
    fresh_url = "https://www.linkedin.com/jobs/view/fresh-existing"
    _add_li(sq, fresh_url, company="Acme", title="COS", discovered_at=_iso(3))
    _add_li(sq, stale_url, company="Beta", title="COS", discovered_at=_iso(45))

    with pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO linkedin_queue
                    (url, company, title, application_url, score, status, approved_batch)
                VALUES
                    (%s, 'Beta', 'COS', %s, 9.0, 'queued', 'legacy-batch')
                """,
                (stale_url, stale_url),
            )
        pg.commit()

        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7, max_age_days=21)
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT url, status, apply_status, apply_error, approved_batch
                  FROM linkedin_queue
                 WHERE url = ANY(%s)
                 ORDER BY url
                """,
                ([fresh_url, stale_url],),
            )
            rows = {r["url"]: r for r in cur.fetchall()}

    assert n == 1
    assert rows[fresh_url]["status"] == "queued"
    assert rows[stale_url]["status"] == "failed"
    assert rows[stale_url]["apply_status"] == "failed"
    assert rows[stale_url]["apply_error"] == "expired"
    assert rows[stale_url]["approved_batch"] == "legacy-batch"


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
    sq.execute(
        "INSERT INTO jobs "
        "(url, application_url, company, title, audit_score, discovered_at, full_description, "
        "linkedin_resolve_status, linkedin_resolved_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (
            "https://www.linkedin.com/jobs/view/999",
            None,
            "Acme",
            "COS",
            9.0,
            _iso(1),
            "x" * 600,
            "easy_apply",
            _iso(0),
        ),
    )
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
    _add_li(sq, "https://www.linkedin.com/jobs/view/research-scored", company="Delta", title="COS",
            audit_score=None, fit_score=None, research_fit_score=8.0)
    _add_li(sq, "https://www.linkedin.com/jobs/view/unscored_dead", company="Gamma", title="COS",
            audit_score=None, fit_score=None, liveness_status="dead")  # dead -> not a candidate
    _add_li(sq, "https://www.linkedin.com/jobs/view/unscored_noco", company=None, title="COS",
            audit_score=None, fit_score=None)  # unapplyable -> not a candidate
    assert sync.count_linkedin_unscored(sq) == 2
    assert sync.count_linkedin_unscored(sq, include_research=True) == 1
