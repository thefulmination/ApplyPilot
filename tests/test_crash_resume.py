from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from applypilot import database


def _insert_job(conn: sqlite3.Connection, url: str, title: str, *, scored: bool = False) -> None:
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, site, location, description, full_description,
            fit_score, scored_at
        )
        VALUES (?, ?, 'TestCo', 'Remote', 'Short description', ?, ?, ?)
        """,
        (url, title, "Full job description " * 20,
         7 if scored else None, "2026-05-07T00:00:00+00:00" if scored else None),
    )
    conn.commit()


def test_pipeline_run_checkpoint_tables_record_stage_progress(tmp_path: Path) -> None:
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)

    run_id = database.create_pipeline_run(
        conn,
        stages=["score", "audit"],
        mode="sequential",
        min_score=8,
        batch_size=25,
        workers=2,
        validation_mode="normal",
    )
    stage_id = database.start_pipeline_stage(conn, run_id, "score", pending_before=3)
    database.finish_pipeline_stage(
        conn,
        stage_id,
        status="ok",
        pending_after=1,
        elapsed_seconds=12.5,
    )
    database.finish_pipeline_run(conn, run_id, status="partial", error="audit failed")

    run = conn.execute("SELECT * FROM pipeline_runs WHERE id = ?", (run_id,)).fetchone()
    stage = conn.execute("SELECT * FROM pipeline_stage_runs WHERE id = ?", (stage_id,)).fetchone()

    assert run["status"] == "partial"
    assert run["stages"] == "score,audit"
    assert run["mode"] == "sequential"
    assert run["error"] == "audit failed"
    assert run["ended_at"] is not None

    assert stage["run_id"] == run_id
    assert stage["stage"] == "score"
    assert stage["status"] == "ok"
    assert stage["pending_before"] == 3
    assert stage["pending_after"] == 1
    assert stage["elapsed_seconds"] == 12.5
    assert stage["ended_at"] is not None


def test_sequential_pipeline_records_stage_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    from applypilot import pipeline

    pending_values = [4, 1]
    started: list[tuple[int, str, int]] = []
    finished: list[dict] = []

    monkeypatch.setitem(pipeline._STAGE_RUNNERS, "score", lambda: {"status": "ok"})
    monkeypatch.setattr(pipeline, "_count_pending", lambda _stage, _min_score=7: pending_values.pop(0))
    monkeypatch.setattr(pipeline, "get_connection", lambda: object())

    def fake_start_stage(_conn: object, run_id: int, stage: str, pending_before: int | None = None) -> int:
        started.append((run_id, stage, pending_before or 0))
        return 123

    def fake_finish_stage(_conn: object, stage_run_id: int, **kwargs: object) -> None:
        finished.append({"stage_run_id": stage_run_id, **kwargs})

    monkeypatch.setattr(pipeline, "start_pipeline_stage", fake_start_stage)
    monkeypatch.setattr(pipeline, "finish_pipeline_stage", fake_finish_stage)

    result = pipeline._run_sequential(["score"], min_score=7, run_id=99)

    assert result["errors"] == {}
    assert started == [(99, "score", 4)]
    assert finished[0]["stage_run_id"] == 123
    assert finished[0]["status"] == "ok"
    assert finished[0]["pending_after"] == 1
    assert isinstance(finished[0]["elapsed_seconds"], float)


def test_scoring_persists_each_job_before_a_later_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    _insert_job(conn, "https://example.com/one", "Chief of Staff")
    _insert_job(conn, "https://example.com/two", "Business Operations")

    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Resume text", encoding="utf-8")

    from applypilot.scoring import scorer

    calls = 0

    # antigravity: test-crash-resume-fix-1
    def fake_score_job(_resume_text: str, job: dict, preference_profile: dict | None = None, **kwargs) -> dict:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated crash")
        return {"score": 9, "keywords": "operations", "reasoning": f"matched {job['title']}"}

    monkeypatch.setattr(scorer, "RESUME_PATH", resume_path)
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "score_job", fake_score_job)

    with pytest.raises(RuntimeError, match="simulated crash"):
        scorer.run_scoring()

    conn.rollback()
    first = conn.execute("SELECT fit_score, score_reasoning, scored_at FROM jobs WHERE url = ?", ("https://example.com/one",)).fetchone()
    second = conn.execute("SELECT fit_score FROM jobs WHERE url = ?", ("https://example.com/two",)).fetchone()

    assert first["fit_score"] == 9
    assert "operations" in first["score_reasoning"]
    assert first["scored_at"] is not None
    assert second["fit_score"] is None


def test_llm_score_errors_remain_retryable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    _insert_job(conn, "https://example.com/one", "Chief of Staff")

    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Resume text", encoding="utf-8")

    from applypilot.scoring import scorer

    # antigravity: test-crash-resume-fix-2
    def fake_score_job(_resume_text: str, _job: dict, preference_profile: dict | None = None, **kwargs) -> dict:
        return {"score": 0, "keywords": "", "reasoning": "LLM error: provider unavailable", "error": "provider unavailable"}

    monkeypatch.setattr(scorer, "RESUME_PATH", resume_path)
    monkeypatch.setattr(scorer, "get_connection", lambda: conn)
    monkeypatch.setattr(scorer, "score_job", fake_score_job)

    result = scorer.run_scoring()

    row = conn.execute(
        """
        SELECT fit_score, score_reasoning, score_error, score_error_at,
               score_attempts, scored_at
          FROM jobs
         WHERE url = ?
        """,
        ("https://example.com/one",),
    ).fetchone()

    assert result["scored"] == 0
    assert result["errors"] == 1
    assert row["fit_score"] is None
    assert row["scored_at"] is None
    assert row["score_error"] == "provider unavailable"
    assert row["score_error_at"] is not None
    assert row["score_attempts"] == 1
    assert database.get_jobs_by_stage(conn, "pending_score", limit=10)[0]["url"] == "https://example.com/one"


def test_audit_persists_each_job_before_a_later_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    _insert_job(conn, "https://example.com/one", "Chief of Staff", scored=True)
    _insert_job(conn, "https://example.com/two", "Business Operations", scored=True)

    from applypilot.scoring import audit

    calls = 0

    def fake_audit_job(job: dict, search_cfg: dict | None = None) -> audit.ScoreAudit:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated audit crash")
        return audit.ScoreAudit(
            role_fit_score=95,
            audit_score=9.2,
            audit_label="priority",
            flags=["chief_of_staff"],
            reason=f"matched {job['title']}",
        )

    monkeypatch.setattr(audit, "init_db", lambda: conn)
    monkeypatch.setattr(audit, "get_connection", lambda: conn)
    monkeypatch.setattr(audit.config, "load_search_config", lambda: {})
    monkeypatch.setattr(audit, "audit_job", fake_audit_job)

    with pytest.raises(RuntimeError, match="simulated audit crash"):
        audit.run_score_audit(write_reports=False)

    conn.rollback()
    first = conn.execute(
        "SELECT audit_score, audit_label, audit_flags, audited_at FROM jobs WHERE url = ?",
        ("https://example.com/one",),
    ).fetchone()
    second = conn.execute("SELECT audit_score FROM jobs WHERE url = ?", ("https://example.com/two",)).fetchone()

    assert first["audit_score"] == 9.2
    assert first["audit_label"] == "priority"
    assert "chief_of_staff" in first["audit_flags"]
    assert first["audited_at"] is not None
    assert second["audit_score"] is None


def test_audit_only_processes_pending_or_stale_scores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    _insert_job(conn, "https://example.com/current", "Already Audited", scored=True)
    _insert_job(conn, "https://example.com/pending", "Needs Audit", scored=True)
    conn.execute(
        """
        UPDATE jobs
           SET audit_score = 8.0,
               audit_label = 'recommended',
               audited_at = scored_at
         WHERE url = 'https://example.com/current'
        """
    )
    conn.commit()

    from applypilot.scoring import audit

    seen: list[str] = []

    def fake_audit_job(job: dict, search_cfg: dict | None = None) -> audit.ScoreAudit:
        seen.append(job["url"])
        return audit.ScoreAudit(
            role_fit_score=90,
            audit_score=9.0,
            audit_label="priority",
            flags=[],
            reason="pending only",
        )

    monkeypatch.setattr(audit, "init_db", lambda: conn)
    monkeypatch.setattr(audit, "get_connection", lambda: conn)
    monkeypatch.setattr(audit.config, "load_search_config", lambda: {})
    monkeypatch.setattr(audit, "audit_job", fake_audit_job)

    audit.run_score_audit(write_reports=False)

    assert seen == ["https://example.com/pending"]


def test_diagnosis_persists_each_job_before_a_later_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "applypilot.db"
    conn = database.init_db(db_path)
    _insert_job(conn, "https://example.com/one", "Chief of Staff", scored=True)
    _insert_job(conn, "https://example.com/two", "Business Operations", scored=True)

    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("Resume text", encoding="utf-8")

    from applypilot.scoring import diagnosis

    calls = 0

    def fake_diagnose_job(_resume_text: str, job: dict) -> diagnosis.FitDiagnosis:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated diagnosis crash")
        return diagnosis.FitDiagnosis(
            gap_category="strong_transferable_fit",
            gap_severity="low",
            resume_fixability="yes",
            recommended_action="tailor_resume",
            summary=f"diagnosed {job['title']}",
            resume_evidence=["operations leadership"],
            missing_or_weak_evidence=[],
            resume_changes=["emphasize operating cadence"],
            confidence="high",
            model="test-model",
            provider="test-provider",
        )

    monkeypatch.setattr(diagnosis, "RESUME_PATH", resume_path)
    monkeypatch.setattr(diagnosis, "init_db", lambda: conn)
    monkeypatch.setattr(diagnosis, "get_connection", lambda: conn)
    monkeypatch.setattr(diagnosis, "diagnose_job", fake_diagnose_job)

    with pytest.raises(RuntimeError, match="simulated diagnosis crash"):
        diagnosis.run_diagnostics()

    conn.rollback()
    first = conn.execute(
        """
        SELECT fit_gap_category, recommended_action, fit_diagnosis,
               diagnosis_model, diagnosis_provider, diagnosed_at
          FROM jobs
         WHERE url = ?
        """,
        ("https://example.com/one",),
    ).fetchone()
    second = conn.execute(
        "SELECT diagnosed_at FROM jobs WHERE url = ?",
        ("https://example.com/two",),
    ).fetchone()

    assert first["fit_gap_category"] == "strong_transferable_fit"
    assert first["recommended_action"] == "tailor_resume"
    assert "diagnosed Chief of Staff" in first["fit_diagnosis"]
    assert first["diagnosis_model"] == "test-model"
    assert first["diagnosis_provider"] == "test-provider"
    assert first["diagnosed_at"] is not None
    assert second["diagnosed_at"] is None
