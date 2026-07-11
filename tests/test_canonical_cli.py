import json
from datetime import datetime, timedelta, timezone

from typer.testing import CliRunner

from applypilot import canonical_decisions, database
from applypilot.apply import pgqueue
from applypilot.cli import app


runner = CliRunner()


def _metrics():
    names = (
        "zero-hard-negative-applies",
        "zero-title-only-promotions",
        "grounded-required-support",
        "canonical-outperforms-legacy",
    )
    return {
        "version": 4,
        "evaluatorVersion": "canonical-replay-evaluator-v1",
        "releaseGates": [
            {"name": name, "locked": True, "passed": True, "failureCount": 0}
            for name in names
        ],
        "releaseGate": {"locked": True, "passed": True, "failedGateNames": []},
    }


def _draft(conn, version="policy-ats-v2", lane="ats", *, metrics=True):
    canonical_decisions.create_draft_policy(conn, {
        "policy_version": version,
        "lane": lane,
        "status": "draft",
        "qualification_model": "canonical-v2",
        "preference_model": "pairwise-v1",
        "outcome_model": "reviewed-v1",
        "kg_version": "kg-v1",
        "label_snapshot": "labels-hash",
        "pairwise_snapshot": "pairwise-hash",
        "outcome_snapshot": "outcomes-hash",
        "config_json": json.dumps({"qualificationFloor": 7}),
        "metrics_json": json.dumps(_metrics()) if metrics else None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "validated_at": None,
        "activated_at": None,
        "retired_at": None,
    })


def _brain(tmp_path, monkeypatch):
    path = tmp_path / "brain.db"
    monkeypatch.setenv("APPLYPILOT_DB_PATH", str(path))
    monkeypatch.delenv("FLEET_PG_DSN", raising=False)
    return database.init_db(path)


def test_canonical_status_and_validate(tmp_path, monkeypatch):
    conn = _brain(tmp_path, monkeypatch)
    _draft(conn)
    status = runner.invoke(app, ["canonical", "status"])
    assert status.exit_code == 0
    assert "missing_projection" in status.stdout
    validated = runner.invoke(app, ["canonical", "validate", "policy-ats-v2"])
    assert validated.exit_code == 0
    assert conn.execute(
        "SELECT status FROM decision_policy_versions WHERE policy_version='policy-ats-v2'"
    ).fetchone()[0] == "validated"


def test_canonical_promote_requires_lane_and_replay_metrics(fleet_db, tmp_path, monkeypatch):
    conn = _brain(tmp_path, monkeypatch)
    _draft(conn, metrics=False)
    missing_lane = runner.invoke(
        app, ["canonical", "promote", "policy-ats-v2", "--dsn", fleet_db]
    )
    assert missing_lane.exit_code != 0
    failed = runner.invoke(
        app,
        ["canonical", "promote", "policy-ats-v2", "--lane", "ats", "--dsn", fleet_db],
    )
    assert failed.exit_code != 0


def test_canonical_promote_pg_failure_leaves_validated_policy(tmp_path, monkeypatch):
    conn = _brain(tmp_path, monkeypatch)
    _draft(conn)

    failed = runner.invoke(
        app,
        ["canonical", "promote", "policy-ats-v2", "--lane", "ats", "--dsn", "invalid"],
    )

    assert failed.exit_code == 2
    assert conn.execute(
        "SELECT status FROM decision_policy_versions WHERE policy_version='policy-ats-v2'"
    ).fetchone()[0] == "validated"


def test_canonical_retire_pg_failure_leaves_sqlite_active(tmp_path, monkeypatch):
    conn = _brain(tmp_path, monkeypatch)
    _draft(conn)
    canonical_decisions.validate_policy(conn, "policy-ats-v2")
    canonical_decisions.activate_policy(conn, "policy-ats-v2", lane="ats")

    failed = runner.invoke(
        app, ["canonical", "retire", "policy-ats-v2", "--dsn", "invalid"]
    )

    assert failed.exit_code == 2
    assert conn.execute(
        "SELECT status FROM decision_policy_versions WHERE policy_version='policy-ats-v2'"
    ).fetchone()[0] == "active"


def test_canonical_promote_and_retire_are_lane_scoped(fleet_db, tmp_path, monkeypatch):
    conn = _brain(tmp_path, monkeypatch)
    _draft(conn)
    promoted = runner.invoke(
        app,
        ["canonical", "promote", "policy-ats-v2", "--lane", "ats", "--dsn", fleet_db],
    )
    assert promoted.exit_code == 0, promoted.stdout
    assert conn.execute(
        "SELECT status FROM decision_policy_versions WHERE policy_version='policy-ats-v2'"
    ).fetchone()[0] == "active"
    now = datetime.now(timezone.utc)
    with pgqueue.connect(fleet_db) as pg, pg.cursor() as cur:
        cur.execute("SELECT ats_policy_version,ats_paused FROM fleet_config WHERE id=1")
        assert cur.fetchone()["ats_policy_version"] == "policy-ats-v2"
        cur.execute(
            "INSERT INTO apply_queue (url,application_url,score,status,lane,approved_batch,"
            "decision_id,policy_version,decision_action,qualification_verdict,qualification_score,"
            "qualification_floor,preference_score,outcome_score,final_score,decision_confidence,"
            "decision_created_at,decision_expires_at,input_hash) "
            "VALUES ('queued','https://example.com',9,'queued','ats','b1','d1','policy-ats-v2',"
            "'apply','qualified',9,7,8,8,9,.9,%s,%s,'hash')",
            (now, now + timedelta(days=1)),
        )
        pg.commit()

    retired = runner.invoke(
        app, ["canonical", "retire", "policy-ats-v2", "--dsn", fleet_db]
    )
    assert retired.exit_code == 0, retired.stdout
    with pgqueue.connect(fleet_db) as pg, pg.cursor() as cur:
        cur.execute("SELECT ats_policy_version,ats_paused FROM fleet_config WHERE id=1")
        cfg = cur.fetchone()
        assert cfg["ats_policy_version"] is None and cfg["ats_paused"] is True
        cur.execute("SELECT status,apply_error FROM apply_queue WHERE url='queued'")
        row = cur.fetchone()
        assert row["status"] == "failed" and row["apply_error"] == "canonical_policy_retired"


def test_canonical_backfill_and_outcome_review(tmp_path, monkeypatch):
    conn = _brain(tmp_path, monkeypatch)
    url = "https://example.com/job"
    conn.execute("INSERT INTO jobs (url,title) VALUES (?,?)", (url, "Role"))
    conn.execute(
        "INSERT INTO email_events (message_id,occurred_at,stage,scanned_at,job_url) "
        "VALUES ('mail','2026-01-01T00:00:00Z','screen','2026-01-01T00:00:00Z',?)",
        (url,),
    )
    conn.commit()
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    assert runner.invoke(app, ["canonical", "backfill", str(artifacts)]).exit_code == 0
    reviewed = runner.invoke(
        app,
        ["canonical", "outcome-review", "mail", "--resolution", "trusted"],
    )
    assert reviewed.exit_code == 0, reviewed.stdout
    assert conn.execute(
        "SELECT resolution FROM email_event_reviews WHERE message_id='mail'"
    ).fetchone()[0] == "trusted"
