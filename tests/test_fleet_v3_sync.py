"""PG-backed tests for the v3 home-brain <-> coordination-Postgres bridge (fleet.sync).

Mirrors tests/test_fleet_pgqueue.py's fleet_sync tests, on the v3 queues:
  - push_apply_eligible pushes ONLY eligible rows, with a dedup_key + the approved_batch.
  - pull_apply_results maps a confirmed apply to the brain and is idempotent on re-pull.
  - push/pull_compute_eligible write compute results back as ADVISORY (research_*) only.

Uses the shared ``fleet_db`` fixture (disposable local Postgres, v3 schema applied).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

psycopg = pytest.importorskip("psycopg")

from applypilot.apply import pgqueue  # noqa: E402
from applypilot.fleet import queue as fleet_queue  # noqa: E402
from applypilot.fleet import sync  # noqa: E402


@pytest.fixture(autouse=True)
def _enable_test_adapters(monkeypatch):
    monkeypatch.setenv("APPLYPILOT_GREENHOUSE_ADAPTER", "1")
    monkeypatch.setenv("APPLYPILOT_LEVER_BOUNDED_PATH", "1")
    monkeypatch.setenv("APPLYPILOT_ASHBY_ADAPTER", "1")


@pytest.fixture(autouse=True)
def _register_canonical_pg_policies(request):
    if "fleet_db" not in request.fixturenames:
        yield
        return
    dsn = request.getfixturevalue("fleet_db")
    with pgqueue.connect(dsn) as conn, conn.cursor() as cur:
        for lane in ("ats", "linkedin"):
            policy = f"canonical-{lane}-active-test"
            cur.execute(
                "INSERT INTO fleet_decision_policies (policy_version,lane,status) "
                "VALUES (%s,%s,'active')",
                (policy, lane),
            )
            cur.execute(f"UPDATE fleet_config SET {lane}_policy_version=%s WHERE id=1", (policy,))
        conn.commit()
    yield


# ---------------------------------------------------------------------------
# Temp SQLite brain (minimal jobs table) -- mirrors the fleet_sync test DDL,
# plus the advisory research_* columns the compute pull writes.
# ---------------------------------------------------------------------------
_JOBS_DDL = """
CREATE TABLE jobs (
    url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
    audit_score REAL, fit_score INTEGER, full_description TEXT, liveness_status TEXT,
    last_verified_live TEXT, liveness_reason TEXT,
    apply_status TEXT, apply_error TEXT, duplicate_of_url TEXT,
    applied_at TEXT, agent_id TEXT, verification_confidence TEXT,
    apply_duration_ms INTEGER, apply_attempts INTEGER DEFAULT 0,
    research_fit_score REAL, research_decision TEXT,
    discovered_at TEXT, decision_source TEXT, fit_gap_category TEXT,
    recommended_action TEXT, audit_flags TEXT,
    linkedin_resolve_status TEXT, linkedin_resolved_at TEXT,
    linkedin_resolve_error TEXT,
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


def _add_job(conn, url, **kw):
    canonical = kw.pop("canonical", True)
    action = kw.pop("canonical_action", None)
    verdict = kw.pop("canonical_verdict", "qualified")
    policy_status = kw.pop("canonical_policy_status", "active")
    policy_lane = kw.pop("canonical_policy_lane", None)
    expires_at = kw.pop("canonical_expires_at", None)
    cols = {"url": url, "application_url": url, "audit_score": 8.0,
            "liveness_status": "live", "full_description": "x" * 600}
    cols.update(kw)
    conn.execute(f"INSERT INTO jobs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 list(cols.values()))
    if canonical:
        lane = policy_lane or ("linkedin" if "linkedin.com" in (cols.get("application_url") or url) else "ats")
        policy = f"canonical-{lane}-{policy_status}-test"
        now = datetime.now(timezone.utc)
        score = float(cols.get("audit_score") or cols.get("fit_score") or cols.get("research_fit_score") or 8.0)
        decision_action = action or ("apply" if score >= 7 else "review")
        decision_id = f"decision-{abs(hash(url))}"
        conn.execute(
            "INSERT OR IGNORE INTO decision_policy_versions VALUES (?,?,?,?,?)",
            (policy, lane, policy_status, '{"qualificationFloor":7}', now.isoformat()),
        )
        conn.execute(
            "INSERT INTO job_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (decision_id, url, policy, lane, 9.0, 8.0, 8.0, score, verdict,
             decision_action, 0.9, f"hash-{decision_id}", now.isoformat(),
             expires_at or (now + timedelta(days=1)).isoformat()),
        )
        conn.execute("UPDATE jobs SET canonical_decision_id=? WHERE url=?", (decision_id, url))
    conn.commit()


# ---------------------------------------------------------------------------
# APPLY push
# ---------------------------------------------------------------------------

def test_push_apply_eligible_filters_and_stamps(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/acme/jobs/1",
             company="Acme Inc", title="Chief of Staff", liveness_reason="gh_api_200",
             last_verified_live="2026-07-12T12:00:00+00:00")                   # eligible
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
    _add_job(sq, "https://boards.greenhouse.io/missing-company/jobs/8",
             company="", title="Analyst", audit_score=9.0)                    # malformed -> skip
    _add_job(sq, "https://boards.greenhouse.io/missing-title/jobs/9",
             company="Valid Co", title="", audit_score=9.0)                   # malformed -> skip

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg,
                                     score_floor=7, approved_batch="batch-A", limit=None)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url, target_host, apply_domain, dedup_key, approved_batch, "
                        "lane, status, liveness_required, eligibility_required, "
                        "eligibility_status, eligibility_reason FROM apply_queue")
            rows = cur.fetchall()

    assert len(rows) == 1
    row = rows[0]
    assert row["url"].endswith("/acme/jobs/1")
    assert row["target_host"] == "boards.greenhouse.io"
    assert row["apply_domain"] == "boards.greenhouse.io"
    assert row["approved_batch"] == "batch-A"
    assert row["lane"] == "ats"
    assert row["status"] == "queued"
    assert row["liveness_required"] is True
    assert row["eligibility_required"] is True
    assert row["eligibility_status"] == "eligible"
    assert row["eligibility_reason"] == "no_deterministic_exclusion"
    # dedup_key is the board-agnostic (company, role) hash -- present + correct.
    assert row["dedup_key"]
    from applypilot.fleet import dedup
    assert row["dedup_key"] == dedup.dedup_key("Acme Inc", "Chief of Staff")


