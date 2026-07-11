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
from applypilot.fleet import queue, sync


@pytest.fixture(autouse=True)
def _register_canonical_pg_policy(request):
    if "fleet_db" not in request.fixturenames:
        yield
        return
    dsn = request.getfixturevalue("fleet_db")
    with pgqueue.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
            "VALUES ('canonical-linkedin-active-test','linkedin','active')"
        )
        cur.execute(
            "UPDATE fleet_config SET linkedin_policy_version='canonical-linkedin-active-test' WHERE id=1"
        )
        conn.commit()
    yield


_JOBS_DDL = """
CREATE TABLE jobs (
    url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
    audit_score REAL, fit_score INTEGER, full_description TEXT, liveness_status TEXT,
    apply_status TEXT, apply_error TEXT, duplicate_of_url TEXT,
    applied_at TEXT, discovered_at TEXT, decision_source TEXT,
    fit_gap_category TEXT, recommended_action TEXT, audit_flags TEXT,
    linkedin_unresolved_kind TEXT, linkedin_next_action TEXT,
    canonical_decision_id TEXT
);
CREATE TABLE decision_policy_versions (
    policy_version TEXT PRIMARY KEY, lane TEXT NOT NULL, status TEXT NOT NULL,
    config_json TEXT, created_at TEXT NOT NULL, UNIQUE(policy_version, lane)
);
CREATE TABLE job_decisions (
    decision_id TEXT PRIMARY KEY, job_url TEXT NOT NULL, policy_version TEXT NOT NULL,
    lane TEXT NOT NULL, qualification_score REAL, preference_score REAL,
    outcome_score REAL, final_score REAL, qualification_verdict TEXT NOT NULL,
    action TEXT NOT NULL, confidence REAL, input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL, expires_at TEXT
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
    canonical = kw.pop("canonical", True)
    action = kw.pop("canonical_action", "apply")
    verdict = kw.pop("canonical_verdict", "qualified")
    status = kw.pop("canonical_policy_status", "active")
    lane = kw.pop("canonical_policy_lane", "linkedin")
    expires_at = kw.pop("canonical_expires_at", None)
    cols = {"url": url, "application_url": url, "company": "Acme", "title": "Chief of Staff",
            "audit_score": 9.0, "liveness_status": "uncertain", "discovered_at": _iso(1),
            "full_description": "x" * 600}
    cols.update(kw)
    conn.execute(f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 list(cols.values()))
    if canonical:
        now = datetime.now(timezone.utc)
        policy = f"canonical-{lane}-{status}-test"
        decision_id = f"decision-{abs(hash(url))}"
        conn.execute(
            "INSERT OR IGNORE INTO decision_policy_versions VALUES (?,?,?,?,?)",
            (policy, lane, status, '{"qualificationFloor":7}', now.isoformat()),
        )
        conn.execute(
            "INSERT INTO job_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, url, policy, lane, 9.0, 8.0, 8.0, 9.0, verdict, action,
             0.9, f"hash-{decision_id}", now.isoformat(),
             expires_at or (now + timedelta(days=1)).isoformat()),
        )
        conn.execute("UPDATE jobs SET canonical_decision_id=? WHERE url=?", (decision_id, url))
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


def test_push_linkedin_excludes_thin_description(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/ok", full_description="x" * 600)
    _add_li(sq, "https://www.linkedin.com/jobs/view/thin", full_description="x" * 499)
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        assert _pushed_urls(pg) == {"https://www.linkedin.com/jobs/view/ok"}


def test_push_linkedin_does_not_use_legacy_title_lane_filter(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/onlane", title="Chief of Staff")
    _add_li(sq, "https://www.linkedin.com/jobs/view/offlane",
            title="Enterprise Account Executive")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 2
        assert _pushed_urls(pg) == {
            "https://www.linkedin.com/jobs/view/onlane",
            "https://www.linkedin.com/jobs/view/offlane",
        }


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
    _add_li(
        sq,
        "https://www.linkedin.com/jobs/view/999",
        application_url=None,
        company="Acme",
        title="COS",
    )
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT application_url FROM linkedin_queue WHERE url='https://www.linkedin.com/jobs/view/999'")
            assert cur.fetchone()["application_url"] == "https://www.linkedin.com/jobs/view/999"


@pytest.mark.parametrize(
    "kwargs",
    [
        {"canonical": False},
        {"canonical_action": "review"},
        {"canonical_action": "reject"},
        {"canonical_verdict": "unqualified"},
        {"canonical_policy_status": "draft"},
        {"canonical_policy_lane": "ats"},
        {"canonical_expires_at": "2020-01-01T00:00:00+00:00"},
    ],
)
def test_push_linkedin_rejects_non_authoritative_canonical_rows(
    fleet_db, tmp_path, kwargs
):
    sq = _home_sqlite(tmp_path)
    _add_li(sq, "https://www.linkedin.com/jobs/view/rejected", **kwargs)
    with pgqueue.connect(fleet_db) as pg:
        assert sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg) == 0


# --- Issue 3: unscored backlog is countable, not silently lost --------------

def test_push_linkedin_jobs_carries_unresolved_metadata(fleet_db):
    now = datetime.now(timezone.utc)
    with pgqueue.connect(fleet_db) as pg:
        n = queue.push_linkedin_jobs(
            pg,
            [{
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
                "decision_id": "decision-unresolved",
                "policy_version": "canonical-linkedin-active-test",
                "decision_action": "apply",
                "qualification_verdict": "qualified",
                "qualification_score": 9.0,
                "qualification_floor": 7.0,
                "preference_score": 8.0,
                "outcome_score": 8.0,
                "final_score": 9.0,
                "decision_confidence": 0.9,
                "decision_created_at": now,
                "decision_expires_at": now + timedelta(days=1),
                "input_hash": "hash-unresolved",
            }],
        )
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("""
                SELECT linkedin_unresolved_kind, linkedin_next_action
                  FROM linkedin_queue
                 WHERE url = 'https://linkedin.com/jobs/unresolved'
            """)
            row = cur.fetchone()
    assert row["linkedin_unresolved_kind"] == "apply_button_missing"
    assert row["linkedin_next_action"] == "run_ats_reconstruction"


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
