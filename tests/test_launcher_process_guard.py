from __future__ import annotations

import inspect
import subprocess
import sys

import pytest

from applypilot.apply import launcher
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
