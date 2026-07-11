from __future__ import annotations

import multiprocessing
from pathlib import Path

import pytest


def _reservation_contender(lock_dir, ready, release, launch_count, results) -> None:
    import os

    os.environ["APPLYPILOT_BROWSER_LOCK_DIR"] = lock_dir
    from applypilot.apply import chrome

    try:
        reservation = chrome._acquire_browser_reservation(
            7,
            9407,
            Path(lock_dir) / "profile-7",
        )
    except chrome.BrowserSlotOccupiedError:
        results.put("occupied")
        return
    with launch_count.get_lock():
        launch_count.value += 1
    results.put("acquired")
    ready.set()
    release.wait(10)
    reservation.release()


def _reservation_owner(lock_dir, slot, port, profile, ready, release) -> None:
    import os

    os.environ["APPLYPILOT_BROWSER_LOCK_DIR"] = lock_dir
    from applypilot.apply import chrome

    reservation = chrome._acquire_browser_reservation(slot, port, Path(profile))
    ready.set()
    release.wait(10)
    reservation.release()


def test_two_processes_contend_and_only_owner_reaches_launch_path(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    launch_count = ctx.Value("i", 0)
    results = ctx.Queue()
    lock_dir = str(tmp_path / "locks")

    owner = ctx.Process(
        target=_reservation_contender,
        args=(lock_dir, ready, release, launch_count, results),
    )
    owner.start()
    assert ready.wait(10)

    loser = ctx.Process(
        target=_reservation_contender,
        args=(lock_dir, ctx.Event(), ctx.Event(), launch_count, results),
    )
    loser.start()
    loser.join(10)
    assert loser.exitcode == 0

    release.set()
    owner.join(10)
    assert owner.exitcode == 0
    assert sorted((results.get(timeout=2), results.get(timeout=2))) == ["acquired", "occupied"]
    assert launch_count.value == 1


def test_launch_reservation_failure_happens_before_zombie_kill(monkeypatch) -> None:
    from applypilot.apply import chrome

    killed = []
    monkeypatch.setattr(
        chrome,
        "_acquire_browser_reservation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(chrome.BrowserSlotOccupiedError("busy")),
    )
    monkeypatch.setattr(chrome, "_kill_on_port", lambda port: killed.append(port))

    with pytest.raises(chrome.BrowserSlotOccupiedError):
        chrome.launch_chrome(3, port=9403)

    assert killed == []


def test_refuse_occupied_port_never_kills_or_launches(tmp_path: Path, monkeypatch) -> None:
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: True)
    monkeypatch.setattr(
        chrome,
        "_kill_on_port",
        lambda port: (_ for _ in ()).throw(AssertionError("must not kill occupied port")),
    )
    monkeypatch.setattr(
        chrome.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch")),
    )

    with pytest.raises(chrome.BrowserSlotOccupiedError):
        chrome.launch_chrome(4, port=9404, kill_existing=False)

    reservation = chrome._acquire_browser_reservation(4, 9404, chrome._worker_profile_dir(4, None))
    reservation.release()


def test_unlocked_stale_reservation_files_are_recovered(tmp_path: Path, monkeypatch) -> None:
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    profile = tmp_path / "profile-5"
    reservation = chrome._acquire_browser_reservation(5, 9405, profile)
    reservation.release()
    for path in (tmp_path / "locks").glob("*.lock"):
        path.write_text('{"pid": 999999, "owner": "stale"}', encoding="utf-8")

    recovered = chrome._acquire_browser_reservation(5, 9405, profile)
    recovered.release()


def test_slot_and_port_are_each_reserved_independently(tmp_path: Path, monkeypatch) -> None:
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    owner = chrome._acquire_browser_reservation(5, 9405, tmp_path / "profile-a")
    try:
        try:
            same_slot = chrome._acquire_browser_reservation(5, 9505, tmp_path / "profile-b")
        except chrome.BrowserSlotOccupiedError:
            pass
        else:
            same_slot.release()
            pytest.fail("same numeric browser slot was not reserved")
        with pytest.raises(chrome.BrowserSlotOccupiedError):
            chrome._acquire_browser_reservation(6, 9405, tmp_path / "profile-b")
    finally:
        owner.release()


class _FakeProcess:
    def __init__(self, pid: int = 12345) -> None:
        self.pid = pid
        self.alive = True

    def poll(self):
        return None if self.alive else 0


def test_successful_cleanup_releases_process_port_and_reservation(tmp_path: Path, monkeypatch) -> None:
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("APPLYPILOT_CHROME_CLEANUP_TIMEOUT", "0")
    profile = tmp_path / "profile-6"
    reservation = chrome._acquire_browser_reservation(6, 9406, profile)
    process = _FakeProcess()
    chrome._chrome_procs[6] = process
    chrome._browser_reservations[id(process)] = reservation
    chrome._job_handles[id(process)] = chrome._OwnedJobHandle(6, process.pid, 606)
    closed = []
    monkeypatch.setattr(chrome, "_close_windows_job_handle", lambda handle: closed.append(handle) or True)
    monkeypatch.setattr(chrome, "_kill_process_tree", lambda pid: setattr(process, "alive", False))
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: False)

    assert chrome.cleanup_worker(6, process) is True
    assert closed == [606]
    assert chrome.cleanup_worker(6, process) is False
    assert closed == [606]

    reacquired = chrome._acquire_browser_reservation(6, 9406, profile)
    reacquired.release()


