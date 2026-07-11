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


def _owner() -> supervisor.SupervisedProcessIdentity:
    return supervisor.SupervisedProcessIdentity(
        pid=100,
        created_at=10.0,
        executable="python.exe",
        command="python -m applypilot.cli apply",
        launched_at=9.0,
        ended_at=20.0,
    )


def test_reservation_contender_skips_before_process_enumeration_or_kill(monkeypatch):
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: None)
    monkeypatch.setattr(
        supervisor,
        "_process_snapshot",
        lambda: (_ for _ in ()).throw(AssertionError("must not enumerate")),
    )
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        lambda **identity: (_ for _ in ()).throw(AssertionError("must not kill")),
    )

    assert supervisor._cleanup_orphans(lambda message: None, owner=_owner()) is False


def test_only_owned_associated_auxiliary_is_cleaned(monkeypatch):
    ownership = _Ownership()
    killed = []
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: ownership)
    monkeypatch.setattr(
        supervisor,
        "_process_snapshot",
        lambda: [
            {"pid": 101, "ppid": 100, "name": "python.exe", "executable": "C:/Python/python.exe", "command": "applypilot", "created": 11.0},
            {"pid": 102, "ppid": 101, "name": "node.exe", "executable": "C:/Node/node.exe", "command": "node @playwright/mcp", "created": 12.0},
            {"pid": 202, "ppid": 200, "name": "node.exe", "executable": "C:/Node/node.exe", "command": "node @playwright/mcp", "created": 12.0},
            {"pid": 103, "ppid": 100, "name": "node.exe", "executable": "C:/Node/node.exe", "command": "node unrelated.js", "created": 12.0},
        ],
    )
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        lambda **identity: killed.append(identity["pid"]) or True,
    )

    assert supervisor._cleanup_orphans(lambda message: None, owner=_owner()) is True
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
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        lambda **identity: killed.append(identity["pid"]) or True,
    )

    assert supervisor._cleanup_orphans(lambda message: None, owner=None) is True
    assert killed == []
    assert ownership.release_calls == 1


def test_cleanup_failure_releases_ownership(monkeypatch):
    ownership = _Ownership(cleanup_error=RuntimeError("cleanup failed"))
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: ownership)
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: [])

    assert supervisor._cleanup_orphans(lambda message: None, owner=_owner()) is False
    assert ownership.release_calls == 1


def test_associated_auxiliary_requires_descendant_node_and_marker():
    owner = supervisor.SupervisedProcessIdentity(
        pid=1,
        created_at=1.0,
        executable="python.exe",
        command="python -m applypilot.cli apply",
        launched_at=1.0,
        ended_at=10.0,
    )
    processes = [
        {"pid": 2, "ppid": 1, "name": "python", "command": "applypilot", "created": 2.0},
        {"pid": 3, "ppid": 2, "name": "node", "command": "node _npx playwright", "created": 3.0},
        {"pid": 4, "ppid": 2, "name": "node", "command": "node ordinary.js", "created": 4.0},
        {"pid": 5, "ppid": 9, "name": "node", "command": "node @playwright/mcp", "created": 5.0},
    ]

    assert supervisor._associated_auxiliary_pids(processes, owner=owner) == [3]


