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
    research_fit_score REAL, research_decision TEXT,
    discovered_at TEXT, decision_source TEXT, fit_gap_category TEXT,
    recommended_action TEXT, audit_flags TEXT,
    linkedin_unresolved_kind TEXT, linkedin_next_action TEXT
);
"""


def _home_sqlite(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "home.db"))
    conn.row_factory = sqlite3.Row
    conn.executescript(_JOBS_DDL)
    return conn


def _add_job(conn, url, **kw):
    cols = {"url": url, "application_url": url, "audit_score": 8.0,
            "liveness_status": "live", "full_description": "x" * 600}
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
    _add_job(sq, "https://boards.greenhouse.io/thin/jobs/7",
             company="Thin", title="PM", full_description="x" * 499)           # too thin -> skip
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


def test_push_apply_eligible_recovers_blank_company_from_greenhouse_board(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(
        sq,
        "source-job",
        company=None,
        title="Business Operations",
        application_url="https://job-boards.greenhouse.io/kikoff/jobs/4187038009",
    )

    with pgqueue.connect(fleet_db) as pg:
        assert sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, approved_batch="b1") == 1
        with pg.cursor() as cur:
            cur.execute("SELECT company, dedup_key FROM apply_queue WHERE url='source-job'")
            row = cur.fetchone()

    from applypilot.fleet import dedup
    assert row["company"] == "kikoff"
    assert row["dedup_key"] == dedup.dedup_key("kikoff", "Business Operations")


def test_push_apply_rows_parks_untrusted_workday_hosts(fleet_db):
    rows = [
        {
            "url": "job-workday",
            "application_url": "https://adobe.wd5.myworkdayjobs.com/external/job/1",
            "company": "Adobe",
            "title": "Analyst",
            "score": 8.5,
            "apply_domain": "adobe.wd5.myworkdayjobs.com",
            "target_host": "adobe.wd5.myworkdayjobs.com",
            "dedup_key": "dedup-workday",
        },
        {
            "url": "job-greenhouse",
            "application_url": "https://boards.greenhouse.io/acme/jobs/1",
            "company": "Acme",
            "title": "Analyst",
            "score": 8.5,
            "apply_domain": "boards.greenhouse.io",
            "target_host": "boards.greenhouse.io",
            "dedup_key": "dedup-greenhouse",
        },
    ]

    with pgqueue.connect(fleet_db) as pg:
        result = sync.push_apply_rows(
            pg, rows, approved_batch="b1", enforce_host_policy=True
        )
        with pg.cursor() as cur:
            cur.execute(
                "SELECT url, status, apply_status, apply_error, approved_batch, "
                "lane, target_host FROM apply_queue ORDER BY url"
            )
            queued = {row["url"]: row for row in cur.fetchall()}

    assert result == {"pushed": 1, "parked": 1}
    assert queued["job-greenhouse"]["status"] == "queued"
    assert queued["job-greenhouse"]["apply_error"] is None
    assert queued["job-greenhouse"]["approved_batch"] == "b1"
    assert queued["job-greenhouse"]["lane"] == "ats"
    assert queued["job-greenhouse"]["target_host"] == "boards.greenhouse.io"

    assert queued["job-workday"]["status"] == "failed"
    assert queued["job-workday"]["apply_status"] == "skipped"
    assert queued["job-workday"]["apply_error"] == "host_policy:workday_tenant_requires_trust"
    assert queued["job-workday"]["approved_batch"] == "b1"
    assert queued["job-workday"]["target_host"] == "adobe.wd5.myworkdayjobs.com"


def test_push_apply_rows_leaves_active_and_terminal_workday_conflicts_untouched(fleet_db):
    rows = [
        {
            "url": "job-workday-leased",
            "application_url": "https://adobe.wd5.myworkdayjobs.com/external/job/1",
            "company": "Adobe",
            "title": "Analyst",
            "score": 8.5,
            "apply_domain": "adobe.wd5.myworkdayjobs.com",
            "target_host": "adobe.wd5.myworkdayjobs.com",
            "dedup_key": "dedup-workday-leased",
        },
        {
            "url": "job-workday-applied",
            "application_url": "https://adobe.wd5.myworkdayjobs.com/external/job/2",
            "company": "Adobe",
            "title": "Senior Analyst",
            "score": 8.5,
            "apply_domain": "adobe.wd5.myworkdayjobs.com",
            "target_host": "adobe.wd5.myworkdayjobs.com",
            "dedup_key": "dedup-workday-applied",
        },
    ]

    with pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, apply_domain, target_host, "
                "lane, dedup_key, approved_batch, status, lease_owner, lease_expires_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'ats',%s,%s,'leased',%s,now() + interval '30 minutes')",
                (
                    "job-workday-leased",
                    "Adobe",
                    "Analyst",
                    "https://adobe.wd5.myworkdayjobs.com/external/job/1",
                    8.5,
                    "adobe.wd5.myworkdayjobs.com",
                    "adobe.wd5.myworkdayjobs.com",
                    "dedup-workday-leased",
                    "old-batch",
                    "w-active",
                ),
            )
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, apply_domain, target_host, "
                "lane, dedup_key, approved_batch, status, apply_status, applied_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'ats',%s,%s,'applied','applied',now())",
                (
                    "job-workday-applied",
                    "Adobe",
                    "Senior Analyst",
                    "https://adobe.wd5.myworkdayjobs.com/external/job/2",
                    8.5,
                    "adobe.wd5.myworkdayjobs.com",
                    "adobe.wd5.myworkdayjobs.com",
                    "dedup-workday-applied",
                    "old-batch",
                ),
            )
        pg.commit()

        result = sync.push_apply_rows(
            pg, rows, approved_batch="b1", enforce_host_policy=True
        )
        with pg.cursor() as cur:
            cur.execute(
                "SELECT url, status, apply_status, apply_error, lease_owner, "
                "lease_expires_at, approved_batch FROM apply_queue ORDER BY url"
            )
            queued = {row["url"]: row for row in cur.fetchall()}

    assert result == {"pushed": 0, "parked": 0}
    leased = queued["job-workday-leased"]
    assert leased["status"] == "leased"
    assert leased["lease_owner"] == "w-active"
    assert leased["lease_expires_at"] is not None
    assert leased["apply_status"] is None
    assert leased["apply_error"] is None
    assert leased["approved_batch"] == "old-batch"

    applied = queued["job-workday-applied"]
    assert applied["status"] == "applied"
    assert applied["apply_status"] == "applied"
    assert applied["apply_error"] is None
    assert applied["approved_batch"] == "old-batch"


def test_push_apply_rows_allows_trusted_workday_hosts(fleet_db):
    rows = [
        {
            "url": "job-workday",
            "application_url": "https://adobe.wd5.myworkdayjobs.com/external/job/1",
            "company": "Adobe",
            "title": "Analyst",
            "score": 8.5,
            "apply_domain": "adobe.wd5.myworkdayjobs.com",
            "target_host": "adobe.wd5.myworkdayjobs.com",
            "dedup_key": "dedup-workday",
        },
    ]

    with pgqueue.connect(fleet_db) as pg:
        result = sync.push_apply_rows(
            pg,
            rows,
            approved_batch="b1",
            enforce_host_policy=True,
            trusted_hosts={"adobe.wd5.myworkdayjobs.com": "canary"},
        )
        with pg.cursor() as cur:
            cur.execute(
                "SELECT status, apply_status, apply_error, approved_batch, lane, target_host "
                "FROM apply_queue WHERE url='job-workday'"
            )
            queued = cur.fetchone()

    assert result == {"pushed": 1, "parked": 0}
    assert queued["status"] == "queued"
    assert queued["apply_status"] is None
    assert queued["apply_error"] is None
    assert queued["approved_batch"] == "b1"
    assert queued["lane"] == "ats"
    assert queued["target_host"] == "adobe.wd5.myworkdayjobs.com"


def test_push_apply_eligible_parks_untrusted_workday_by_default(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(
        sq,
        "job-workday",
        application_url="https://adobe.wd5.myworkdayjobs.com/external/job/1",
        company="Adobe",
        title="Analyst",
        audit_score=8.5,
    )
    _add_job(
        sq,
        "job-greenhouse",
        application_url="https://boards.greenhouse.io/acme/jobs/1",
        company="Acme",
        title="Analyst",
        audit_score=8.5,
    )

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(
            sqlite_conn=sq, pg_conn=pg, score_floor=7, approved_batch="b1"
        )
        with pg.cursor() as cur:
            cur.execute(
                "SELECT url, status, apply_status, apply_error, approved_batch, "
                "lane, target_host FROM apply_queue ORDER BY url"
            )
            queued = {row["url"]: row for row in cur.fetchall()}

    assert n == 1
    assert queued["job-greenhouse"]["status"] == "queued"
    assert queued["job-greenhouse"]["apply_error"] is None
    assert queued["job-greenhouse"]["approved_batch"] == "b1"
    assert queued["job-greenhouse"]["lane"] == "ats"
    assert queued["job-greenhouse"]["target_host"] == "boards.greenhouse.io"
    assert queued["job-workday"]["status"] == "failed"
    assert queued["job-workday"]["apply_status"] == "skipped"
    assert queued["job-workday"]["apply_error"] == "host_policy:workday_tenant_requires_trust"
    assert queued["job-workday"]["approved_batch"] == "b1"
    assert queued["job-workday"]["target_host"] == "adobe.wd5.myworkdayjobs.com"


def test_push_apply_eligible_skips_applied_set_dedup(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/acme/jobs/applied",
             company="Acme Inc", title="Chief of Staff", apply_status="applied")
    _add_job(sq, "https://boards.greenhouse.io/other/jobs/1",
             company="Acme Inc", title="Chief of Staff")

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(
            sqlite_conn=sq, pg_conn=pg, score_floor=7, approved_batch="batch-A"
        )
        assert n == 0
        with pg.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM apply_queue")
            assert cur.fetchone()["n"] == 0
            cur.execute("SELECT COUNT(*) AS n FROM applied_set")
            assert cur.fetchone()["n"] == 1


def test_push_apply_eligible_retires_existing_applied_set_duplicates(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/acme/jobs/applied",
             company="Acme Inc", title="Chief of Staff", apply_status="applied")
    _add_job(sq, "https://boards.greenhouse.io/other/jobs/1",
             company="Acme Inc", title="Chief of Staff")

    from applypilot.fleet import dedup
    dk = dedup.dedup_key("Acme Inc", "Chief of Staff")
    with pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO apply_queue "
                "(url, company, title, application_url, score, status, lane, approved_batch, dedup_key, apply_domain) "
                "VALUES ('https://boards.greenhouse.io/other/jobs/1', 'Acme Inc', 'Chief of Staff', "
                "'https://boards.greenhouse.io/other/jobs/1', 9, 'queued', 'ats', 'old-batch', %s, "
                "'boards.greenhouse.io')",
                (dk,),
            )
        pg.commit()

        assert sync.push_apply_eligible(
            sqlite_conn=sq, pg_conn=pg, score_floor=7, approved_batch="batch-A"
        ) == 0
        with pg.cursor() as cur:
            cur.execute(
                "SELECT status, apply_status, apply_error, approved_batch "
                "FROM apply_queue WHERE url='https://boards.greenhouse.io/other/jobs/1'"
            )
            row = cur.fetchone()

    assert row["status"] == "failed"
    assert row["apply_status"] == "skipped"
    assert row["apply_error"] == "dedup:already_applied"
    assert row["approved_batch"] == "old-batch"


def test_push_apply_eligible_can_opt_into_research_scores(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/research/jobs/1",
             company="Research Co", title="Strategy Lead",
             audit_score=None, fit_score=None, research_fit_score=8.0)

    with pgqueue.connect(fleet_db) as pg:
        assert sync.push_apply_eligible(
            sqlite_conn=sq, pg_conn=pg, score_floor=7, approved_batch="batch-A"
        ) == 0
        assert sync.push_apply_eligible(
            sqlite_conn=sq, pg_conn=pg, score_floor=7, approved_batch="batch-A",
            include_research=True,
        ) == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url, score FROM apply_queue")
            rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["url"].endswith("/research/jobs/1")
    assert float(rows[0]["score"]) == 8.0


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


def test_push_apply_eligible_filters_off_lane_by_default(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/acme/jobs/1",
             company="Acme Inc", title="Chief of Staff")
    _add_job(sq, "https://boards.greenhouse.io/sales/jobs/2",
             company="Sales Co", title="Enterprise Account Executive")

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg,
                                     score_floor=7, approved_batch="batch-A", limit=None)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM apply_queue")
            urls = {r["url"] for r in cur.fetchall()}

    assert urls == {"https://boards.greenhouse.io/acme/jobs/1"}


def test_push_apply_eligible_keeps_human_decision_off_lane_rows(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/sales/jobs/1",
             company="Sales Co", title="Enterprise Account Executive",
             decision_source="human_review")
    _add_job(sq, "https://boards.greenhouse.io/sales/jobs/2",
             company="Sales Co", title="Enterprise Account Executive",
             decision_source="human_review", fit_gap_category="wrong_role_lane",
             recommended_action="ignore")

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg,
                                     score_floor=7, approved_batch="batch-A", limit=None)
        assert n == 2
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM apply_queue")
            urls = {r["url"] for r in cur.fetchall()}

    assert urls == {
        "https://boards.greenhouse.io/sales/jobs/1",
        "https://boards.greenhouse.io/sales/jobs/2",
    }


def test_push_apply_eligible_keeps_audit_flag_positive_override(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/acme/jobs/1",
             company="Acme Inc", title="Enterprise Account Executive to CRO",
             audit_flags='["chief_of_staff"]')

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg,
                                     score_floor=7, approved_batch="batch-A", limit=None)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM apply_queue")
            urls = {r["url"] for r in cur.fetchall()}

    assert urls == {"https://boards.greenhouse.io/acme/jobs/1"}


def test_push_apply_eligible_can_disable_lane_filter(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/sales/jobs/1",
             company="Sales Co", title="Enterprise Account Executive")

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg,
                                     score_floor=7, approved_batch="batch-A",
                                     limit=None, lane_filter=False)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM apply_queue")
            urls = {r["url"] for r in cur.fetchall()}

    assert urls == {"https://boards.greenhouse.io/sales/jobs/1"}


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


def test_push_linkedin_eligible_carries_resolver_metadata(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(
        sq,
        "https://www.linkedin.com/jobs/view/metadata",
        company="Acme",
        title="Chief of Staff",
        audit_score=9.0,
        linkedin_unresolved_kind="apply_button_missing",
        linkedin_next_action="run_ats_reconstruction",
    )

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg,
                                        score_floor=7, approved_batch="batch-L", limit=None)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("""
                SELECT linkedin_unresolved_kind, linkedin_next_action
                  FROM linkedin_queue
                 WHERE url = 'https://www.linkedin.com/jobs/view/metadata'
            """)
            row = cur.fetchone()

    assert row["linkedin_unresolved_kind"] == "apply_button_missing"
    assert row["linkedin_next_action"] == "run_ats_reconstruction"


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


def test_push_compute_eligible_default_floor_excludes_null_score_rows(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://co/jobs/scored", company="Co", title="Analyst", audit_score=8.0)
    _add_job(sq, "https://co/jobs/unscored", company="Co", title="PM",
             audit_score=None, fit_score=None, full_description="real JD")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_compute_eligible(sqlite_conn=sq, pg_conn=pg, task="score", score_floor=7)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM compute_queue ORDER BY url")
            rows = cur.fetchall()

    assert [r["url"] for r in rows] == ["https://co/jobs/scored"]


def test_push_compute_eligible_unscored_only_selects_described_nondup_research_unscored(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://co/jobs/fresh", company="Co", title="Fresh",
             audit_score=None, fit_score=None, full_description="fresh JD",
             discovered_at="2026-07-04T09:00:00")
    _add_job(sq, "https://co/jobs/older", company="Co", title="Older",
             audit_score=None, fit_score=None, full_description="older JD",
             discovered_at="2026-07-03T09:00:00")
    _add_job(sq, "https://co/jobs/scored", company="Co", title="Scored",
             audit_score=8.0, fit_score=None, full_description="scored JD")
    _add_job(sq, "https://co/jobs/research", company="Co", title="Research",
             audit_score=None, fit_score=None, research_fit_score=8.5,
             full_description="already research scored")
    _add_job(sq, "https://co/jobs/blank", company="Co", title="Blank",
             audit_score=None, fit_score=None, full_description=" ")
    _add_job(sq, "https://co/jobs/dup", company="Co", title="Dup",
             audit_score=None, fit_score=None, full_description="dup JD",
             duplicate_of_url="https://co/jobs/fresh")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_compute_eligible(sqlite_conn=sq, pg_conn=pg, task="score", unscored_only=True)
        assert n == 2
        with pg.cursor() as cur:
            cur.execute("SELECT url, payload FROM compute_queue ORDER BY url")
            rows = cur.fetchall()

    assert [r["url"] for r in rows] == ["https://co/jobs/fresh", "https://co/jobs/older"]
    assert {r["payload"]["full_description"] for r in rows} == {"fresh JD", "older JD"}


def test_push_compute_eligible_unscored_only_respects_limit(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://co/jobs/first", company="Co", title="First",
             audit_score=None, fit_score=None, full_description="first JD",
             discovered_at="2026-07-04T10:00:00")
    _add_job(sq, "https://co/jobs/second", company="Co", title="Second",
             audit_score=None, fit_score=None, full_description="second JD",
             discovered_at="2026-07-03T10:00:00")
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_compute_eligible(sqlite_conn=sq, pg_conn=pg, task="score",
                                       unscored_only=True, limit=1)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM compute_queue")
            rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0]["url"] == "https://co/jobs/first"


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


def test_reopen_compute_results_recovers_stranded_advisory_scores(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    url = "https://co/jobs/restore"
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
        sq.execute(
            "UPDATE jobs SET research_fit_score=NULL, research_decision=NULL WHERE url=?",
            (url,),
        )
        sq.commit()
        assert sync.pull_compute_results(sqlite_conn=sq, pg_conn=pg) == 0

        assert sync.reopen_compute_results(pg_conn=pg) == 1
        assert sync.reopen_compute_results(pg_conn=pg) == 0
        assert sync.pull_compute_results(sqlite_conn=sq, pg_conn=pg) == 1
        row = sq.execute("SELECT research_fit_score, research_decision, fit_score, audit_score "
                         "FROM jobs WHERE url=?", (url,)).fetchone()
        assert row["research_fit_score"] == 9.5
        assert row["research_decision"] == "strong_qualified"
        assert row["fit_score"] == 6
        assert row["audit_score"] == 8.0


def test_pull_compute_results_marks_each_task_independently(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    url = "https://co/jobs/multi-compute"
    _add_job(sq, url, company="Co", title="Analyst", audit_score=8.0, fit_score=6)
    with pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO compute_queue (url, task, status, result) VALUES "
                "(%s, 'score', 'done', %s), (%s, 'audit', 'done', %s)",
                (
                    url,
                    '{"research_fit_score": 9.5}',
                    url,
                    '{"research_decision": "qualified"}',
                ),
            )
        pg.commit()

        assert sync.pull_compute_results(sqlite_conn=sq, pg_conn=pg, batch=1) == 1
        assert sync.pull_compute_results(sqlite_conn=sq, pg_conn=pg, batch=1) == 1
        assert sync.pull_compute_results(sqlite_conn=sq, pg_conn=pg, batch=1) == 0

        row = sq.execute("SELECT research_fit_score, research_decision FROM jobs WHERE url=?", (url,)).fetchone()
        assert row["research_fit_score"] == 9.5
        assert row["research_decision"] == "qualified"


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