def test_failed_cleanup_returns_false_and_keeps_reservation(tmp_path: Path, monkeypatch) -> None:
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("APPLYPILOT_CHROME_CLEANUP_TIMEOUT", "0")
    profile = tmp_path / "profile-8"
    reservation = chrome._acquire_browser_reservation(8, 9408, profile)
    process = _FakeProcess()
    chrome._chrome_procs[8] = process
    chrome._browser_reservations[id(process)] = reservation
    monkeypatch.setattr(chrome, "_kill_process_tree", lambda pid: None)
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: True)

    assert chrome.cleanup_worker(8, process) is False
    with pytest.raises(chrome.BrowserSlotOccupiedError):
        chrome._acquire_browser_reservation(8, 9408, profile)

    process.alive = False
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: False)
    assert chrome.cleanup_worker(8, process) is True


def test_cleanup_worker_does_not_kill_foreign_process(tmp_path: Path, monkeypatch) -> None:
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("APPLYPILOT_CHROME_CLEANUP_TIMEOUT", "0")
    owner = _FakeProcess(111)
    foreign = _FakeProcess(222)
    reservation = chrome._acquire_browser_reservation(0, 9400, tmp_path / "profile-0")
    chrome._chrome_procs[0] = owner
    chrome._browser_reservations[id(owner)] = reservation
    chrome._job_handles[id(owner)] = chrome._OwnedJobHandle(0, owner.pid, 700)
    killed = []
    closed = []
    monkeypatch.setattr(chrome, "_kill_process_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(chrome, "_close_windows_job_handle", lambda handle: closed.append(handle) or True)

    assert chrome.cleanup_worker(0, foreign) is False
    assert killed == []
    assert closed == []
    assert id(owner) in chrome._job_handles

    owner.alive = False
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: False)
    assert chrome.cleanup_worker(0, owner) is True
    assert closed == [700]


def test_cleanup_worker_wrong_worker_id_leaves_owner_untouched(tmp_path: Path, monkeypatch) -> None:
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("APPLYPILOT_CHROME_CLEANUP_TIMEOUT", "0")
    owner = _FakeProcess(333)
    reservation = chrome._acquire_browser_reservation(0, 9400, tmp_path / "profile-0")
    chrome._chrome_procs[0] = owner
    chrome._browser_reservations[id(owner)] = reservation
    chrome._job_handles[id(owner)] = chrome._OwnedJobHandle(0, owner.pid, 800)
    killed = []
    closed = []
    monkeypatch.setattr(chrome, "_kill_process_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(chrome, "_close_windows_job_handle", lambda handle: closed.append(handle) or True)

    assert chrome.cleanup_worker(1, owner) is False
    assert killed == []
    assert closed == []
    assert id(owner) in chrome._job_handles

    owner.alive = False
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: False)
    assert chrome.cleanup_worker(0, owner) is True
    assert closed == [800]


def test_cleanup_worker_foreign_handle_record_leaves_process_untouched(tmp_path: Path, monkeypatch) -> None:
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    owner = _FakeProcess(444)
    reservation = chrome._acquire_browser_reservation(0, 9400, tmp_path / "profile-0")
    chrome._chrome_procs[0] = owner
    chrome._browser_reservations[id(owner)] = reservation
    chrome._job_handles[id(owner)] = chrome._OwnedJobHandle(9, owner.pid, 900)
    killed = []
    closed = []
    monkeypatch.setattr(chrome, "_kill_process_tree", lambda pid: killed.append(pid))
    monkeypatch.setattr(chrome, "_close_windows_job_handle", lambda handle: closed.append(handle) or True)

    assert chrome.cleanup_worker(0, owner) is False
    assert killed == []
    assert closed == []
    assert chrome._job_handles[id(owner)].handle == 900

    chrome._job_handles.pop(id(owner))
    chrome._chrome_procs.pop(0)
    chrome._browser_reservations.pop(id(owner))
    reservation.release()


def test_cleanup_on_exit_closes_only_successful_owned_handles(tmp_path: Path, monkeypatch) -> None:
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("APPLYPILOT_CHROME_CLEANUP_TIMEOUT", "0")
    successful = _FakeProcess(901)
    failing = _FakeProcess(902)
    successful.alive = False
    success_reservation = chrome._acquire_browser_reservation(0, 9500, tmp_path / "profile-0")
    fail_reservation = chrome._acquire_browser_reservation(1, 9501, tmp_path / "profile-1")
    monkeypatch.setattr(chrome, "_chrome_procs", {0: successful, 1: failing})
    monkeypatch.setattr(
        chrome,
        "_browser_reservations",
        {id(successful): success_reservation, id(failing): fail_reservation},
    )
    monkeypatch.setattr(
        chrome,
        "_job_handles",
        {
            id(successful): chrome._OwnedJobHandle(0, successful.pid, 9010),
            id(failing): chrome._OwnedJobHandle(1, failing.pid, 9020),
        },
    )
    closed = []
    monkeypatch.setattr(chrome, "_kill_process_tree", lambda pid: None)
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: port == 9501)
    monkeypatch.setattr(chrome, "_close_windows_job_handle", lambda handle: closed.append(handle) or True)

    chrome.cleanup_on_exit()

    assert closed == [9010]
    assert id(successful) not in chrome._job_handles
    assert chrome._job_handles[id(failing)].handle == 9020
    assert chrome._chrome_procs[1] is failing

    failing.alive = False
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: False)
    assert chrome.cleanup_worker(1, failing) is True


