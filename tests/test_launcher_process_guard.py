from __future__ import annotations

import inspect
import json
import subprocess
import sys

import pytest

from applypilot.apply import launcher
from applypilot.apply import lifecycle_fault
from applypilot.apply import process_guard
from applypilot.apply.process_guard import SpawnedChildGuard


class _RunningProcess:
    pid = 7311

    def poll(self):
        return None


class _RecordingGuard:
    def __init__(self):
        self.calls = 0

    def terminate_and_reap(self):
        self.calls += 1
        return True


class _UncertainGuard:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def terminate_and_reap(self):
        if self.error is not None:
            raise self.error
        return False


@pytest.mark.parametrize(
    "reason",
    [
        "terminal-result-grace",
        "reader-timeout",
        "wait-timeout",
        "finally",
        "sigint-skip",
        "sigint-stop",
    ],
)
def test_each_agent_cleanup_category_uses_stable_guard(reason):
    guard = _RecordingGuard()

    assert launcher._terminate_agent_child(4, _RunningProcess(), guard, reason) is True
    assert guard.calls == 1


@pytest.mark.parametrize(
    "reason",
    [
        "terminal-result-grace",
        "reader-timeout",
        "wait-timeout",
        "finally",
        "sigint-skip",
        "sigint-stop",
    ],
)
@pytest.mark.parametrize("error", [None, RuntimeError("cleanup crashed")])
def test_each_agent_cleanup_uncertainty_persists_and_raises(
    reason, error, tmp_path, monkeypatch
):
    monkeypatch.setattr(launcher.config, "DB_PATH", tmp_path / "applypilot.db")

    with pytest.raises(launcher.LifecycleHardFault, match=reason):
        launcher._terminate_agent_child(4, _RunningProcess(), _UncertainGuard(error), reason)

    faults = list((tmp_path / "lifecycle-faults").glob("fault-*.json"))
    assert len(faults) == 1
    payload = json.loads(faults[0].read_text(encoding="utf-8"))
    assert payload["pid"] == 7311
    assert reason in payload["reason"]


def test_launcher_has_no_chrome_reservation_cleanup_dependency():
    source = inspect.getsource(launcher)

    assert "_kill_process_tree" not in source
    for reason in (
        "terminal-result-grace",
        "reader-timeout",
        "wait-timeout",
        "finally",
        "sigint-skip",
        "sigint-stop",
    ):
        assert source.count(f'"{reason}"') == 1


def test_spawned_child_guard_terminates_and_reaps_exact_child():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    guard = SpawnedChildGuard.acquire(process)
    assert guard is not None

    try:
        assert guard.terminate_and_reap(timeout=10) is True
        assert process.poll() is not None
        assert guard.released is True
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)


def test_completed_child_guard_is_releasable_without_identity_capture():
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    process.wait(timeout=10)

    guard = SpawnedChildGuard.acquire(process)

    assert guard is not None
    assert guard.kind == "completed"
    assert guard.terminate_and_reap() is True
    assert guard.released is True


def test_posix_emergency_cleanup_signals_before_wait_without_poll(monkeypatch):
    events = []

    class Process:
        pid = 8123

        def poll(self):
            raise AssertionError("emergency cleanup must not poll before signaling")

        def kill(self):
            events.append("kill")

        def wait(self, timeout):
            events.append(("wait", timeout))
            return -9

    monkeypatch.setattr(process_guard.platform, "system", lambda: "Linux")

    assert process_guard.emergency_cleanup_direct_child(Process(), timeout=3) is True
    assert events == ["kill", ("wait", 3)]


def test_windows_emergency_cleanup_uses_existing_popen_handle(monkeypatch):
    events = []

    class Process:
        pid = 8123
        _handle = 991

        def poll(self):
            raise AssertionError("emergency cleanup must not poll before termination")

        def wait(self, timeout):
            events.append(("wait", timeout))
            return 1

    monkeypatch.setattr(process_guard.platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        process_guard,
        "_terminate_windows_handle",
        lambda handle, pid: events.append(("terminate", handle, pid)) or True,
    )

    assert process_guard.emergency_cleanup_direct_child(Process(), timeout=4) is True
    assert events == [("terminate", 991, 8123), ("wait", 4)]


