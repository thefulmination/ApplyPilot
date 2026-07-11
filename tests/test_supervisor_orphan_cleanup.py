from __future__ import annotations

import json
from pathlib import Path

import pytest

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
        identity.pop("direct_child", None)
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


@pytest.mark.parametrize("reason", ["budget stop", "stall restart"])
def test_supervisor_termination_failure_is_hard_fault_for_stop_and_restart(
    reason, tmp_path, monkeypatch
):
    process = _SupervisedProcess()
    logs = []
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    monkeypatch.setattr(supervisor, "_terminate_process", lambda proc, identity: False)

    with pytest.raises(RuntimeError, match="child may still be live"):
        supervisor._require_termination(process, _supervised_child_identity(), logs.append, reason)

    assert logs == [
        f"HARD-FAULT: {reason} termination could not be proven; child may still be live"
    ]


def test_supervisor_refuses_restart_when_orphan_cleanup_is_unproven(
    tmp_path, monkeypatch
):
    logs = []
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    monkeypatch.setattr(supervisor, "_cleanup_orphans", lambda log, owner=None: False)

    with pytest.raises(RuntimeError, match="refusing next launch"):
        supervisor._require_orphan_cleanup(logs.append, _owner())

    assert logs == ["HARD-FAULT: orphan cleanup could not be proven; refusing next launch"]


def test_orphan_cleanup_fault_persists_restart_interlock(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    monkeypatch.setattr(supervisor, "_cleanup_orphans", lambda log, owner=None: False)

    with pytest.raises(RuntimeError, match="refusing next launch"):
        supervisor._require_orphan_cleanup(lambda _message: None, _owner())

    marker = supervisor._hard_fault_marker()
    assert marker.exists()
    with pytest.raises(RuntimeError, match="hard-fault marker present"):
        supervisor._enforce_hard_fault_gate()

    monkeypatch.setenv("APPLYPILOT_RECONCILE_HARD_FAULT", "1")
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: [])
    monkeypatch.setattr(supervisor, "_process_exists", lambda _pid: False)
    with pytest.raises(RuntimeError, match="marker retained"):
        supervisor._enforce_hard_fault_gate()
    assert marker.exists()


class _Guard:
    def __init__(self, cleanup: bool):
        self.cleanup = cleanup
        self.terminate_calls = 0
        self.release_calls = 0

    def terminate_and_reap(self):
        self.terminate_calls += 1
        return self.cleanup

    def release(self):
        self.release_calls += 1


def test_guarded_supervisor_capture_none_reaps_and_does_not_mark_when_proven(
    tmp_path, monkeypatch
):
    process = _SupervisedProcess()
    guard = _Guard(True)
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    monkeypatch.setattr(supervisor.SpawnedChildGuard, "acquire", lambda proc: guard)
    monkeypatch.setattr(supervisor, "_capture_supervised_identity", lambda pid, launched: None)

    with pytest.raises(RuntimeError, match="identity capture failed"):
        supervisor._capture_guarded_supervised_child(process, 10.0)

    assert guard.terminate_calls == 1
    assert not supervisor._hard_fault_marker().exists()


def test_guarded_supervisor_capture_exception_marks_when_cleanup_uncertain(
    tmp_path, monkeypatch
):
    process = _SupervisedProcess()
    guard = _Guard(False)
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    monkeypatch.setattr(supervisor.SpawnedChildGuard, "acquire", lambda proc: guard)
    monkeypatch.setattr(
        supervisor,
        "_capture_supervised_identity",
        lambda pid, launched: (_ for _ in ()).throw(OSError("capture failed")),
    )

    with pytest.raises(RuntimeError, match="identity capture raised"):
        supervisor._capture_guarded_supervised_child(process, 10.0)

    assert guard.terminate_calls == 1
    assert supervisor._hard_fault_marker().exists()


@pytest.mark.parametrize("cleanup_proven", [True, False])
def test_supervisor_guard_acquisition_failure_uses_direct_child_emergency_cleanup(
    cleanup_proven, tmp_path, monkeypatch
):
    process = _SupervisedProcess()
    cleanup_calls = []
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    monkeypatch.setattr(supervisor.SpawnedChildGuard, "acquire", lambda _proc: None)
    monkeypatch.setattr(
        supervisor,
        "emergency_cleanup_direct_child",
        lambda proc: cleanup_calls.append(proc) or cleanup_proven,
    )

    with pytest.raises(RuntimeError, match="stable spawn guard"):
        supervisor._capture_guarded_supervised_child(process, 10.0)

    assert cleanup_calls == [process]
    assert supervisor._hard_fault_marker().exists() is (not cleanup_proven)


def test_hard_fault_marker_is_atomic_and_privacy_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    marker = supervisor._persist_hard_fault("stall timeout", _supervised_child_identity())
    payload = json.loads(marker.read_text(encoding="utf-8"))

    assert payload["pid"] == 501
    assert payload["reason"] == "stall timeout"
    assert payload["executable_name"] == "python.exe"
    assert payload["executable_sha256"].startswith("sha256:")
    assert payload["command_sha256"].startswith("sha256:")
    assert "command" not in payload
    assert "C:/Python" not in marker.read_text(encoding="utf-8")
    assert list(tmp_path.glob("*.tmp")) == []