def test_linkedin_login_cannot_kill_another_process_port_owner(tmp_path: Path, monkeypatch) -> None:
    from applypilot import config
    from applypilot.apply import chrome

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    lock_dir = str(tmp_path / "locks")
    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", lock_dir)
    owner = ctx.Process(
        target=_reservation_owner,
        args=(
            lock_dir,
            0,
            chrome.LINKEDIN_LOGIN_CDP_PORT,
            str(tmp_path / "owner-profile"),
            ready,
            release,
        ),
    )
    owner.start()
    assert ready.wait(10)

    killed = []
    monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
    monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path / "login-profiles")
    monkeypatch.setattr(chrome, "_kill_on_port", lambda port: killed.append(port))
    monkeypatch.setattr(
        chrome.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not launch")),
    )
    try:
        with pytest.raises(chrome.BrowserSlotOccupiedError):
            chrome.linkedin_login(timeout_seconds=0)
        assert killed == []
    finally:
        release.set()
        owner.join(10)
        assert owner.exitcode == 0


def test_cleanup_on_exit_does_not_kill_or_release_another_process_owner(
    tmp_path: Path, monkeypatch
) -> None:
    from applypilot.apply import chrome

    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    release = ctx.Event()
    lock_dir = str(tmp_path / "locks")
    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", lock_dir)
    profile = tmp_path / "owner-profile"
    owner = ctx.Process(
        target=_reservation_owner,
        args=(lock_dir, 0, chrome.BASE_CDP_PORT, str(profile), ready, release),
    )
    owner.start()
    assert ready.wait(10)

    killed = []
    monkeypatch.setattr(chrome, "_chrome_procs", {})
    monkeypatch.setattr(chrome, "_browser_reservations", {})
    monkeypatch.setattr(chrome, "_kill_on_port", lambda port: killed.append(port))
    try:
        chrome.cleanup_on_exit()
        assert killed == []
        with pytest.raises(chrome.BrowserSlotOccupiedError):
            chrome._acquire_browser_reservation(0, chrome.BASE_CDP_PORT, profile)
    finally:
        release.set()
        owner.join(10)
        assert owner.exitcode == 0


def test_linkedin_login_launch_failure_releases_reservation(tmp_path: Path, monkeypatch) -> None:
    from applypilot import config
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
    monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path / "profiles")
    monkeypatch.setattr(config, "resolve_browser_path", lambda browser: "chrome.exe")
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: False)
    monkeypatch.setattr(
        chrome.subprocess,
        "Popen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("launch failed")),
    )

    with pytest.raises(OSError, match="launch failed"):
        chrome.linkedin_login(timeout_seconds=0)

    seed = config.CHROME_WORKER_DIR / chrome.SEED_PROFILE_NAME
    reservation = chrome._acquire_browser_reservation(
        chrome.LINKEDIN_LOGIN_SLOT,
        chrome.LINKEDIN_LOGIN_CDP_PORT,
        seed,
    )
    reservation.release()


def test_linkedin_login_normal_path_releases_owned_process(tmp_path: Path, monkeypatch) -> None:
    from applypilot import config
    from applypilot.apply import chrome

    process = _FakeProcess()
    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("APPLYPILOT_CHROME_CLEANUP_TIMEOUT", "0")
    monkeypatch.setattr(chrome.platform, "system", lambda: "Windows")
    monkeypatch.setattr(config, "CHROME_WORKER_DIR", tmp_path / "profiles")
    monkeypatch.setattr(config, "resolve_browser_path", lambda browser: "chrome.exe")
    monkeypatch.setattr(chrome, "_assign_kill_on_close_job", lambda worker_id, pid: None)
    monkeypatch.setattr(chrome.subprocess, "Popen", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(chrome, "has_linkedin_session", lambda profile: True)
    monkeypatch.setattr(chrome, "_close_browser_cdp", lambda port: setattr(process, "alive", False) or True)
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: False)

    ok, seed = chrome.linkedin_login(timeout_seconds=1, poll_seconds=0)

    assert ok is True
    assert seed == config.CHROME_WORKER_DIR / chrome.SEED_PROFILE_NAME
    reservation = chrome._acquire_browser_reservation(
        chrome.LINKEDIN_LOGIN_SLOT,
        chrome.LINKEDIN_LOGIN_CDP_PORT,
        seed,
    )
    reservation.release()