def test_public_browser_cleanup_ownership_releases_after_failure(tmp_path: Path, monkeypatch):
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    profile = tmp_path / "profile-0"
    ownership = chrome.reserve_browser_cleanup(0, chrome.BASE_CDP_PORT, profile)
    assert ownership is not None
    monkeypatch.setattr(
        chrome,
        "_cleanup_reserved_listener",
        lambda reservation: (_ for _ in ()).throw(RuntimeError("kill failed")),
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


def test_reused_supervisor_owner_pid_leaves_descendants_untouched():
    owner = supervisor.SupervisedProcessIdentity(
        pid=100,
        created_at=10.0,
        executable="python.exe",
        command="python -m applypilot.cli apply",
        launched_at=9.0,
        ended_at=20.0,
    )
    processes = [
        {"pid": 100, "ppid": 1, "name": "python.exe", "command": "unrelated", "created": 30.0},
        {"pid": 101, "ppid": 100, "name": "node.exe", "command": "node @playwright/mcp", "created": 12.0},
    ]

    assert supervisor._associated_auxiliary_pids(processes, owner=owner) == []


def test_valid_lifetime_descendants_are_cleaned_and_missing_timestamps_fail_closed():
    owner = supervisor.SupervisedProcessIdentity(
        pid=100,
        created_at=10.0,
        executable="python.exe",
        command="python -m applypilot.cli apply",
        launched_at=9.0,
        ended_at=20.0,
    )
    processes = [
        {"pid": 101, "ppid": 100, "name": "python.exe", "command": "agent", "created": 11.0},
        {"pid": 102, "ppid": 101, "name": "node.exe", "command": "node playwright", "created": 12.0},
        {"pid": 103, "ppid": 100, "name": "node.exe", "command": "node @playwright/mcp", "created": None},
        {"pid": 104, "ppid": 100, "name": "node.exe", "command": "node @playwright/mcp", "created": 25.0},
    ]

    assert supervisor._associated_auxiliary_pids(processes, owner=owner) == [102]


def _cleanup_snapshots(
    *, changed: bool = False, parent_changed: bool = False, disappeared: bool = False
):
    approved = [
        {
            "pid": 101,
            "ppid": 100,
            "name": "python.exe",
            "executable": "C:/Python/python.exe",
            "command": "python -m applypilot.cli apply",
            "created": 11.0,
        },
        {
            "pid": 102,
            "ppid": 101,
            "name": "node.exe",
            "executable": "C:/Node/node.exe",
            "command": "node @playwright/mcp",
            "created": 12.0,
        },
    ]
    if disappeared:
        live = [approved[0]]
    elif parent_changed:
        live = [{**approved[0], "created": 31.0}, approved[1]]
    elif changed:
        live = [approved[0], {**approved[1], "created": 30.0, "command": "node unrelated.js"}]
    else:
        live = approved
    return iter((approved, approved, live))


def _verified_termination_recorder(terminated):
    def terminate(**identity):
        authority = identity.pop("final_authority")
        if authority() is not True:
            return False
        terminated.append(identity)
        return True

    return terminate


def test_auxiliary_identity_change_between_approval_and_kill_is_not_terminated(monkeypatch):
    ownership = _Ownership()
    snapshots = _cleanup_snapshots(changed=True)
    terminated = []
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: ownership)
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        _verified_termination_recorder(terminated),
    )

    assert supervisor._cleanup_orphans(lambda _message: None, owner=_owner()) is False
    assert terminated == []
    assert ownership.release_calls == 1


def test_auxiliary_parent_identity_change_before_kill_is_not_terminated(monkeypatch):
    ownership = _Ownership()
    snapshots = _cleanup_snapshots(parent_changed=True)
    terminated = []
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: ownership)
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        _verified_termination_recorder(terminated),
    )

    assert supervisor._cleanup_orphans(lambda _message: None, owner=_owner()) is False
    assert terminated == []
    assert ownership.release_calls == 1


def test_stable_auxiliary_identity_is_terminated_once(monkeypatch):
    ownership = _Ownership()
    snapshots = _cleanup_snapshots()
    terminated = []
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: ownership)
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        _verified_termination_recorder(terminated),
    )

    assert supervisor._cleanup_orphans(lambda _message: None, owner=_owner()) is True
    assert terminated == [
        {"pid": 102, "created_at": 12.0, "executable": "C:/Node/node.exe"}
    ]
    assert ownership.release_calls == 1


def test_auxiliary_disappearance_before_kill_is_safe_and_releases(monkeypatch):
    ownership = _Ownership()
    snapshots = _cleanup_snapshots(disappeared=True)
    monkeypatch.setattr(supervisor, "reserve_browser_cleanup", lambda *_args: ownership)
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        _verified_termination_recorder([]),
    )

    assert supervisor._cleanup_orphans(lambda _message: None, owner=_owner()) is False
    assert ownership.release_calls == 1


