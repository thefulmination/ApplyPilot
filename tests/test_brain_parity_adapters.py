from __future__ import annotations

import copy
import hashlib
import re
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import pytest

from applypilot.brain.importer import SOURCE_TABLES
from applypilot.brain.policy_artifacts import compile_policy_artifacts
from applypilot.brain.sqlite_to_postgres import SOURCE_NAMESPACE


JOB_URL = "https://jobs.example/role-1"
MESSAGE_ID = "gmail-message-1"
POLICY_VERSION = "ats-parity-v1"
CREATED = "2026-07-16T00:00:00Z"
KG_CONTENT = b'{"nodes":[]}'
KG_HASH = hashlib.sha256(KG_CONTENT).hexdigest()
SOURCE_FINGERPRINT = "f" * 64
LABEL_SNAPSHOT = "a" * 64
PAIRWISE_SNAPSHOT = "b" * 64
OUTCOME_SNAPSHOT = "c" * 64


def _utc(minute: int) -> datetime:
    return datetime(2026, 7, 16, 0, minute, tzinfo=timezone.utc)


def _job_id(url: str) -> str:
    return hashlib.sha256(f"{SOURCE_NAMESPACE}:job:{url}".encode()).hexdigest()


def _application_id(source_id: int) -> str:
    return hashlib.sha256(f"{SOURCE_NAMESPACE}:application:{source_id}".encode()).hexdigest()


class _RecordingCursor:
    def __init__(self, pg: _RecordingPostgres) -> None:
        self.pg = pg
        self.rows: list[dict[str, Any]] = []

    def __enter__(self) -> _RecordingCursor:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        match = re.search(r"/\* parity:([a-z_]+) \*/", sql)
        if match is None:
            raise AssertionError(f"target query lacks a parity marker: {sql}")
        marker = match.group(1)
        self.pg.statements.append((marker, " ".join(sql.split()), params))
        self.rows = copy.deepcopy(self.pg.responses.get(marker, []))

    def __iter__(self):
        return iter(self.rows)

    def fetchall(self) -> list[dict[str, Any]]:
        raise AssertionError("parity adapters must stream target rows")