def test_push_apply_eligible_updates_liveness_only_from_newer_evidence(fleet_db, tmp_path):
    url = "https://boards.greenhouse.io/acme/jobs/123"
    sq = _home_sqlite(tmp_path)
    _add_job(
        sq,
        url,
        company="Acme Inc",
        title="Analyst",
        liveness_status="uncertain",
        liveness_reason="thin_body",
        last_verified_live="2026-07-10T12:00:00+00:00",
    )

    with pgqueue.connect(fleet_db) as pg:
        assert sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, approved_batch="batch-A") == 1
        with pg.cursor() as cur:
            cur.execute(
                "UPDATE apply_queue SET liveness_status='live', liveness_reason='gh_api_200', "
                "liveness_checked_at='2026-07-12T12:00:00+00:00' WHERE url=%s",
                (url,),
            )
        pg.commit()

        assert sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, approved_batch="batch-A") == 1
        with pg.cursor() as cur:
            cur.execute(
                "SELECT liveness_status, liveness_reason, liveness_checked_at "
                "FROM apply_queue WHERE url=%s",
                (url,),
            )
            row = cur.fetchone()
        assert row["liveness_status"] == "live"
        assert row["liveness_reason"] == "gh_api_200"
        assert row["liveness_checked_at"].astimezone(timezone.utc).isoformat() == "2026-07-12T12:00:00+00:00"

        sq.execute(
            "UPDATE jobs SET liveness_status='uncertain', liveness_reason='redirect_login', "
            "last_verified_live='2026-07-13T12:00:00+00:00' WHERE url=?",
            (url,),
        )
        sq.commit()
        assert sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, approved_batch="batch-A") == 1
        with pg.cursor() as cur:
            cur.execute(
                "SELECT liveness_status, liveness_reason, liveness_checked_at "
                "FROM apply_queue WHERE url=%s",
                (url,),
            )
            row = cur.fetchone()
        assert row["liveness_status"] == "uncertain"
        assert row["liveness_reason"] == "redirect_login"
        assert row["liveness_checked_at"].astimezone(timezone.utc).isoformat() == "2026-07-13T12:00:00+00:00"


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