class _SupervisedProcess:
    pid = 501

    def __init__(self) -> None:
        self.wait_calls = 0

    def poll(self):
        return None

    def wait(self, timeout):
        self.wait_calls += 1


def _supervised_child_identity() -> supervisor.SupervisedProcessIdentity:
    return supervisor.SupervisedProcessIdentity(
        pid=501,
        created_at=12.0,
        executable="C:/Python/python.exe",
        command="python -m applypilot.cli apply --continuous",
        launched_at=11.0,
        parent_pid=50,
        parent_created_at=5.0,
        parent_executable="C:/Python/python.exe",
        parent_command="python -m applypilot.cli supervise-apply",
    )


def _supervised_live_rows(*, child_created=12.0, parent_created=5.0):
    return [
        {
            "pid": 50,
            "ppid": 1,
            "name": "python.exe",
            "executable": "C:/Python/python.exe",
            "command": "python -m applypilot.cli supervise-apply",
            "created": parent_created,
        },
        {
            "pid": 501,
            "ppid": 50,
            "name": "python.exe",
            "executable": "C:/Python/python.exe",
            "command": "python -m applypilot.cli apply --continuous",
            "created": child_created,
        },
    ]


def test_supervisor_child_identity_change_before_termination_is_refused(monkeypatch):
    process = _SupervisedProcess()
    terminated = []
    monkeypatch.setattr(supervisor.os, "getpid", lambda: 50)
    monkeypatch.setattr(
        supervisor, "_process_snapshot", lambda: _supervised_live_rows(child_created=30.0)
    )
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        lambda **identity: terminated.append(identity) or True,
    )
    monkeypatch.setattr(supervisor.time, "time", lambda: 20.0)

    assert supervisor._terminate_process(process, _supervised_child_identity()) is False
    assert terminated == []
    assert process.wait_calls == 0


def test_supervisor_parent_identity_change_before_termination_is_refused(monkeypatch):
    process = _SupervisedProcess()
    terminated = []
    monkeypatch.setattr(supervisor.os, "getpid", lambda: 50)
    monkeypatch.setattr(
        supervisor, "_process_snapshot", lambda: _supervised_live_rows(parent_created=30.0)
    )
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        lambda **identity: terminated.append(identity) or True,
    )
    monkeypatch.setattr(supervisor.time, "time", lambda: 20.0)

    assert supervisor._terminate_process(process, _supervised_child_identity()) is False
    assert terminated == []


def test_supervisor_stable_child_identity_terminates_once(monkeypatch):
    process = _SupervisedProcess()
    terminated = []
    monkeypatch.setattr(supervisor.os, "getpid", lambda: 50)
    monkeypatch.setattr(supervisor, "_process_snapshot", _supervised_live_rows)
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        _verified_termination_recorder(terminated),
    )
    monkeypatch.setattr(supervisor.time, "time", lambda: 20.0)

    assert supervisor._terminate_process(process, _supervised_child_identity()) is True
    assert terminated == [
        {"pid": 501, "created_at": 12.0, "executable": "C:/Python/python.exe"}
    ]
    assert process.wait_calls == 1


def test_supervisor_claim_transition_at_final_handle_boundary_is_refused(monkeypatch):
    process = _SupervisedProcess()
    terminated = []
    snapshots = iter((_supervised_live_rows(), _supervised_live_rows(parent_created=30.0)))
    monkeypatch.setattr(supervisor.os, "getpid", lambda: 50)
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: next(snapshots))
    monkeypatch.setattr(
        supervisor,
        "terminate_verified_process",
        _verified_termination_recorder(terminated),
    )
    monkeypatch.setattr(supervisor.time, "time", lambda: 20.0)

    assert supervisor._terminate_process(process, _supervised_child_identity()) is False
    assert terminated == []
    assert process.wait_calls == 0
