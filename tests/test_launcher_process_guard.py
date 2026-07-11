from __future__ import annotations

import inspect
import subprocess
import sys

import pytest

from applypilot.apply import launcher
from applypilot.apply.process_guard import SpawnedChildGuard
from applypilot.apply import process_guard


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
    cleanup_proven, monkeypatch
):
    process = _RunningProcess()
    monkeypatch.setattr(launcher.SpawnedChildGuard, "acquire", lambda _proc: None)
    monkeypatch.setattr(
        launcher,
        "emergency_cleanup_direct_child",
        lambda proc: proc is process and cleanup_proven,
    )

    with pytest.raises(RuntimeError, match="stable agent child guard"):
        launcher._acquire_agent_child_guard(process)
