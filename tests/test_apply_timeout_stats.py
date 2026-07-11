from __future__ import annotations

from pathlib import Path

from applypilot.apply import launcher


class _FakeStdin:
    def write(self, _text: str) -> None:
        return None

    def close(self) -> None:
        return None


class _FakeProc:
    def __init__(self) -> None:
        self.stdin = _FakeStdin()
        self.stdout = []
        self.pid = 4242
        self.returncode = None

    def poll(self):
        return self.returncode


class _FakeThread:
    def __init__(self, *args, **kwargs) -> None:
        self._alive = True

    def start(self) -> None:
        return None

    def is_alive(self) -> bool:
        return self._alive

    def join(self, timeout=None) -> None:
        return None


def _job() -> dict:
    return {
        "url": "https://example.test/job",
        "application_url": "https://example.test/apply",
        "title": "Operations Manager",
        "site": "Acme",
    }


def test_timeout_records_last_run_stats(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(launcher.config, "LOG_DIR", tmp_path)
    monkeypatch.setattr(launcher.config, "APP_DIR", tmp_path)
    monkeypatch.setattr(launcher, "AGENT_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(launcher, "_maybe_greenhouse_apply", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "_maybe_lever_shadow", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda worker_id: tmp_path / f"worker-{worker_id}")
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **kwargs: "prompt")
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **k: _FakeProc())
    monkeypatch.setattr(launcher.threading, "Thread", _FakeThread)
    monkeypatch.setattr(launcher, "_kill_process_tree", lambda pid: None)
    monkeypatch.setattr(launcher, "add_event", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "update_state", lambda *a, **k: None)
    monkeypatch.setattr(launcher, "_last_run_stats", {}, raising=False)

    status, _duration_ms = launcher.run_job(_job(), port=9222, worker_id=3)

    assert status == "failed:timeout"
    stats = launcher._last_run_stats[3]
    assert stats["application_tool_calls"] == 0
    assert stats["job_log_path"] is None
    assert stats["final_result_source"] == "transcript"
    assert stats["transcript_digest"].startswith("sha256:")
    assert stats["result_metadata"]["submit_clicked"] is False
    assert stats["result_metadata"]["phase_costs_usd"] == {"agent_execution": 0.0}
