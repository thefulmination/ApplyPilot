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
    chrome._browser_reservations[id(process)] = reservation
    monkeypatch.setattr(chrome, "_kill_process_tree", lambda pid: setattr(process, "alive", False))
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: False)

    assert chrome.cleanup_worker(6, process) is True

    reacquired = chrome._acquire_browser_reservation(6, 9406, profile)
    reacquired.release()


def test_failed_cleanup_returns_false_and_keeps_reservation(tmp_path: Path, monkeypatch) -> None:
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("APPLYPILOT_CHROME_CLEANUP_TIMEOUT", "0")
    profile = tmp_path / "profile-8"
    reservation = chrome._acquire_browser_reservation(8, 9408, profile)
    process = _FakeProcess()
    chrome._browser_reservations[id(process)] = reservation
    monkeypatch.setattr(chrome, "_kill_process_tree", lambda pid: None)
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: True)

    assert chrome.cleanup_worker(8, process) is False
    with pytest.raises(chrome.BrowserSlotOccupiedError):
        chrome._acquire_browser_reservation(8, 9408, profile)

    process.alive = False
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: False)
    assert chrome.cleanup_worker(8, process) is True
