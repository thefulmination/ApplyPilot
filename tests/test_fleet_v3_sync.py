"""PG-backed tests for the v3 home-brain <-> coordination-Postgres bridge (fleet.sync).

Mirrors tests/test_fleet_pgqueue.py's fleet_sync tests, on the v3 queues:
  - push_apply_eligible pushes ONLY eligible rows, with a dedup_key + the approved_batch.
  - pull_apply_results maps a confirmed apply to the brain and is idempotent on re-pull.
  - push/pull_compute_eligible write compute results back as ADVISORY (research_*) only.

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema applied).
"""
from __future__ import annotations

import sqlite3

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue
from applypilot.fleet import queue as fleet_queue
from applypilot.fleet import sync


# ---------------------------------------------------------------------------
# Temp SQLite brain (minimal jobs table) -- mirrors the fleet_sync test DDL,
# plus the advisory research_* columns the compute pull writes.
# ---------------------------------------------------------------------------
_JOBS_DDL = """
CREATE TABLE jobs (
    url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
    audit_score REAL, fit_score INTEGER, full_description TEXT, liveness_status TEXT,
    apply_status TEXT, apply_error TEXT, duplicate_of_url TEXT,
    applied_at TEXT, agent_id TEXT, verification_confidence TEXT,
    apply_duration_ms INTEGER, apply_attempts INTEGER DEFAULT 0,
    research_fit_score REAL, research_decision TEXT
);
"""


def _home_sqlite(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "home.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(_JOBS_DDL)
    return conn


def _add_job(conn, url, **kw):
    cols = {"url": url, "application_url": url, "audit_score": 8.0, "liveness_status": "live"}
    cols.update(kw)
    conn.execute(f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 list(cols.values()))
    conn.commit()


# ---------------------------------------------------------------------------
# APPLY push
# ---------------------------------------------------------------------------

def test_push_apply_eligible_filters_and_stamps(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/acme/jobs/1",
             company="Acme Inc", title="Chief of Staff")                       # eligible
    _add_job(sq, "https://www.linkedin.com/jobs/view/123", audit_score=9.0)    # linkedin -> skip
    _add_job(sq, "https://boards.greenhouse.io/x/jobs/2",
             company="X", title="Analyst", apply_status="applied")            # applied -> skip
    _add_job(sq, "https://boards.greenhouse.io/x/jobs/3",
             company="X", title="Analyst", apply_status="in_progress")        # in-flight -> skip
    _add_job(sq, "https://boards.greenhouse.io/y/jobs/4",
             company="Y", title="PM", audit_score=5.0)                        # below floor -> skip
    _add_job(sq, "https://boards.greenhouse.io/z/jobs/5",
             company="Z", title="Eng", audit_score=9.0,
             liveness_status="dead")                                          # dead -> skip
    _add_job(sq, "https://boards.greenhouse.io/d/jobs/6",
             company="D", title="Eng", audit_score=9.0,
             duplicate_of_url="https://boards.greenhouse.io/acme/jobs/1")     # dedup'd -> skip

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg,
                                     score_floor=7, approved_batch="batch-A", limit=None)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url, target_host, apply_domain, dedup_key, approved_batch, "
                        "lane, status FROM apply_queue")
            rows = cur.fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row["url"].endswith("/acme/jobs/1")
    assert row["target_host"] == "boards.greenhouse.io"
    assert row["apply_domain"] == "boards.greenhouse.io"
    assert row["approved_batch"] == "batch-A"
    assert row["lane"] == "ats"
    assert row["status"] == "queued"
    # dedup_key is the board-agnostic (company, role) hash -- present + correct.
    assert row["dedup_key"]
    from applypilot.fleet import dedup
    assert row["dedup_key"] == dedup.dedup_key("Acme Inc", "Chief of Staff")


def test_push_apply_eligible_skips_company_blocklist_matches(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/acme/jobs/1",
             company="Acme Inc", title="Chief of Staff")                       # eligible
    _add_job(sq, "https://boards.greenhouse.io/openai/jobs/2",
             company="OpenAI", title="Strategy")                               # blocked by company
    _add_job(sq, "https://hiring.cafe/viewjob/openai-3",
             company="HiringCafe", title="Ops",
             application_url="https://jobs.ashbyhq.com/openai/3")              # blocked by app url

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg,
                                     score_floor=7, approved_batch="batch-A", limit=None)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM apply_queue")
            urls = {r["url"] for r in cur.fetchall()}

    assert urls == {"https://boards.greenhouse.io/acme/jobs/1"}