def test_hard_fault_gate_refuses_without_explicit_reconciliation(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    marker = supervisor._persist_hard_fault("uncertain", _supervised_child_identity())

    with pytest.raises(RuntimeError, match="hard-fault marker present"):
        supervisor._enforce_hard_fault_gate()

    assert marker.exists()


@pytest.mark.parametrize("exists", [True, None])
def test_identityless_marker_retained_when_pid_live_or_uncertain(
    exists, tmp_path, monkeypatch
):
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    marker = supervisor._persist_hard_fault("guard unavailable", pid=501)
    monkeypatch.setenv("APPLYPILOT_RECONCILE_HARD_FAULT", "1")
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: [])
    monkeypatch.setattr(supervisor, "_process_exists", lambda _pid: exists)

    with pytest.raises(RuntimeError, match="marker retained"):
        supervisor._enforce_hard_fault_gate()

    assert marker.exists()


def test_identityless_marker_clears_only_when_pid_definitively_absent(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    marker = supervisor._persist_hard_fault("guard unavailable", pid=501)
    monkeypatch.setenv("APPLYPILOT_RECONCILE_HARD_FAULT", "1")
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: [])
    monkeypatch.setattr(supervisor, "_process_exists", lambda _pid: False)

    supervisor._enforce_hard_fault_gate()

    assert not marker.exists()


def test_identityless_marker_retained_when_pid_status_query_raises(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    marker = supervisor._persist_hard_fault("guard unavailable", pid=501)
    monkeypatch.setenv("APPLYPILOT_RECONCILE_HARD_FAULT", "1")
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: [])
    monkeypatch.setattr(
        supervisor,
        "_process_exists",
        lambda _pid: (_ for _ in ()).throw(PermissionError("inaccessible")),
    )

    with pytest.raises(RuntimeError, match="marker retained"):
        supervisor._enforce_hard_fault_gate()

    assert marker.exists()


@pytest.mark.parametrize(
    "current",
    [
        {"pid": 501, "created": None, "executable": "", "command": ""},
        {
            "pid": 501,
            "created": 12.0,
            "executable": "C:/Different/python.exe",
            "command": "query unavailable",
        },
    ],
)
def test_full_marker_retains_on_incomplete_or_same_start_identity_uncertainty(
    current, tmp_path, monkeypatch
):
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    marker = supervisor._persist_hard_fault("uncertain", _supervised_child_identity())
    monkeypatch.setenv("APPLYPILOT_RECONCILE_HARD_FAULT", "1")
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: [current])

    with pytest.raises(RuntimeError, match="marker retained"):
        supervisor._enforce_hard_fault_gate()

    assert marker.exists()


def test_full_marker_clears_when_creation_identity_proves_pid_reuse(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    marker = supervisor._persist_hard_fault("uncertain", _supervised_child_identity())
    monkeypatch.setenv("APPLYPILOT_RECONCILE_HARD_FAULT", "1")
    monkeypatch.setattr(
        supervisor,
        "_process_snapshot",
        lambda: [
            {
                "pid": 501,
                "created": 99.0,
                "executable": "C:/Python/python.exe",
                "command": "python -m applypilot.cli apply --continuous",
            }
        ],
    )

    supervisor._enforce_hard_fault_gate()

    assert not marker.exists()


def test_explicit_reconciliation_clears_only_proven_gone_child(tmp_path, monkeypatch):
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    marker = supervisor._persist_hard_fault("uncertain", _supervised_child_identity())
    monkeypatch.setenv("APPLYPILOT_RECONCILE_HARD_FAULT", "1")
    monkeypatch.setattr(supervisor, "_process_snapshot", lambda: [])
    monkeypatch.setattr(supervisor, "_process_exists", lambda pid: False)

    supervisor._enforce_hard_fault_gate()

    assert not marker.exists()


@pytest.mark.parametrize("termination_proven", [True, False])
def test_explicit_reconciliation_clears_exact_live_child_only_after_proven_termination(
    termination_proven, tmp_path, monkeypatch
):
    monkeypatch.setattr(supervisor.config, "DB_PATH", tmp_path / "applypilot.db")
    marker = supervisor._persist_hard_fault("uncertain", _supervised_child_identity())
    monkeypatch.setenv("APPLYPILOT_RECONCILE_HARD_FAULT", "1")
    monkeypatch.setattr(supervisor, "_process_snapshot", _supervised_live_rows)
    calls = []

    def terminate(**claim):
        calls.append(claim["pid"])
        assert claim["final_authority"]() is True
        return termination_proven

    monkeypatch.setattr(supervisor, "terminate_verified_process", terminate)

    if termination_proven:
        supervisor._enforce_hard_fault_gate()
        assert not marker.exists()
    else:
        with pytest.raises(RuntimeError, match="marker retained"):
            supervisor._enforce_hard_fault_gate()
        assert marker.exists()
    assert calls == [501]


def test_darwin_executable_capture_uses_libproc(monkeypatch):
    import ctypes

    class ProcPidPath:
        def __call__(self, pid, buffer, size):
            value = b"/Applications/Python.app/Contents/MacOS/Python"
            ctypes.memmove(buffer, value + b"\0", len(value) + 1)
            return len(value)

    class LibProc:
        proc_pidpath = ProcPidPath()

    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: LibProc())

    assert supervisor._process_executable(123).endswith("/MacOS/Python")


def test_darwin_executable_capture_fails_closed_without_proc_pidpath(monkeypatch):
    import ctypes

    monkeypatch.setattr(supervisor.sys, "platform", "darwin")
    monkeypatch.setattr(ctypes, "CDLL", lambda *_args, **_kwargs: object())

    assert supervisor._process_executable(123) == ""
