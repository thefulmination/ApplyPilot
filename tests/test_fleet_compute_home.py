import json
import sqlite3
from applypilot.apply import pgqueue
from applypilot.fleet import compute_context as cc
from applypilot.fleet import compute_home_main as chm

_DDL = """CREATE TABLE jobs (url TEXT PRIMARY KEY, company TEXT, title TEXT, application_url TEXT,
  audit_score REAL, fit_score INTEGER, full_description TEXT, duplicate_of_url TEXT,
  research_fit_score REAL, research_decision TEXT, discovered_at TEXT);"""


def _publish_score_context(pg, version="ctx-test"):
    cc.publish_context(pg, resume_text="REAL RESUME", preference_profile={},
                       kg_prompt="", search_cfg={}, version=version)


def test_push_backlog_includes_full_description(fleet_db, tmp_path):
    sq = sqlite3.connect(str(tmp_path / "b.db")); sq.row_factory = sqlite3.Row
    sq.executescript(_DDL)
    sq.execute("INSERT INTO jobs (url, company, title, application_url, audit_score, full_description) "
               "VALUES ('u1','Acme','COS','https://x',8.0,'the full JD')"); sq.commit()
    with pgqueue.connect(fleet_db) as pg:
        _publish_score_context(pg)
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
        _publish_score_context(pg)
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
        _publish_score_context(pg)
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


def test_push_backlog_rejects_missing_score_context(fleet_db, tmp_path):
    sq = sqlite3.connect(str(tmp_path / "b.db")); sq.row_factory = sqlite3.Row
    sq.executescript(_DDL)
    sq.execute("INSERT INTO jobs (url, company, title, application_url, audit_score, full_description) "
               "VALUES ('u1','Acme','COS','https://x',8.0,'the full JD')")
    sq.commit()

    with pgqueue.connect(fleet_db) as pg:
        try:
            chm.push_backlog(sqlite_conn=sq, pg_conn=pg, task="score", score_floor=7)
        except RuntimeError as exc:
            assert "publish-context" in str(exc)
        else:
            raise AssertionError("score push must refuse to enqueue without ctx:resume")


def test_publish_context_from_app_dir_writes_versioned_compute_assets(fleet_db, tmp_path):
    app_dir = tmp_path / ".applypilot"
    app_dir.mkdir()
    (app_dir / "resume.txt").write_text("REAL RESUME", encoding="utf-8")
    (app_dir / "job_preference_profile.json").write_text('{"promptSummary":"prefs"}', encoding="utf-8")
    (app_dir / "job_knowledge_graph_prompt.md").write_text("KG PROMPT", encoding="utf-8")
    (app_dir / "searches.yaml").write_text("score_audit:\n  floor: 7\n", encoding="utf-8")

    with pgqueue.connect(fleet_db) as pg:
        version = chm.publish_context_from_app_dir(app_dir=app_dir, pg_conn=pg)
        ctx, loaded_version = cc.load_context(pg, providers=["deepseek"])

    assert version.startswith("ctx-")
    assert loaded_version == version
    assert ctx.resume_text == "REAL RESUME"
    assert ctx.preference_profile == {"promptSummary": "prefs"}
    assert ctx.kg_prompt == "KG PROMPT"
    assert ctx.search_cfg == {"score_audit": {"floor": 7}}


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


def test_requeue_results_snapshots_and_resets_nonmatching_context(fleet_db):
    with pgqueue.connect(fleet_db) as pg:
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO compute_queue (url, task, payload, status, result, est_cost_usd, synced_to_home_at) "
                "VALUES (%s,'score',%s,'done',%s,0.01,now())",
                ("u-old", json.dumps({"url": "u-old"}),
                 json.dumps({"research_fit_score": 2, "ctx_version": "ctx-old"})),
            )
            cur.execute(
                "INSERT INTO compute_queue (url, task, payload, status, result, est_cost_usd, synced_to_home_at) "
                "VALUES (%s,'score',%s,'done',%s,0.01,now())",
                ("u-new", json.dumps({"url": "u-new"}),
                 json.dumps({"research_fit_score": 8, "ctx_version": "ctx-new"})),
            )
        pg.commit()

        n = chm.requeue_results(pg_conn=pg, task="score", before_context_version="ctx-new",
                                snapshot=True, snapshot_name="compute_requeue_test_snapshot")

        with pg.cursor() as cur:
            cur.execute("SELECT status, result, est_cost_usd, synced_to_home_at FROM compute_queue WHERE url='u-old'")
            old = cur.fetchone()
            cur.execute("SELECT status, result FROM compute_queue WHERE url='u-new'")
            new = cur.fetchone()
            cur.execute("SELECT count(*) AS n FROM compute_requeue_test_snapshot")
            snap_count = cur.fetchone()["n"]

    assert n == 1
    assert old["status"] == "queued" and old["result"] is None
    assert float(old["est_cost_usd"]) == 0.0 and old["synced_to_home_at"] is None
    assert new["status"] == "done" and new["result"]["ctx_version"] == "ctx-new"
    assert snap_count == 1


def test_status_report_includes_context_workers_queue_and_bad_reasoning(fleet_db):
    with pgqueue.connect(fleet_db) as pg:
        _publish_score_context(pg, version="ctx-status")
        with pg.cursor() as cur:
            cur.execute(
                "INSERT INTO compute_queue (url, task, payload, status, result, est_cost_usd) "
                "VALUES (%s,'score',%s,'queued',NULL,0)",
                ("u-queued", json.dumps({"url": "u-queued"})),
            )
            cur.execute(
                "INSERT INTO compute_queue (url, task, payload, status, lease_owner, result, est_cost_usd) "
                "VALUES (%s,'score',%s,'leased','w1',NULL,0)",
                ("u-leased", json.dumps({"url": "u-leased"})),
            )
            cur.execute(
                "INSERT INTO compute_queue (url, task, payload, status, result, est_cost_usd, synced_to_home_at) "
                "VALUES (%s,'score',%s,'done',%s,0.01,NULL)",
                ("u-done", json.dumps({"url": "u-done"}),
                 json.dumps({"reasoning": "without a resume", "ctx_version": "ctx-status"})),
            )
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, machine_owner, role, state, last_beat) "
                "VALUES ('m4-score-0','m4','compute','computing',now())"
            )
        pg.commit()

        report = chm.status_report(pg_conn=pg, task="score")

    assert report["context"]["version"] == "ctx-status"
    assert report["context"]["resume_chars"] == len("REAL RESUME")
    assert report["queue"]["queued"]["count"] == 1
    assert report["queue"]["leased"]["count"] == 1
    assert report["queue"]["done"]["unsynced"] == 1
    assert report["active_workers"]["total"] == 1
    assert report["bad_reasoning_unsynced_done"] == 1