def test_push_apply_eligible_retires_queued_rows_marked_dead_in_home_db(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/alive/jobs/1", company="Acme", title="Chief of Staff")
    _add_job(
        sq,
        "https://boards.greenhouse.io/dead/jobs/1",
        company="Dead Co",
        title="Former PM",
        application_url="https://boards.greenhouse.io/dead/jobs/apply/1",
        liveness_status="dead",
    )
    with pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO apply_queue (url, company, title, application_url, score, lane, status, approved_batch)
                VALUES
                  ('https://boards.greenhouse.io/dead/jobs/1', 'Dead Co', 'Former PM',
                   'https://boards.greenhouse.io/dead/jobs/apply/1', 9.0, 'ats', 'queued', 'legacy'),
                  ('https://boards.greenhouse.io/dead/jobs/mapped-by-appurl', 'Dead Co', 'Former PM',
                   'https://boards.greenhouse.io/dead/jobs/apply/1', 9.0, 'ats', 'queued', 'legacy'),
                  ('https://boards.greenhouse.io/other/jobs/1', 'Other Co', 'Other', 'https://boards.greenhouse.io/other/jobs/apply/1', 9.0, 'ats', 'queued', 'legacy')
                """
            )
        pg.commit()
        sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, approved_batch="batch-A")
        with pg.cursor() as cur:
            cur.execute(
                "SELECT url, status, apply_status, apply_error FROM apply_queue "
                "WHERE url IN ("
                " 'https://boards.greenhouse.io/alive/jobs/1',"
                " 'https://boards.greenhouse.io/dead/jobs/1',"
                " 'https://boards.greenhouse.io/dead/jobs/mapped-by-appurl',"
                " 'https://boards.greenhouse.io/other/jobs/1'"
                ") ORDER BY url"
            )
            rows = {r["url"]: r for r in cur.fetchall()}

    assert rows["https://boards.greenhouse.io/dead/jobs/1"]["status"] == "failed"
    assert rows["https://boards.greenhouse.io/dead/jobs/1"]["apply_status"] == "failed"
    assert rows["https://boards.greenhouse.io/dead/jobs/1"]["apply_error"] == "expired"
    assert rows["https://boards.greenhouse.io/dead/jobs/mapped-by-appurl"]["status"] == "failed"
    assert rows["https://boards.greenhouse.io/dead/jobs/mapped-by-appurl"]["apply_status"] == "failed"
    assert rows["https://boards.greenhouse.io/dead/jobs/mapped-by-appurl"]["apply_error"] == "expired"

    assert rows["https://boards.greenhouse.io/other/jobs/1"]["status"] == "queued"


def test_push_apply_eligible_rejects_score_only_rows(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/research/jobs/1",
             company="Research Co", title="Strategy Lead",
             audit_score=None, fit_score=None, research_fit_score=8.0, canonical=False)

    with pgqueue.connect(fleet_db) as pg:
        assert sync.push_apply_eligible(
            sqlite_conn=sq, pg_conn=pg, score_floor=7, approved_batch="batch-A"
        ) == 0
        assert sync.push_apply_eligible(
            sqlite_conn=sq, pg_conn=pg, score_floor=0, approved_batch="batch-A",
        ) == 0
        with pg.cursor() as cur:
            cur.execute("SELECT url, score FROM apply_queue")
            rows = cur.fetchall()

    assert rows == []


def test_push_apply_eligible_score_floor_cannot_override_canonical_authority(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/acme/jobs/58",
             company="Acme Inc", title="Chief of Staff", audit_score=5.8)
    _add_job(sq, "https://boards.greenhouse.io/acme/jobs/57",
             company="Acme Inc", title="BizOps Analyst", audit_score=5.7)

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(
            sqlite_conn=sq, pg_conn=pg, score_floor=5.8, approved_batch="batch-A", limit=None
        )
        assert n == 0
        with pg.cursor() as cur:
            cur.execute("SELECT url, score FROM apply_queue ORDER BY url")
            rows = cur.fetchall()

    assert rows == []


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


def test_push_apply_eligible_does_not_use_legacy_title_lane_filter(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/acme/jobs/1",
             company="Acme Inc", title="Chief of Staff")
    _add_job(sq, "https://boards.greenhouse.io/sales/jobs/2",
             company="Sales Co", title="Enterprise Account Executive")

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg,
                                     score_floor=7, approved_batch="batch-A", limit=None)
        assert n == 2
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM apply_queue")
            urls = {r["url"] for r in cur.fetchall()}

    assert urls == {
        "https://boards.greenhouse.io/acme/jobs/1",
        "https://boards.greenhouse.io/sales/jobs/2",
    }


@pytest.mark.parametrize(
    ("kwargs"),
    [
        {"canonical_action": "review"},
        {"canonical_action": "reject"},
        {"canonical_verdict": "unqualified"},
        {"canonical_verdict": "uncertain"},
        {"canonical_policy_status": "draft"},
        {"canonical_policy_status": "validated"},
        {"canonical_policy_status": "retired"},
        {"canonical_policy_lane": "linkedin"},
        {"canonical_expires_at": "2020-01-01T00:00:00+00:00"},
    ],
)
def test_push_apply_eligible_rejects_non_authoritative_canonical_rows(
    fleet_db, tmp_path, kwargs
):
    sq = _home_sqlite(tmp_path)
    _add_job(
        sq,
        "https://boards.greenhouse.io/acme/jobs/rejected",
        company="Acme",
        title="Chief of Staff",
        **kwargs,
    )
    with pgqueue.connect(fleet_db) as pg:
        assert sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg) == 0


def test_push_apply_selector_carries_complete_canonical_provenance(
    fleet_db, tmp_path, monkeypatch
):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://jobs.lever.co/acme/authority", company="Acme", title="COS")
    captured = []

    def capture(_pg, rows, **_kwargs):
        captured.extend(rows)
        return len(rows)

    monkeypatch.setattr(sync._queue, "push_apply_jobs", capture)
    with pgqueue.connect(fleet_db) as pg:
        assert sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg) == 1

    row = captured[0]
    required = {
        "decision_id", "policy_version", "decision_action", "qualification_verdict",
        "qualification_score", "qualification_floor", "preference_score", "outcome_score",
        "final_score", "decision_confidence", "decision_created_at",
        "decision_expires_at", "input_hash",
    }
    assert required <= row.keys()
    assert row["decision_action"] == "apply"
    assert row["qualification_verdict"] == "qualified"


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
             company="Acme", title="Chief of Staff", audit_score=9.0,
             linkedin_resolve_status="easy_apply", linkedin_resolved_at=datetime.now(timezone.utc).isoformat())
    _add_job(sq, "https://www.linkedin.com/jobs/view/openai",
             company="OpenAI", title="Strategy", audit_score=10.0,
             linkedin_resolve_status="easy_apply", linkedin_resolved_at=datetime.now(timezone.utc).isoformat())

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(sqlite_conn=sq, pg_conn=pg,
                                        score_floor=7, approved_batch="batch-L", limit=None,
                                        max_resolved_age_days=0)
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
        linkedin_resolve_status="easy_apply",
        linkedin_resolved_at=datetime.now(timezone.utc).isoformat(),
        linkedin_resolve_error="no_primary_apply_button",
        linkedin_unresolved_kind="apply_button_missing",
        linkedin_next_action="run_ats_reconstruction",
    )

    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_linkedin_eligible(
            sqlite_conn=sq,
            pg_conn=pg,
            score_floor=7,
            approved_batch="batch-L",
            limit=None,
        )
        assert n == 1
        with pg.cursor() as cur:
            cur.execute(
                """
                SELECT linkedin_resolve_error, linkedin_unresolved_kind, linkedin_next_action
                  FROM linkedin_queue
                 WHERE url = 'https://www.linkedin.com/jobs/view/metadata'
                """
            )
            row = cur.fetchone()

    assert row["linkedin_resolve_error"] == "no_primary_apply_button"
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


def test_push_apply_eligible_excludes_exhausted_attempts(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    _add_job(sq, "https://boards.greenhouse.io/ok/jobs/1", company="OK", title="COS")
    _add_job(
        sq,
        "https://boards.greenhouse.io/exhausted/jobs/2",
        company="Exhausted",
        title="Ops",
        audit_score=9.0,
        apply_status="failed",
        apply_error="expired",
        apply_attempts=99,
    )
    with pgqueue.connect(fleet_db) as pg:
        n = sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, score_floor=7, approved_batch="b1")
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT url FROM apply_queue")
            urls = {r["url"] for r in cur.fetchall()}
    assert any(u.endswith("/ok/jobs/1") for u in urls)
    assert not any("/exhausted/jobs/2" in u for u in urls)


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
        check = fleet_queue.claim_liveness_check(pg, "preflight-w1")
        assert check is not None and check["url"] == url
        assert fleet_queue.write_liveness_result(
            pg, "preflight-w1", url, status="live", reason="greenhouse_api_200"
        )
        job = fleet_queue.lease_apply(pg, "w1", home_ip="1.2.3.4")
        assert job is not None and job["url"] == url
        ok = fleet_queue.write_apply_result(
            pg, "w1", url, status="applied", target_host="boards.greenhouse.io",
            home_ip="1.2.3.4", apply_status="applied", est_cost_usd=0.6)
        assert ok is True
        assert pg.execute(
            "SELECT public.fleet_controller_verify_submission"
            "('ats',%s,'independent-receipt','email_receipt') AS ok",
            (url,),
        ).fetchone()["ok"] is True
        pg.commit()

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
        check = fleet_queue.claim_liveness_check(pg, "preflight-w1")
        assert check is not None and check["url"] == url
        assert fleet_queue.write_liveness_result(
            pg, "preflight-w1", url, status="live", reason="lever_api_200"
        )
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


def test_registered_tenant_stages_opaque_session_profile(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    url = "https://acme.wd5.myworkdayjobs.com/site/job/Role/JR-session"
    _add_job(sq, url, company="Acme", title="Role")
    from applypilot import tenants
    from applypilot import database
    from applypilot.apply import tenant_sessions

    database.ensure_tenant_tables(sq)
    tenants.set_tenant(sq, "acme.wd5.myworkdayjobs.com", "supervised")
    with pgqueue.connect(fleet_db) as pg:
        sync.push_apply_eligible(sqlite_conn=sq, pg_conn=pg, approved_batch="session-batch")
        with pg.cursor() as cur:
            cur.execute(
                "SELECT session_required, tenant_profile_id FROM apply_queue WHERE url=%s",
                (url,),
            )
            row = cur.fetchone()
    assert row["session_required"] is True
    assert row["tenant_profile_id"] == tenant_sessions.profile_id_for_host(
        "acme.wd5.myworkdayjobs.com"
    )


def test_pull_linkedin_results_marks_expired_jobs_dead(fleet_db, tmp_path):
    sq = _home_sqlite(tmp_path)
    url = "https://www.linkedin.com/jobs/view/expired"
    _add_job(sq, url, company="Acme", title="COS", liveness_status="live")
    with pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO linkedin_queue (url, application_url, score, status, apply_error) "
                "VALUES (%s,%s,%s,'failed','expired')",
                (url, url, 8.0),
            )
        pg.commit()

        assert sync.pull_linkedin_results(sqlite_conn=sq, pg_conn=pg).get("failed") == 1
        brain = sq.execute(
            "SELECT apply_status, apply_error, liveness_status, liveness_reason, last_verified_live "
            "FROM jobs WHERE url=?",
            (url,),
        ).fetchone()

    assert brain["apply_status"] == "failed"
    assert brain["apply_error"] == "expired"
    assert brain["liveness_status"] == "dead"
    assert brain["liveness_reason"] == "fleet_result_expired"
    assert brain["last_verified_live"] is not None


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
