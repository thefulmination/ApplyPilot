import json
from contextlib import contextmanager
from datetime import datetime, timezone

from typer.testing import CliRunner

from applypilot import canonical_decisions, database
from applypilot.apply import pgqueue
from applypilot.cli import _postgres_policy_transition, app


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


def test_canonical_status_reads_only_unified_postgres(monkeypatch):
    class ReadOnlyConnection:
        def rollback(self):
            pass

    sentinel = ReadOnlyConnection()

    @contextmanager
    def connect(dsn):
        assert dsn == "postgresql://brain"
        yield sentinel

    monkeypatch.setattr(pgqueue, "connect", connect)
    monkeypatch.setattr(
        "applypilot.brain.lifecycle.authority_status",
        lambda conn: {
            "authority": "postgres_staging_candidate",
            "cutover_proven": False,
            "lanes": {"ats": {}, "linkedin": {}},
        },
    )
    monkeypatch.setattr(
        "applypilot.cli._sqlite_cache_connection",
        lambda: (_ for _ in ()).throw(AssertionError("SQLite must not be opened")),
    )

    status = runner.invoke(
        app, ["canonical", "status", "--dsn", "postgresql://brain"]
    )

    assert status.exit_code == 0, status.stdout
    assert '"authority": "postgres_staging_candidate"' in status.stdout


def test_canonical_validate_and_promote_require_explicit_postgres_steps(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "applypilot.cli._postgres_policy_transition",
        lambda dsn, policy, lifecycle, lane=None: calls.append(
            (dsn, policy, lifecycle, lane)
        ) or {"lane": lane or "ats"},
    )
    validated = runner.invoke(
        app,
        ["canonical", "validate", "policy-ats-v2", "--dsn", "postgresql://brain"],
    )
    assert validated.exit_code == 0, validated.stdout

    missing_lane = runner.invoke(
        app,
        [
            "canonical", "promote", "policy-ats-v2", "--to", "canary",
            "--dsn", "postgresql://brain",
        ],
    )
    assert missing_lane.exit_code != 0
    missing_target = runner.invoke(
        app,
        [
            "canonical", "promote", "policy-ats-v2", "--lane", "ats",
            "--dsn", "postgresql://brain",
        ],
    )
    assert missing_target.exit_code != 0
    promoted = runner.invoke(
        app,
        [
            "canonical", "promote", "policy-ats-v2", "--lane", "ats",
            "--to", "canary", "--dsn", "postgresql://brain",
        ],
    )
    assert promoted.exit_code == 0, promoted.stdout
    retired = runner.invoke(
        app,
        ["canonical", "retire", "policy-ats-v2", "--dsn", "postgresql://brain"],
    )
    assert retired.exit_code == 0, retired.stdout
    assert calls == [
        ("postgresql://brain", "policy-ats-v2", "validated", None),
        ("postgresql://brain", "policy-ats-v2", "canary", "ats"),
        ("postgresql://brain", "policy-ats-v2", "retired", None),
    ]


def test_postgres_policy_transition_never_opens_sqlite(monkeypatch):
    sentinel = object()

    @contextmanager
    def connect(dsn):
        assert dsn == "postgresql://brain"
        yield sentinel

    monkeypatch.setattr(pgqueue, "connect", connect)
    monkeypatch.setattr(
        "applypilot.brain.lifecycle.transition_policy",
        lambda conn, policy, lifecycle, expected_lane=None: {
            "policy_version": policy,
            "lane": expected_lane,
            "lifecycle": lifecycle,
        },
    )
    monkeypatch.setattr(
        "applypilot.cli._sqlite_cache_connection",
        lambda: (_ for _ in ()).throw(AssertionError("SQLite must not be opened")),
    )

    result = _postgres_policy_transition(
        "postgresql://brain", "ats-v7", "canary", lane="ats"
    )

    assert result == {
        "policy_version": "ats-v7",
        "lane": "ats",
        "lifecycle": "canary",
    }


def test_canonical_canary_commands_are_lane_scoped_and_bounded(monkeypatch):
    sentinel = object()
    calls = []

    @contextmanager
    def connect(dsn):
        assert dsn == "postgresql://brain"
        yield sentinel

    monkeypatch.setattr(pgqueue, "connect", connect)
    monkeypatch.setattr(
        "applypilot.brain.lifecycle.arm_canary",
        lambda conn, policy, lane, capacity, expected_ats_pause_source=None: calls.append(
            ("arm", policy, lane, capacity, expected_ats_pause_source)
        ) or {
            "worker_id": "ats-0",
            "pinned_worker_version": "release-v1",
            "expected_worker_version": "release-canary-v2",
            "candidate_url": "https://example.test/job",
        },
    )
    monkeypatch.setattr(
        "applypilot.brain.lifecycle.stop_canary",
        lambda conn, lane: calls.append(("stop", lane)),
    )

    armed = runner.invoke(
        app,
        [
            "canonical", "canary-arm", "ats-v7", "--lane", "ats",
            "--capacity", "20", "--dsn", "postgresql://brain",
            "--expected-ats-pause-source", "operator_hold",
        ],
    )
    stopped = runner.invoke(
        app,
        ["canonical", "canary-stop", "--lane", "ats", "--dsn", "postgresql://brain"],
    )
    invalid = runner.invoke(
        app,
        [
            "canonical", "canary-arm", "ats-v7", "--lane", "ats",
            "--capacity", "0", "--dsn", "postgresql://brain",
        ],
    )

    assert armed.exit_code == 0, armed.stdout
    assert "version=release-canary-v2" in armed.stdout
    assert stopped.exit_code == 0, stopped.stdout
    assert invalid.exit_code != 0
    assert calls == [
        ("arm", "ats-v7", "ats", 20, "operator_hold"),
        ("stop", "ats"),
    ]


def test_canonical_canary_arm_can_authorize_sql_null_pause_source(monkeypatch):
    seen = []

    @contextmanager
    def connect(_dsn):
        yield object()

    monkeypatch.setattr(pgqueue, "connect", connect)
    monkeypatch.setattr(
        "applypilot.brain.lifecycle.arm_canary",
        lambda _conn, _policy, _lane, _capacity, expected_ats_pause_source: seen.append(
            expected_ats_pause_source
        )
        or {
            "worker_id": "ats-0",
            "pinned_worker_version": "release-v1",
            "expected_worker_version": "release-v1",
            "candidate_url": "https://example.test/job",
        },
    )

    result = runner.invoke(
        app,
        [
            "canonical",
            "canary-arm",
            "ats-v7",
            "--lane",
            "ats",
            "--capacity",
            "1",
            "--expect-null-ats-pause-source",
            "--dsn",
            "postgresql://brain",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert seen == [None]


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
    assert runner.invoke(
        app, ["canonical", "cache-backfill", str(artifacts)]
    ).exit_code == 0
    reviewed = runner.invoke(
        app,
        ["canonical", "cache-outcome-review", "mail", "--resolution", "trusted"],
    )
    assert reviewed.exit_code == 0, reviewed.stdout
    assert conn.execute(
        "SELECT resolution FROM email_event_reviews WHERE message_id='mail'"
    ).fetchone()[0] == "trusted"


def test_legacy_authoritative_names_no_longer_expose_sqlite_writers():
    for command in ("backfill", "outcome-review", "outcome-review-queue"):
        result = runner.invoke(app, ["canonical", command])
        assert result.exit_code == 2
