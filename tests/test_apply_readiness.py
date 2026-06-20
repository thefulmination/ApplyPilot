from __future__ import annotations

from pathlib import Path

from applypilot import database
from applypilot.apply import readiness


def _insert_ready_job(
    conn,
    *,
    url: str,
    title: str,
    resume_path: str,
    application_url: str | None = None,
    fit_score: int = 8,
) -> None:
    conn.execute(
        """
        INSERT INTO jobs (
            url, title, site, company, application_url, tailored_resume_path,
            fit_score, full_description, discovered_at
        )
        VALUES (?, ?, 'ExampleBoard', 'ExampleCo', ?, ?, ?, 'description', '2026-06-01T00:00:00+00:00')
        """,
        (url, title, application_url, resume_path, fit_score),
    )
    conn.commit()


def test_preapply_readiness_blocks_missing_resume_pdf(tmp_path: Path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    txt_path = tmp_path / "resume.txt"
    txt_path.write_text("resume text", encoding="utf-8")
    _insert_ready_job(
        conn,
        url="https://example.com/job/1",
        title="Chief of Staff",
        application_url="https://example.com/apply/1",
        resume_path=str(txt_path),
    )

    monkeypatch.setattr(readiness, "get_connection", lambda: conn)
    monkeypatch.setattr(readiness.config, "is_manual_ats", lambda _url: False)
    monkeypatch.setattr(readiness.config, "is_auth_gated_application", lambda _url: False)

    checks = readiness.collect_preapply_checks(min_score=7, limit=10, stale_days=0)
    summary = readiness.summarize_checks(checks)

    assert summary["blocked"] == 1
    assert checks[0]["severity"] == "blocked"
    assert {issue["code"] for issue in checks[0]["issues"]} == {
        "missing_resume_pdf",
        "missing_cover_letter",
    }


def test_preapply_readiness_flags_duplicate_application_targets(tmp_path: Path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    first_resume = tmp_path / "first.txt"
    second_resume = tmp_path / "second.txt"
    for path in (first_resume, second_resume):
        path.write_text("resume text", encoding="utf-8")
        path.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n")

    shared_apply_url = "https://example.com/apply/shared"
    _insert_ready_job(
        conn,
        url="https://example.com/job/1",
        title="Chief of Staff",
        application_url=shared_apply_url,
        resume_path=str(first_resume),
    )
    _insert_ready_job(
        conn,
        url="https://example.com/job/2",
        title="Strategy Lead",
        application_url=shared_apply_url,
        resume_path=str(second_resume),
    )

    monkeypatch.setattr(readiness, "get_connection", lambda: conn)
    monkeypatch.setattr(readiness.config, "is_manual_ats", lambda _url: False)
    monkeypatch.setattr(readiness.config, "is_auth_gated_application", lambda _url: False)

    checks = readiness.collect_preapply_checks(min_score=7, limit=10, stale_days=0)
    summary = readiness.summarize_checks(checks)

    assert summary["blocked"] == 2
    assert summary["issue_counts"]["duplicate_application_target"] == 2


def test_preapply_readiness_blocks_auth_gated_applications(tmp_path: Path, monkeypatch) -> None:
    conn = database.init_db(tmp_path / "applypilot.db")
    resume_path = tmp_path / "resume.txt"
    resume_path.write_text("resume text", encoding="utf-8")
    resume_path.with_suffix(".pdf").write_bytes(b"%PDF-1.4\n")
    _insert_ready_job(
        conn,
        url="https://example.com/job/3",
        title="COO",
        application_url="https://company.myworkdayjobs.com/login",
        resume_path=str(resume_path),
    )

    monkeypatch.setattr(readiness, "get_connection", lambda: conn)
    monkeypatch.setattr(readiness.config, "is_manual_ats", lambda _url: False)
    monkeypatch.setattr(readiness.config, "is_auth_gated_application", lambda _url: True)

    checks = readiness.collect_preapply_checks(min_score=7, limit=10, stale_days=0)
    summary = readiness.summarize_checks(checks)

    assert summary["blocked"] == 1
    assert checks[0]["severity"] == "blocked"
    assert "auth_gate" in {issue["code"] for issue in checks[0]["issues"]}