def test_emergency_cleanup_reports_uncertain_wait_failure(monkeypatch):
    class Process:
        pid = 8123

        def kill(self):
            pass

        def wait(self, timeout):
            raise RuntimeError("wait authority failed")

    monkeypatch.setattr(process_guard.platform, "system", lambda: "Linux")

    assert process_guard.emergency_cleanup_direct_child(Process()) is False


def test_real_child_emergency_cleanup_reaps_without_leak():
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    try:
        assert process_guard.emergency_cleanup_direct_child(process, timeout=10) is True
        assert process.returncode is not None
    finally:
        if process.returncode is None:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.parametrize("cleanup_proven", [True, False])
def test_launcher_guard_acquisition_failure_never_continues(
    cleanup_proven, tmp_path, monkeypatch
):
    process = _RunningProcess()
    monkeypatch.setattr(launcher.config, "DB_PATH", tmp_path / "applypilot.db")
    monkeypatch.setattr(launcher.SpawnedChildGuard, "acquire", lambda _proc: None)
    monkeypatch.setattr(
        launcher,
        "emergency_cleanup_direct_child",
        lambda proc: proc is process and cleanup_proven,
    )

    expected = RuntimeError if cleanup_proven else launcher.LifecycleHardFault
    with pytest.raises(expected, match="stable agent child guard"):
        launcher._acquire_agent_child_guard(process)
    assert bool(lifecycle_fault.lifecycle_hard_fault_paths()) is (not cleanup_proven)


def test_local_lstart_parser_uses_local_wall_time_mktime(monkeypatch):
    captured = []
    monkeypatch.setattr(
        process_guard.time,
        "mktime",
        lambda fields: captured.append(fields) or 321.5,
    )

    epoch = process_guard.parse_ps_lstart_local("Sat Jul 11 12:34:56 2026")

    assert epoch == 321.5
    assert captured[0].tm_year == 2026
    assert captured[0].tm_isdst == -1


def test_launcher_uncertain_guard_cleanup_persists_interlock_and_escapes_job_result(
    tmp_path, monkeypatch
):
    import io

    from applypilot.apply import supervisor

    class Process:
        pid = 8123
        stdin = io.StringIO()
        stdout = iter(())

        def poll(self):
            return None

    process = Process()
    monkeypatch.setattr(launcher.config, "DB_PATH", tmp_path / "applypilot.db")
    monkeypatch.setattr(launcher.config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(launcher.config, "APP_DIR", tmp_path)
    monkeypatch.setattr(launcher.config, "resolve_resume_stem", lambda _path: None)
    monkeypatch.setattr(launcher, "_maybe_greenhouse_apply", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "_maybe_lever_shadow", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "reset_worker_dir", lambda _worker: tmp_path)
    monkeypatch.setattr(launcher.prompt_mod, "build_prompt", lambda **_kwargs: "prompt")
    monkeypatch.setattr(launcher, "_make_mcp_config", lambda _port: {})
    monkeypatch.setattr(launcher, "build_apply_agent_command", lambda **_kwargs: ["agent"])
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(launcher.SpawnedChildGuard, "acquire", lambda _proc: None)
    monkeypatch.setattr(launcher, "emergency_cleanup_direct_child", lambda _proc: False)
    monkeypatch.setattr(launcher, "add_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launcher, "update_state", lambda *_args, **_kwargs: None)

    job = {
        "url": "https://example.invalid/job/1",
        "application_url": "https://example.invalid/job/1",
        "title": "Test Role",
        "site": "Example",
        "fit_score": 8,
        "tailored_resume_path": None,
    }
    with pytest.raises(launcher.LifecycleHardFault):
        launcher._run_job_impl(job, port=9400, worker_id=0)

    assert len(lifecycle_fault.lifecycle_hard_fault_paths()) >= 1
    monkeypatch.delenv("APPLYPILOT_RECONCILE_HARD_FAULT", raising=False)
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    with pytest.raises(RuntimeError, match="hard-fault records present"):
        supervisor._enforce_hard_fault_gate()
