from __future__ import annotations

import threading
import time
from pathlib import Path


class _SelectResult:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchall(self) -> list[dict]:
        return self._rows


class _RecordingConn:
    def __init__(self, rows: list[dict] | None = None):
        self.rows = rows or []
        self.main_thread_id = threading.get_ident()
        self.update_thread_ids: list[int] = []
        self.commits = 0

    def execute(self, sql: str, params=()):
        if sql.lstrip().upper().startswith("SELECT"):
            return _SelectResult(self.rows)
        self.update_thread_ids.append(threading.get_ident())
        return None

    def commit(self) -> None:
        self.commits += 1


def test_run_tailoring_uses_bounded_workers_and_serializes_db_writes(monkeypatch, tmp_path: Path):
    from applypilot.scoring import tailor

    jobs = [
        {
            "url": f"https://example.com/{idx}",
            "title": f"Role {idx}",
            "site": "TestCo",
            "location": "Remote",
            "fit_score": 9,
            "full_description": "Build operating systems.",
        }
        for idx in range(4)
    ]
    conn = _RecordingConn()
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_tailor_resume(*args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        job = args[1]
        return f"tailored {job['title']}", {
            "status": "approved",
            "attempts": 1,
            "model_used": "fake-model",
        }

    resume = tmp_path / "resume.txt"
    resume.write_text("base resume", encoding="utf-8")
    monkeypatch.setattr(tailor, "RESUME_PATH", resume)
    monkeypatch.setattr(tailor, "TAILORED_DIR", tmp_path / "tailored")
    monkeypatch.setattr(tailor, "load_profile", lambda: {})
    monkeypatch.setattr(tailor, "load_resume_strategy", lambda: {})
    monkeypatch.setattr(tailor, "get_connection", lambda: conn)
    monkeypatch.setattr(tailor, "get_jobs_by_stage", lambda **kwargs: jobs)
    monkeypatch.setattr(tailor, "tailor_resume", fake_tailor_resume)

    result = tailor.run_tailoring(min_score=7, limit=4, validation_mode="lenient", workers=2)

    assert result["approved"] == 4
    assert max_active == 2
    assert conn.update_thread_ids == [conn.main_thread_id] * 4
    assert conn.commits == 4


def test_run_cover_letters_uses_bounded_workers_and_serializes_db_writes(monkeypatch, tmp_path: Path):
    from applypilot.scoring import cover_letter

    jobs = [
        {
            "url": f"https://example.com/{idx}",
            "title": f"Role {idx}",
            "site": "TestCo",
            "location": "Remote",
            "fit_score": 9,
            "audit_score": 9,
            "full_description": "Build operating systems.",
            "tailored_resume_path": str(tmp_path / f"resume-{idx}.txt"),
        }
        for idx in range(4)
    ]
    conn = _RecordingConn(rows=jobs)
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_generate_cover_letter(*args, **kwargs):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        job = args[1]
        return f"Dear Hiring Manager,\n{job['title']}\nJonathan"

    resume = tmp_path / "resume.txt"
    resume.write_text("base resume", encoding="utf-8")
    monkeypatch.setattr(cover_letter, "RESUME_PATH", resume)
    monkeypatch.setattr(cover_letter, "COVER_LETTER_DIR", tmp_path / "covers")
    monkeypatch.setattr(cover_letter, "load_profile", lambda: {})
    monkeypatch.setattr(cover_letter, "load_resume_strategy", lambda: {})
    monkeypatch.setattr(cover_letter, "get_connection", lambda: conn)
    monkeypatch.setattr(cover_letter, "generate_cover_letter", fake_generate_cover_letter)

    result = cover_letter.run_cover_letters(min_score=7, limit=4, validation_mode="lenient", workers=2)

    assert result["generated"] == 4
    assert max_active == 2
    assert conn.update_thread_ids == [conn.main_thread_id] * 4
    assert conn.commits == 4