def test_push_linkedin_eligible_skips_company_blocklist_matches(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://www.linkedin.com/jobs/view/acme",
             company="Acme", title="Chief of Staff", audit_score=9.0)
    _add_job(sq, "https://www.linkedin.com/jobs/view/openai",
             company="OpenAI", title="Strategy", audit_score=10.0)

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg,
                                        score_floor=7, approved_batch="batch-L", limit=None)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM linkedin_queue")
            urls = {r["url"] for r in cur.fetchall()}

    assert urls == {"https://www.linkedin.com/jobs/view/acme"}


def test_push_apply_eligible_excludes_crash_unconfirmed(fleet_db, tmp_path):
    # A posting that may already have been submitted under the user's name (a
    # crash_unconfirmed / no_confirmation terminal) must NEVER be re-pushed.
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/ok/jobs/1", company="OK", title="COS")  # eligible
    _add_job(sq, "https://boards.greenhouse.io/cu/jobs/2", company="CU", title="COS",
             audit_score=9.0, apply_status="crash_unconfirmed")                        # possibly-submitted
    _add_job(sq, "https://boards.greenhouse.io/ce/jobs/3", company="CE", title="COS",
             audit_score=9.0, apply_error="no_confirmation")                           # possibly-submitted
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7, approved_batch="b1")
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM apply_queue")
            urls = {r["url"] for r in cur.fetchall()}
    assert any(u.endswith("/ok/jobs/1") for u in urls)
    assert not any(("/cu/jobs/2" in u or "/ce/jobs/3" in u) for u in urls), \
        "a crash_unconfirmed / no_confirmation posting must not be re-pushed"


def test_push_apply_eligible_respects_limit(fleet_db, tmp_path):
    # limit is pushed into SQL (top-N by score), not just a post-fetch slice.
    sq = _home_sqlite(tmp_path)
    for i in range(3):
        _add_job(sq, f"https://boards.greenhouse.io/x/jobs/{i}",
                 company=f"C{i}", title="COS", audit_score=float(9 - i))
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7,
                                     approved_batch="b1", limit=2)
        assert n == 2
        with pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM apply_queue")
            assert cur.fetchone()["n"] == 2


def test_push_apply_eligible_is_idempotent(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://jobs.lever.co/acme/1", company="Acme", title="COS")
    with pgqueue.connect(fleet_db) as pg:
        sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, approved_batch="b1")
        sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, approved_batch="b1")  # re-push
        with pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM apply_queue")
            assert cur.fetchone()["n"] == 1   # no duplicates


# ---------------------------------------------------------------------------
# APPLY pull
# ---------------------------------------------------------------------------

def test_pull_apply_results_maps_applied_and_idempotent(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    url = "https://boards.greenhouse.io/acme/jobs/1"
    _add_job(sq, url, company="Acme", title="COS")
    with pgqueue.connect(fleet_db) as pg:
        sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, approved_batch="b1")
        job = fleet_queue.lease_apply(pg, "w1", home_ip="1.2.3.4")
        assert job is not None and job["url"] == url
        ok = fleet_queue.write_apply_result(
            pg, "w1", url, status="applied", target_host="boards.greenhouse.io",
            home_ip="1.2.3.4", apply_status="applied", est_cost_usd=0.6)
        assert ok is True

        assert sync.pull_apply_results(sqlite_conn=sq, pg_conn=pg).get("applied") == 1
        brain = sq.execute("SELECT apply_status, applied_at FROM jobs WHERE url=?", (url,)).fetchone()
        assert brain["apply_status"] == "applied"
        assert brain["applied_at"] is not None

        # re-pull is a no-op (PG row stamped synced_to_home_at)
        assert sync.pull_apply_results(sqlite_conn=sq, pg_conn=pg) == {}


def test_pull_apply_results_never_demotes_confirmed_apply(fleet_db, tmp_path):
    """A blocked terminal arriving for a url the brain already marks 'applied' must not demote it."""
    sq = _home_sqlite(tmp_path)
    url = "https://boards.greenhouse.io/acme/jobs/9"
    _add_job(sq, url, company="Acme", title="COS", apply_status="applied")
    with pgqueue.connect(fleet_db) as pg:
        # Force a terminal 'blocked' PG row for this url (simulate a stale fleet result).
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue (url, application_url, score, status, apply_error) "
                "VALUES (%s,%s,%s,'blocked','cloudflare')", (url, url, 8.0))
        pg.commit()
        sync.pull_apply_results(sqlite_conn=sq, pg_conn=pg)
        row = sq.execute("SELECT apply_status FROM jobs WHERE url=?", (url,)).fetchone()
        assert row["apply_status"] == "applied"   # never demoted


