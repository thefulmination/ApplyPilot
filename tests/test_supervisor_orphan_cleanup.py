from __future__ import annotations

from pathlib import Path

from applypilot.apply import supervisor


class _Ownership:
    def __init__(self, *, cleanup_result: bool = True, cleanup_error: Exception | None = None):
        self.cleanup_result = cleanup_result
        self.cleanup_error = cleanup_error
        self.cleanup_calls = 0
        self.release_calls = 0

    def cleanup_browser(self) -> bool:
        self.cleanup_calls += 1
        if self.cleanup_error is not None:
            raise self.cleanup_error
        return self.cleanup_result

    def release(self) -> None:
        self.release_calls += 1


def test_reservation_contender_skips_before_process_enumeration_or_kill(monkeypatch):
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: None)
    monkeypatch.setattr(
        supervisor,
        "_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("must not enumerate")),
    )
    monkeypatch.setattr(
        supervisor,
        "_kill_auxiliary_process",
        lambda pid: (_ for _ in ()).throw(AssertionError("must not kill")),
    )

    assert supervisor._cleanup_orphans(lambda message: None, owner_pid=100) is False


def test_only_owned_associated_auxiliary_is_cleaned(monkeypatch):
    ownership = _Ownership()
    killed = []
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: ownership)
    monkeypatch.setattr(
        supervisor,
        "_process_snapshot",
        lambda: [
            {"pid": 101, "ppid": 100, "name": "python.exe", "command": "applypilot"},
            {"pid": 102, "ppid": 101, "name": "node.exe", "command": "node @playwright/mcp"},
            {"pid": 202, "ppid": 200, "name": "node.exe", "command": "node @playwright/mcp"},
            {"pid": 103, "ppid": 100, "name": "node.exe", "command": "node unrelated.js"},
        ],
    )
    monkeypatch.setattr(supervisor, "_kill_auxiliary_process", lambda pid: killed.append(pid))

    assert supervisor._cleanup_orphans(lambda message: None, owner_pid=100) is True
    assert killed == [102]
    assert ownership.cleanup_calls == 1
    assert ownership.release_calls == 1


def test_no_owner_pid_leaves_all_auxiliary_processes_untouched(monkeypatch):
    ownership = _Ownership()
    killed = []
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: ownership)
    monkeypatch.setattr(
        supervisor,
        "_process_snapshot",
        lambda: [{"pid": 202, "ppid": 200, "name": "node.exe", "command": "node playwright"}],
    )
    monkeypatch.setattr(supervisor, "_kill_auxiliary_process", lambda pid: killed.append(pid))

    assert supervisor._cleanup_orphans(lambda message: None, owner_pid=None) is True
    assert killed == []
    assert ownership.release_calls == 1


def test_cleanup_failure_releases_ownership(monkeypatch):
    ownership = _Ownership(cleanup_error=RuntimeError("cleanup failed"))
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: ownership)
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: [])

    assert supervisor._cleanup_orphans(lambda message: None, owner_pid=100) is False
    assert ownership.release_calls == 1


def test_associated_auxiliary_requires_descendant_node_and_marker():
    processes = [
        {"pid": 2, "ppid": 1, "name": "python", "command": "applypilot"},
        {"pid": 3, "ppid": 2, "name": "node", "command": "node _npx playwright"},
        {"pid": 4, "ppid": 2, "name": "node", "command": "node ordinary.js"},
        {"pid": 5, "ppid": 9, "name": "node", "command": "node @playwright/mcp"},
    ]

    assert supervisor._associated_auxiliary_pids(processes, owner_pid=1) == [3]


def test_public_browser_cleanup_ownership_releases_after_failure(tmp_path: Path, monkeypatch):
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    profile = tmp_path / "profile-0"
    ownership = chrome.reserve_browser_cleanup(0, chrome.BASE_CDP_PORT, profile)
    assert ownership is not None
    monkeypatch.setattr(
        chrome,
        "_kill_on_port",
        lambda port: (_ for _ in ()).throw(RuntimeError("kill failed")),
    )
    try:
        try:
            ownership.cleanup_browser()
        except RuntimeError:
            pass
    finally:
        ownership.release()

    reacquired = chrome.reserve_browser_cleanup(0, chrome.BASE_CDP_PORT, profile)
    assert reacquired is not None
    reacquired.release()