class _RecordingPostgres:
    def __init__(self, responses: dict[str, list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.statements: list[tuple[str, str, tuple[Any, ...]]] = []

    def cursor(self, name: str | None = None) -> _RecordingCursor:
        del name
        return _RecordingCursor(self)


def _source() -> sqlite3.Connection:
    source = sqlite3.connect(":memory:")
    source.row_factory = sqlite3.Row
    source.executescript(
        """
        CREATE TABLE jobs (
            url TEXT PRIMARY KEY, title TEXT, company TEXT, site TEXT, source_board TEXT,
            discovered_at TEXT, detail_scraped_at TEXT, scored_at TEXT, applied_at TEXT, salary TEXT
        );
        CREATE TABLE applications (
            id INTEGER PRIMARY KEY, job_url TEXT, channel TEXT, source TEXT, status TEXT,
            notes TEXT, created_at TEXT, updated_at TEXT
        );
        CREATE TABLE application_events (
            id INTEGER PRIMARY KEY, job_url TEXT, status TEXT, event_type TEXT,
            channel TEXT, happened_at TEXT, notes TEXT
        );
        CREATE TABLE email_events (
            message_id TEXT PRIMARY KEY, job_url TEXT, occurred_at TEXT,
            stage TEXT, outcome TEXT, sender TEXT
        );
        CREATE TABLE email_event_reviews (
            id INTEGER PRIMARY KEY, message_id TEXT, review_action TEXT, reviewed_by TEXT,
            reviewed_at TEXT, corrected_job_url TEXT, corrected_stage TEXT,
            corrected_outcome TEXT, corrected_confidence TEXT, resolution TEXT, note TEXT
        );
        CREATE TABLE reviewed_outcomes (
            event_id TEXT, job_url TEXT, attribution_json TEXT, review_status TEXT,
            normalized_stage TEXT, weight REAL, reviewer TEXT, reason TEXT,
            created_at TEXT, reviewed_at TEXT, updated_at TEXT,
            PRIMARY KEY (event_id, job_url)
        );
        CREATE TABLE research_labels (
            id TEXT PRIMARY KEY, job_url TEXT, item_id TEXT, source_project_id TEXT,
            decision TEXT, rating INTEGER, reason TEXT, cleaned_reason TEXT,
            tags_json TEXT, method TEXT, fit_map_feedback_json TEXT,
            review_queue_json TEXT, item_status_at_review TEXT, created_at TEXT,
            raw_event_json TEXT
        );
        CREATE TABLE research_label_confidence (
            label_id TEXT PRIMARY KEY, item_id TEXT, weight REAL, item_flip_rate REAL,
            method TEXT, imported_at TEXT, raw_json TEXT
        );
        CREATE TABLE research_pairwise_labels (
            id TEXT PRIMARY KEY, left_job_url TEXT, right_job_url TEXT,
            left_item_id TEXT, right_item_id TEXT, winner TEXT, method TEXT,
            source_project_id TEXT, created_at TEXT, raw_event_json TEXT
        );
        CREATE TABLE research_kg_artifacts (
            kg_version TEXT PRIMARY KEY, compact_kg_json BLOB, built_at TEXT,
            input_label_count INTEGER, inputs_sha TEXT
        );
        CREATE TABLE research_kg_runs (kg_version TEXT PRIMARY KEY);
        CREATE TABLE research_scores (id INTEGER PRIMARY KEY);
        CREATE TABLE decision_policy_versions (
            policy_version TEXT PRIMARY KEY, lane TEXT, status TEXT,
            qualification_model TEXT, preference_model TEXT, outcome_model TEXT,
            kg_version TEXT, label_snapshot TEXT, pairwise_snapshot TEXT,
            outcome_snapshot TEXT, config_json TEXT, metrics_json TEXT,
            created_at TEXT, validated_at TEXT, activated_at TEXT, retired_at TEXT
        );
        CREATE TABLE job_decisions (
            decision_id TEXT PRIMARY KEY, job_url TEXT, policy_version TEXT, lane TEXT,
            qualification_score REAL, preference_score REAL, outcome_score REAL,
            final_score REAL, qualification_verdict TEXT, action TEXT, confidence REAL,
            uncertainty_json TEXT, blockers_json TEXT, requirements_json TEXT,
            evidence_node_ids_json TEXT, title_signals_json TEXT, explanation TEXT,
            input_hash TEXT, created_at TEXT, expires_at TEXT
        );
        """
    )
    source.execute(
        "INSERT INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?)",
        (JOB_URL, "Platform Engineer", "Example Co", "example", "board", CREATED, None, None, None, "$100k"),
    )
    source.execute(
        "INSERT INTO applications VALUES (?,?,?,?,?,?,?,?)",
        (7, JOB_URL, "referral", "board", "submitted", "tracked", CREATED, "2026-07-16T00:01:00Z"),
    )
    source.execute(
        "INSERT INTO application_events VALUES (?,?,?,?,?,?,?)",
        (8, JOB_URL, "submitted", "submitted", "referral", "2026-07-16T00:02:00Z", "sent"),
    )
    source.execute(
        "INSERT INTO email_events VALUES (?,?,?,?,?,?)",
        (MESSAGE_ID, JOB_URL, "2026-07-16T00:03:00Z", "interview", "positive", "recruiter@example.com"),
    )
    source.execute(
        "INSERT INTO email_event_reviews VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            9,
            MESSAGE_ID,
            "accept",
            "reviewer",
            "2026-07-16T00:04:00Z",
            None,
            "interview",
            "positive",
            "high",
            "trusted",
            "ok",
        ),
    )
    source.execute(
        "INSERT INTO reviewed_outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            MESSAGE_ID,
            JOB_URL,
            '{"rule":"subject"}',
            "accepted",
            "interview",
            1.25,
            "reviewer",
            "verified",
            "2026-07-16T00:04:00Z",
            "2026-07-16T00:05:00Z",
            "2026-07-16T00:06:00Z",
        ),
    )
    source.execute(
        "INSERT INTO research_labels VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "label-1",
            None,
            "item-1",
            "project-1",
            "apply",
            4,
            "strong",
            "strong",
            '["remote"]',
            "human",
            None,
            None,
            "reviewed",
            "2026-07-16T00:07:00Z",
            '{"source":"test"}',
        ),
    )
    source.execute(
        "INSERT INTO research_label_confidence VALUES (?,?,?,?,?,?,?)",
        ("label-1", "confidence-item", 0.75, 0.2, "calibrated", "2026-07-16T00:08:00Z", '{"sample":10}'),
    )
    source.execute(
        "INSERT INTO research_pairwise_labels VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            "pair-1",
            None,
            None,
            "left-item",
            "right-item",
            "left",
            "human",
            "project-1",
            "2026-07-16T00:09:00Z",
            '{"source":"test"}',
        ),
    )
    source.execute(
        "INSERT INTO research_kg_artifacts VALUES (?,?,?,?,?)",
        (KG_HASH, KG_CONTENT, "2026-07-16T00:10:00Z", 1, KG_HASH),
    )
    source.execute(
        "INSERT INTO decision_policy_versions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            POLICY_VERSION,
            "ats",
            "draft",
            "qualification-v1",
            None,
            None,
            KG_HASH,
            LABEL_SNAPSHOT,
            PAIRWISE_SNAPSHOT,
            OUTCOME_SNAPSHOT,
            '{"floor":0.6}',
            None,
            CREATED,
            None,
            None,
            None,
        ),
    )
    source.execute(
        "INSERT INTO job_decisions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "decision-1",
            JOB_URL,
            POLICY_VERSION,
            "ats",
            0.8,
            0.7,
            0.6,
            0.75,
            "qualified",
            "apply",
            0.9,
            '[{"kind":"low"}]',
            None,
            '["python"]',
            '["node-1"]',
            '["platform"]',
            "qualified",
            "a" * 64,
            "2026-07-16T00:11:00Z",
            "2026-07-17T00:11:00Z",
        ),
    )
    source.commit()
    return source


