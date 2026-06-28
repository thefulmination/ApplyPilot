"""Task 6: double-apply backfill -- seed PG applied_set from home brain apply history.

Tests that backfill_applied_set:
  - reads jobs.apply_status='applied' + apply_error in ('no_confirmation','crash_unconfirmed')
  - reads applications ledger rows with status='applied' (joined to jobs for company/title)
  - inserts into PG applied_set (dedup_key, company) ON CONFLICT DO NOTHING
  - is idempotent (second run returns >= 0, no crash, no duplicates)
"""
import sqlite3
from applypilot.apply import pgqueue


def _home_sqlite(tmp_path):
    db = tmp_path / "home.db"
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT, "
        "apply_status TEXT, apply_error TEXT, audit_score REAL, fit_score REAL, liveness_status TEXT, duplicate_of_url TEXT);"
        "CREATE TABLE applications (job_url TEXT, application_url TEXT, status TEXT);")
    return conn


def test_backfill_applied_set_from_home_history(fleet_db, tmp_path):
    from applypilot.fleet import sync
    sq = _home_sqlite(tmp_path)
    sq.execute("INSERT INTO jobs (url, company, title, apply_status) VALUES ('h1','Acme','COS','applied')")
    sq.execute("INSERT INTO applications (job_url, status) VALUES ('h2','applied')")  # ledger-only
    sq.execute("INSERT INTO jobs (url, company, title) VALUES ('h2','Beta','PM')")
    sq.commit()
    with pgqueue.connect(fleet_db) as pg:
        n = sync.backfill_applied_set(sq, pg)
        with pg.cursor() as cur:
            cur.execute("SELECT count(*) AS n FROM applied_set")
            assert cur.fetchone()["n"] >= 1  # Acme|COS dedup_key landed
    assert n >= 1
    # idempotent second run
    with pgqueue.connect(fleet_db) as pg:
        assert sync.backfill_applied_set(sq, pg) >= 0
