from __future__ import annotations

import sqlite3

import pytest

from applypilot.brain.source_receipt import SourceReceiptError, capture_sqlite_source_receipt


def _make_source(path):
    connection = sqlite3.connect(path)
    try:
        for table in (
            "jobs", "applications", "application_events", "email_events",
            "email_event_reviews", "reviewed_outcomes", "research_labels",
            "research_label_confidence", "research_pairwise_labels",
            "research_kg_artifacts", "research_kg_runs", "research_scores",
            "decision_policy_versions", "job_decisions",
        ):
            key = "url TEXT PRIMARY KEY" if table == "jobs" else "id INTEGER PRIMARY KEY"
            connection.execute(f'CREATE TABLE "{table}" ({key})')
        connection.execute("INSERT INTO jobs(url) VALUES ('https://example.test/job')")
        connection.commit()
    finally:
        connection.close()


def test_receipt_contains_required_counts_and_fingerprint(tmp_path):
    path = tmp_path / "applypilot.db"
    _make_source(path)

    receipt = capture_sqlite_source_receipt(path)

    assert receipt.quick_check == "ok"
    assert receipt.sha256
    assert receipt.byte_length == path.stat().st_size
    assert receipt.table_counts["jobs"] == 1
    assert set(receipt.table_counts) == {
        "jobs", "applications", "application_events", "email_events",
        "email_event_reviews", "reviewed_outcomes", "research_labels",
        "research_label_confidence", "research_pairwise_labels",
        "research_kg_artifacts", "research_kg_runs", "research_scores",
        "decision_policy_versions", "job_decisions",
    }


@pytest.mark.parametrize(("suffix", "content"), [("-wal", b""), ("-wal", b"wal"), ("-shm", b"shm")])
def test_receipt_rejects_any_sidecar_state(tmp_path, suffix, content):
    path = tmp_path / "applypilot.db"
    _make_source(path)
    path.with_name(path.name + suffix).write_bytes(content)

    with pytest.raises(SourceReceiptError, match="has sidecar state"):
        capture_sqlite_source_receipt(path)