def test_pull_apply_results_blocked_maps_to_failed_and_pins(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    url = "https://jobs.lever.co/acme/2"
    _add_job(sq, url, company="Acme", title="COS")
    with pgqueue.connect(fleet_db) as pg:
        sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, approved_batch="b1")
        job = fleet_queue.lease_apply(pg, "w1", home_ip="1.2.3.4")
        fleet_queue.write_apply_result(
            pg, "w1", job["url"], status="blocked", target_host="jobs.lever.co",
            home_ip="1.2.3.4", apply_error="cloudflare")
        sync.pull_apply_results(sqlite_conn=sq, pg_conn=pg)
        row = sq.execute("SELECT apply_status, apply_error, apply_attempts FROM jobs WHERE url=?",
                         (url,)).fetchone()
        assert row["apply_status"] == "failed"     # blocked -> failed home-side
        assert row["apply_error"] == "cloudflare"
        assert row["apply_attempts"] == 99


# ---------------------------------------------------------------------------
# COMPUTE push + pull (advisory)
# ---------------------------------------------------------------------------

def test_push_compute_eligible_enqueues_with_payload(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://co/jobs/1", company="Co", title="Analyst")
    _add_job(sq, "https://co/jobs/2", company="Co", title="PM",
             duplicate_of_url="https://co/jobs/1")   # dedup'd -> skip
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_compute_eligible(sqlite_conn=sq, pg_conn=pg, task="score")
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url, task, payload, status FROM compute_queue")
            rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["task"] == "score"
    assert rows[0]["status"] == "queued"
    assert rows[0]["payload"]["company"] == "Co"


def test_pull_compute_results_is_advisory_only_and_idempotent(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    url = "https://co/jobs/1"
    _add_job(sq, url, company="Co", title="Analyst", audit_score=8.0, fit_score=6)
    with pgqueue.connect(fleet_db) as pg:
        sync.push_compute_eligible(sqlite_conn=sq, pg_conn=pg, task="score")
        job = fleet_queue.lease_compute(pg, "c1")
        assert job is not None and job["url"] == url
        fleet_queue.write_compute_result(
            pg, "c1", url,
            result={"research_fit_score": 9.5, "research_decision": "strong_qualified"},
            status="done", cost_usd=0.01, model="deepseek-chat", task="score")

        assert sync.pull_compute_results(sqlite_conn=sq, pg_conn=pg) == 1
        row = sq.execute("SELECT research_fit_score, research_decision, fit_score, audit_score "
                         "FROM jobs WHERE url=?", (url,)).fetchone()
        # advisory columns written ...
        assert row["research_fit_score"] == 9.5
        assert row["research_decision"] == "strong_qualified"
        # ... but fit_score / audit_score NEVER auto-promoted
        assert row["fit_score"] == 6
        assert row["audit_score"] == 8.0

        # re-pull is a no-op
        assert sync.pull_compute_results(sqlite_conn=sq, pg_conn=pg) == 0


# ---------------------------------------------------------------------------
# LINKEDIN pull (regression: pull was a report-only stub -- LinkedIn applies
# never reached the brain, so brain-driven paths saw them as never-applied)
# ---------------------------------------------------------------------------

def test_pull_linkedin_results_maps_applied_and_idempotent(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    url = "https://www.linkedin.com/jobs/view/12345"
    _add_job(sq, url, company="Acme", title="COS")
    with pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue (url, application_url, score, status, applied_at) "
                "VALUES (%s,%s,%s,'applied', now())", (url, url, 8.0))
        pg.commit()

        assert sync.pull_linkedin_results(sqlite_conn=sq, pg_conn=pg).get("applied") == 1
        brain = sq.execute("SELECT apply_status, applied_at FROM jobs WHERE url=?", (url,)).fetchone()
        assert brain["apply_status"] == "applied"
        assert brain["applied_at"] is not None

        # re-pull is a no-op (linkedin_queue row stamped synced_to_home_at)
        assert sync.pull_linkedin_results(sqlite_conn=sq, pg_conn=pg) == {}


def test_lane_pulls_do_not_cross_consume(fleet_db, tmp_path):
    """The ATS pull must not stamp/ingest linkedin_queue rows and vice versa."""
    sq = _home_sqlite(tmp_path)
    li_url = "https://www.linkedin.com/jobs/view/777"
    _add_job(sq, li_url, company="L", title="COS")
    with pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue (url, application_url, score, status, applied_at) "
                "VALUES (%s,%s,%s,'applied', now())", (li_url, li_url, 8.0))
        pg.commit()

        assert sync.pull_apply_results(sqlite_conn=sq, pg_conn=pg) == {}   # ats pull: no-op
        with pg.cursor() as cur:
            cur.execute("SELECT synced_to_home_at FROM linkedin_queue WHERE url=%s", (li_url,))
            assert cur.fetchone()["synced_to_home_at"] is None             # untouched

        assert sync.pull_linkedin_results(sqlite_conn=sq, pg_conn=pg).get("applied") == 1
