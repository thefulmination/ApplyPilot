import sqlite3
from applypilot.apply import pgqueue
from applypilot.fleet import compute_home_main as chm

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
  audit_score REAL, fit_score INTEGER, full_description TEXT, duplicate_of_url TEXT,
  research_fit_score REAL, research_decision TEXT);"""


def test_push_backlog_includes_full_description(fleet_db, tmp_path):
    sq = sqlite3.connect(str(tmp_path / "b.db")); sq.row_factory = sqlite3.Row
    sq.executescript(_DDL)
    sq.execute("INSERT INTO jobs (url, company, title, application_url, audit_score, full_description) "
               "VALUES ('u1','Acme','COS','https://x',8.0,'the full JD')"); sq.commit()
    with pgqueue.connect(fleet_db) as pg:
        n = chm.push_backlog(sqlite_conn=sq, pg_conn=pg, task="score", score_floor=7, limit=None)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT payload FROM compute_queue WHERE url='u1'")
            assert cur.fetchone()["payload"]["full_description"] == "the full JD"