def _responses(source: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    job_id = _job_id(JOB_URL)
    application_id = _application_id(7)
    email_metadata = {"stage": "interview", "outcome": "positive", "sender": "recruiter@example.com"}
    review_metadata = {
        "id": 9,
        "message_id": MESSAGE_ID,
        "review_action": "accept",
        "reviewed_by": "reviewer",
        "reviewed_at": "2026-07-16T00:04:00Z",
        "corrected_job_url": None,
        "corrected_stage": "interview",
        "corrected_outcome": "positive",
        "corrected_confidence": "high",
        "resolution": "trusted",
        "note": "ok",
    }
    label_metadata = {"source": "test"}
    confidence_metadata = {
        "label_id": "label-1",
        "item_id": "confidence-item",
        "weight": 0.75,
        "item_flip_rate": 0.2,
        "method": "calibrated",
        "imported_at": "2026-07-16T00:08:00Z",
        "raw_json": '{"sample":10}',
    }
    policy_row = dict(source.execute("SELECT * FROM decision_policy_versions").fetchone())
    compiled = compile_policy_artifacts(policy_row)
    artifact_bindings = [
        [artifact.role, artifact.sha256]
        for artifact in compiled.artifacts
        if artifact.role
        in {
            "qualification_model",
            "preference_model",
            "outcome_model",
            "config",
            "metrics",
            "label_snapshot",
            "pairwise_snapshot",
            "outcome_snapshot",
        }
    ]
    artifact_bindings.append(["knowledge_graph", KG_HASH])
    artifact_bindings.sort(key=lambda binding: binding[0])
    return {
        "jobs": [
            {
                "job_id": job_id,
                "source_job_id": JOB_URL,
                "canonical_url": JOB_URL,
                "title": "Platform Engineer",
                "company": "Example Co",
                "current_metadata": {
                    "site": "example",
                    "source_board": "board",
                    "discovered_at": CREATED,
                    "detail_scraped_at": None,
                    "scored_at": None,
                    "applied_at": None,
                    "salary": "$100k",
                },
            }
        ],
        "aliases": [
            {
                "job_id": job_id,
                "source_database_fingerprint": SOURCE_FINGERPRINT,
                "source_item_id": JOB_URL,
                "source_url": JOB_URL,
                "alias_type": "sqlite-job",
                "alias_metadata": {},
            }
        ],
        "observations": [
            {
                "source_observation_id": hashlib.sha256(f"observation:{JOB_URL}".encode()).hexdigest(),
                "job_id": job_id,
                "job_source_namespace": SOURCE_NAMESPACE,
                "observed_at": _utc(0),
                "observation_metadata": {
                    "source_url": JOB_URL,
                    "site": "example",
                    "source_board": "board",
                },
                "source_job_id": JOB_URL,
            }
        ],
        "applications": [
            {
                "application_id": application_id,
                "job_id": job_id,
                "source_application_id": "7",
                "source_channel": "referral",
                "lane": "ats",
                "current_state": "submitted",
                "application_metadata": {"source": "board", "notes": "tracked"},
                "created_at": _utc(0),
                "updated_at": _utc(1),
            }
        ],
        "application_events": [
            {
                "application_id": application_id,
                "source_event_id": "8",
                "event_type": "submitted",
                "source_channel": "referral",
                "occurred_at": _utc(2),
                "event_metadata": {"status": "submitted", "notes": "sent", "job_url": JOB_URL},
                "payload_artifact_hash": None,
                "supersedes_application_event_id": None,
            }
        ],
        "email_events": [
            {
                "source_event_id": MESSAGE_ID,
                "job_id": job_id,
                "event_type": "interview",
                "occurred_at": _utc(3),
                "event_metadata": email_metadata,
                "payload_artifact_hash": None,
                "supersedes_email_event_id": None,
            }
        ],
        "email_event_reviews": [
            {
                "source_event_id": "review:9",
                "job_id": job_id,
                "event_type": "review",
                "occurred_at": _utc(4),
                "event_metadata": review_metadata,
                "payload_artifact_hash": None,
                "supersedes_email_event_id": None,
            }
        ],
        "reviewed_outcomes": [
            {
                "parity_key": f"{MESSAGE_ID}:{JOB_URL}",
                "row_kind": "outcome",
                "source_event_id": f"{MESSAGE_ID}:{JOB_URL}",
                "job_id": job_id,
                "review_status": "accepted",
                "normalized_stage": "interview",
                "weight": Decimal("1.25"),
                "reviewer": "reviewer",
                "reason": "verified",
                "review_metadata": {"rule": "subject"},
                "created_at": _utc(4),
                "reviewed_at": _utc(5),
                "updated_at": _utc(6),
                "evidence_artifact_hash": None,
                "supersedes_reviewed_outcome_id": None,
                "linked_source_event_id": MESSAGE_ID,
                "linked_job_id": job_id,
                "linked_event_type": "interview",
                "linked_occurred_at": _utc(3),
                "linked_event_metadata": email_metadata,
                "linked_payload_artifact_hash": None,
                "linked_supersedes_email_event_id": None,
            }
        ],
        "research_labels": [
            {
                "source_event_id": "label-1",
                "job_id": None,
                "source_item_id": "item-1",
                "source_item_url": None,
                "project": "project-1",
                "method": "human",
                "confidence": Decimal("1"),
                "weight": Decimal("1"),
                "label_name": "fitmap_decision",
                "label_value": {"decision": "apply", "rating": 4, "reason": "strong", "tags": '["remote"]'},
                "occurred_at": _utc(7),
                "event_metadata": label_metadata,
                "raw_artifact_hash": None,
                "evidence_artifact_hash": None,
                "supersedes_label_event_source_id": None,
            }
        ],
        "research_label_confidence": [
            {
                "source_event_id": "confidence:label-1",
                "job_id": None,
                "source_item_id": "item-1",
                "source_item_url": None,
                "project": "project-1",
                "method": "calibrated",
                "confidence": Decimal("0.8"),
                "weight": Decimal("0.75"),
                "label_name": "fitmap_decision",
                "label_value": {"decision": "apply", "rating": 4},
                "occurred_at": _utc(8),
                "event_metadata": confidence_metadata,
                "raw_artifact_hash": None,
                "evidence_artifact_hash": None,
                "supersedes_label_event_source_id": "label-1",
            }
        ],
        "research_pairwise_labels": [
            {
                "source_event_id": "pair-1",
                "left_job_id": None,
                "right_job_id": None,
                "left_source_item_id": "left-item",
                "right_source_item_id": "right-item",
                "left_source_url": None,
                "right_source_url": None,
                "project": "project-1",
                "method": "human",
                "confidence": None,
                "weight": Decimal("1"),
                "preference": "left",
                "occurred_at": _utc(9),
                "event_metadata": label_metadata,
                "raw_artifact_hash": None,
                "evidence_artifact_hash": None,
                "supersedes_pairwise_event_id": None,
            }
        ],
        "research_kg_artifacts": [
            {
                "artifact_hash": KG_HASH,
                "byte_length": len(KG_CONTENT),
                "media_type": "application/json",
            }
        ],
        "research_kg_runs": [],
        "research_scores": [],
        "decision_policy_versions": [
            {
                "policy_version": POLICY_VERSION,
                "lane": "ats",
                "gate_definition_version": 1,
                "lifecycle": "draft",
                "policy_metadata": compiled.metadata_object(),
                "created_at": _utc(0),
                "validated_at": None,
                "canary_at": None,
                "activated_at": None,
                "retired_at": None,
                "artifact_bindings": artifact_bindings,
            }
        ],
        "job_decisions": [
            {
                "decision_id": "decision-1",
                "source_decision_id": "decision-1",
                "job_id": job_id,
                "policy_version": POLICY_VERSION,
                "lane": "ats",
                "qualification_score": Decimal("0.8"),
                "qualification_floor": None,
                "preference_score": Decimal("0.7"),
                "outcome_score": Decimal("0.6"),
                "final_score": Decimal("0.75"),
                "qualification_verdict": "qualified",
                "action": "apply",
                "confidence": Decimal("0.9"),
                "uncertainty": [{"kind": "low"}],
                "blockers": [],
                "requirements": ["python"],
                "evidence_nodes": ["node-1"],
                "title_signals": ["platform"],
                "explanation": "qualified",
                "input_hash": "a" * 64,
                "created_at": _utc(11),
                "expires_at": datetime(2026, 7, 17, 0, 11, tzinfo=timezone.utc),
                "uncertainty_artifact_hash": None,
                "blockers_artifact_hash": None,
                "requirements_artifact_hash": None,
                "evidence_artifact_hash": None,
                "decision_artifact_hash": None,
            }
        ],
    }


def _compute(source: sqlite3.Connection, responses: dict[str, list[dict[str, Any]]]):
    from applypilot.brain.parity_adapters import compute_import_parity

    pg = _RecordingPostgres(responses)
    return compute_import_parity(pg, source, source_fingerprint=SOURCE_FINGERPRINT), pg


def test_all_source_table_projections_match_and_queries_are_scoped() -> None:
    source = _source()
    results, pg = _compute(source, _responses(source))

    assert tuple(results) == ("jobs", "aliases", "observations") + tuple(
        table.name for table in SOURCE_TABLES if table.name != "jobs"
    )
    assert all(result.passed for result in results.values())
    assert all(result.source_count == result.target_count for result in results.values())

    statements = {marker: (sql, params) for marker, sql, params in pg.statements}
    for marker in {
        "jobs",
        "aliases",
        "observations",
        "applications",
        "application_events",
        "email_events",
        "email_event_reviews",
        "reviewed_outcomes",
        "research_labels",
        "research_label_confidence",
        "research_pairwise_labels",
    }:
        assert SOURCE_NAMESPACE in statements[marker][1]
    assert "ANY" in statements["decision_policy_versions"][0]
    assert statements["decision_policy_versions"][1] == ([POLICY_VERSION],)
    assert "source_namespace=%s" in statements["job_decisions"][0]
    assert "ANY" not in statements["job_decisions"][0]
    assert statements["job_decisions"][1] == (SOURCE_NAMESPACE,)


@pytest.mark.parametrize(
    ("marker", "field", "bad_value", "result_key"),
    [
        ("research_labels", "source_item_id", "wrong-item", "research_labels"),
        (
            "research_label_confidence",
            "supersedes_label_event_source_id",
            "wrong-predecessor",
            "research_label_confidence",
        ),
        ("decision_policy_versions", "policy_metadata", {"wrong": True}, "decision_policy_versions"),
        ("decision_policy_versions", "artifact_bindings", [], "decision_policy_versions"),
        ("research_kg_artifacts", "byte_length", len(KG_CONTENT) + 1, "research_kg_artifacts"),
        ("research_kg_artifacts", "artifact_hash", "b" * 64, "research_kg_artifacts"),
        ("aliases", "alias_type", "wrong-type", "aliases"),
        ("observations", "source_observation_id", "wrong-observation", "observations"),
        ("observations", "job_source_namespace", "foreign-source", "observations"),
        ("job_decisions", "final_score", Decimal("0.76"), "job_decisions"),
    ],
)
def test_high_risk_one_field_mismatch_fails(
    marker: str,
    field: str,
    bad_value: Any,
    result_key: str,
) -> None:
    source = _source()
    responses = _responses(source)
    responses[marker][0][field] = bad_value

    results, _ = _compute(source, responses)

    assert not results[result_key].passed
    assert results[result_key].mismatch_count == 1
    assert results[result_key].source_hash != results[result_key].target_hash


@pytest.mark.parametrize("membership", ["missing", "extra"])
def test_missing_and_extra_target_membership_each_fail(membership: str) -> None:
    source = _source()
    responses = _responses(source)
    if membership == "missing":
        responses["jobs"] = []
    else:
        extra_url = "https://x"
        responses["jobs"].append(
            {
                "job_id": _job_id(extra_url),
                "source_job_id": extra_url,
                "canonical_url": extra_url,
                "title": "Extra",
                "company": "Extra Co",
                "current_metadata": {},
            }
        )
        responses["jobs"].sort(key=lambda row: (len(row["source_job_id"].encode()), row["source_job_id"].encode()))

    results, _ = _compute(source, responses)

    assert not results["jobs"].passed
    assert results["jobs"].mismatch_count == 1


@pytest.mark.parametrize("nonzero_side", ["source", "target", "both"])
def test_unsupported_research_tables_are_strictly_zero_only(nonzero_side: str) -> None:
    source = _source()
    responses = _responses(source)
    if nonzero_side in {"source", "both"}:
        source.execute("INSERT INTO research_scores VALUES (1)")
    if nonzero_side in {"target", "both"}:
        responses["research_scores"] = [{"unsupported_key": b"one", "unsupported_value": {"id": 1}}]

    results, _ = _compute(source, responses)

    assert not results["research_scores"].passed
    assert results["research_scores"].mismatch_count > 0



def test_policy_parity_bindings_use_wrapper_hashes_not_source_fingerprints() -> None:
    source = _source()
    policy_row = dict(source.execute("SELECT * FROM decision_policy_versions").fetchone())
    compiled = compile_policy_artifacts(policy_row)
    bindings = dict(_responses(source)["decision_policy_versions"][0]["artifact_bindings"])

    for role in ("label_snapshot", "pairwise_snapshot", "outcome_snapshot"):
        artifact = compiled.artifact(role)
        assert artifact is not None
        assert bindings[role] == artifact.sha256
        assert bindings[role] != policy_row[role]
