import json
import sqlite3
from applypilot.apply import pgqueue
from applypilot.fleet import compute_home_main as chm

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
  audit_score REAL, fit_score INTEGER, full_description TEXT, duplicate_of_url TEXT,
  research_fit_score REAL, research_decision TEXT, discovered_at TEXT);"""


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


def test_push_backlog_can_include_unscored_described_rows(fleet_db, tmp_path):
    sq = sqlite3.connect(str(tmp_path / "b.db")); sq.row_factory = sqlite3.Row
    sq.executescript(_DDL)
    sq.execute(
        "INSERT INTO jobs (url, company, title, application_url, audit_score, fit_score, "
        "full_description, discovered_at) VALUES "
        "('u1','Acme','COS','https://x',NULL,NULL,'the full JD','2026-07-04T10:00:00')"
    )
    sq.commit()
    with pgqueue.connect(fleet_db) as pg:
        n = chm.push_backlog(sqlite_conn=sq, pg_conn=pg, task="score", unscored_only=True, limit=1)
        assert n == 1
        with pg.cursor() as cur:
            cur.execute("SELECT payload FROM compute_queue WHERE url='u1'")
            assert cur.fetchone()["payload"]["full_description"] == "the full JD"


def test_push_backlog_can_add_audit_task_after_score_task_exists(fleet_db, tmp_path):
    sq = sqlite3.connect(str(tmp_path / "b.db")); sq.row_factory = sqlite3.Row
    sq.executescript(_DDL)
    sq.execute("INSERT INTO jobs (url, company, title, application_url, audit_score, full_description) "
               "VALUES ('u1','Acme','COS','https://x',8.0,'the full JD')")
    sq.commit()
    with pgqueue.connect(fleet_db) as pg:
        assert chm.push_backlog(sqlite_conn=sq, pg_conn=pg, task="score", score_floor=7) == 1
        assert chm.push_backlog(sqlite_conn=sq, pg_conn=pg, task="audit", score_floor=7) == 1
        with pg.cursor() as cur:
            cur.execute("SELECT task, payload FROM compute_queue WHERE url='u1' ORDER BY task")
            rows = cur.fetchall()

    assert [r["task"] for r in rows] == ["audit", "score"]
    assert {r["payload"]["full_description"] for r in rows} == {"the full JD"}


def test_main_push_threads_unscored_only_and_limit(monkeypatch, capsys):
    called: dict[str, object] = {}

    def fake_push_backlog(**kwargs):
        called.update(kwargs)
        return 7

    monkeypatch.setattr(chm, "push_backlog", fake_push_backlog)

    assert chm.main(["push", "--unscored-only", "--limit", "25"]) == 0

    assert called["unscored_only"] is True
    assert called["limit"] == 25
    assert capsys.readouterr().out == "pushed 7\n"


def test_reopen_results_exposes_compute_reopen_command(fleet_db):
    with pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO compute_queue (url, task, payload, status, result, synced_to_home_at) "
                "VALUES (%s,'score',%s,'done',%s,now())",
                ("u1", json.dumps({"url": "u1"}), json.dumps({"research_fit_score": 8.0})),
            )
        pg.commit()

        assert chm.reopen_results(pg_conn=pg) == 1
        assert chm.reopen_results(pg_conn=pg) == 0


def test_main_reopen_prints_reopened_count(monkeypatch, capsys):
    monkeypatch.setattr(chm, "reopen_results", lambda: 3)

    assert chm.main(["reopen"]) == 0

    assert capsys.readouterr().out == "reopened 3\n"
