from __future__ import annotations

import json
from pathlib import Path


def _setup(monkeypatch, tmp_path: Path):
    from applypilot import database, import_decisions

    conn = database.init_db(tmp_path / "applypilot.db")
    monkeypatch.setattr(import_decisions, "init_db", lambda *a, **k: conn)
    monkeypatch.setattr(import_decisions, "get_connection", lambda: conn)
    return conn, import_decisions


def _insert_job(conn, url, *, title="Engineer", site="Co", fit_score=5,
                full_description="desc", applied_at=None):
    conn.execute(
        "INSERT INTO jobs (url, title, site, full_description, fit_score, applied_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (url, title, site, full_description, fit_score, applied_at),
    )
    conn.commit()


def _write(tmp_path, records, name="decisions.jsonl"):
    path = tmp_path / name
    path.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    return path


def test_approved_decision_is_review_evidence_not_audit_authority(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.com/j1", fit_score=5)
    path = _write(tmp_path, [{"url": "https://x.com/j1", "verdict": "approve", "decision_score": 9, "reason": "great fit"}])

    r = mod.import_decisions(path)

    assert r["updated"] == 1
    row = conn.execute(
        "SELECT audit_score, decision_source, decision_verdict, external_decision_score, fit_score, audit_label "
        "FROM jobs WHERE url = ?", ("https://x.com/j1",)).fetchone()
    assert row["audit_score"] is None
    assert row["decision_source"] == "brainstorm"
    assert row["decision_verdict"] == "approve"
    assert row["external_decision_score"] == 9.0
    assert row["fit_score"] == 5              # ApplyPilot's benchmark left untouched
    assert row["audit_label"] is None


def test_non_approved_verdict_is_skipped(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.com/j2")
    path = _write(tmp_path, [{"url": "https://x.com/j2", "verdict": "skip", "decision_score": 9}])

    r = mod.import_decisions(path)

    assert r["skipped_not_approved"] == 1
    assert r["updated"] == 0
    row = conn.execute("SELECT audit_score, decision_source FROM jobs WHERE url = ?", ("https://x.com/j2",)).fetchone()
    assert row["audit_score"] is None and row["decision_source"] is None


def test_score_rescaling(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.com/unit")
    _insert_job(conn, "https://x.com/pct")
    p_unit = _write(tmp_path, [{"url": "https://x.com/unit", "apply": True, "score": 0.9}], "u.jsonl")
    p_pct = _write(tmp_path, [{"url": "https://x.com/pct", "apply": True, "score": 90}], "p.jsonl")

    mod.import_decisions(p_unit, scale="unit")
    mod.import_decisions(p_pct, scale="percent")

    assert conn.execute("SELECT external_decision_score FROM jobs WHERE url=?", ("https://x.com/unit",)).fetchone()[0] == 9.0
    assert conn.execute("SELECT external_decision_score FROM jobs WHERE url=?", ("https://x.com/pct",)).fetchone()[0] == 9.0


def test_already_applied_not_reopened(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.com/done", applied_at="2026-06-01T00:00:00+00:00")
    path = _write(tmp_path, [{"url": "https://x.com/done", "verdict": "approve", "decision_score": 10}])

    r = mod.import_decisions(path)

    assert r["skipped_already_applied"] == 1
    assert conn.execute("SELECT audit_score FROM jobs WHERE url=?", ("https://x.com/done",)).fetchone()[0] is None


def test_unknown_url_inserted_when_title_present(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    path = _write(tmp_path, [{
        "url": "https://x.com/new", "verdict": "approve", "decision_score": 8,
        "title": "Staff Engineer", "company": "NewCo", "full_description": "great role",
    }])

    r = mod.import_decisions(path)

    assert r["inserted"] == 1
    row = conn.execute("SELECT audit_score, title, decision_source FROM jobs WHERE url=?", ("https://x.com/new",)).fetchone()
    assert row["audit_score"] is None and row["title"] == "Staff Engineer" and row["decision_source"] == "brainstorm"


def test_unknown_url_without_title_reported(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    path = _write(tmp_path, [{"url": "https://x.com/ghost", "verdict": "approve", "decision_score": 8}])

    r = mod.import_decisions(path)

    assert r["not_found_insufficient_metadata"] == 1
    assert "https://x.com/ghost" in r["not_found_urls"]


def test_below_threshold_is_flagged(monkeypatch, tmp_path):
    conn, mod = _setup(monkeypatch, tmp_path)
    _insert_job(conn, "https://x.com/low")
    path = _write(tmp_path, [{"url": "https://x.com/low", "verdict": "approve", "decision_score": 5}])

    r = mod.import_decisions(path)

    assert r["updated"] == 1
    assert r["below_apply_threshold"] == 1  # 5 < default apply threshold (7)


def test_audit_stage_skips_decided_rows(monkeypatch, tmp_path):
    from applypilot import pipeline

    conn, mod = _setup(monkeypatch, tmp_path)
    # One brainstorm-decided row (should NOT be re-audited) and one plain scored
    # row (should still be pending for audit).
    _insert_job(conn, "https://x.com/decided", fit_score=8)
    _insert_job(conn, "https://x.com/plain", fit_score=8)
    mod.import_decisions(_write(tmp_path, [{"url": "https://x.com/decided", "verdict": "approve", "decision_score": 9}]))

    monkeypatch.setattr(pipeline, "get_connection", lambda: conn)
    pending = pipeline._count_pending("audit")

    assert pending == 1  # only the plain row; the decided row is excluded
