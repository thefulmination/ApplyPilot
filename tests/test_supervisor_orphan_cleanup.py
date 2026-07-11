"""The orphan Playwright-MCP cleanup must never invoke PowerShell on POSIX (macOS fleet
worker) and must keep the existing PowerShell path on Windows."""
import sys
from pathlib import Path

from applypilot.apply import supervisor


def test_orphan_kill_cmd_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cmd = supervisor._orphan_kill_cmd()
    assert cmd[0] == "powershell"
    assert "_npx|playwright|modelcontextprotocol|@playwright" in " ".join(cmd)


def test_orphan_kill_cmd_posix_uses_pkill(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    cmd = supervisor._orphan_kill_cmd()
    assert cmd[0] == "pkill" and cmd[1] == "-f"
    # node-scoped, mirroring the Windows Name='node.exe' pre-filter
    assert cmd[2] == "(^|/)node .*(_npx|playwright|modelcontextprotocol|@playwright)"
    assert "powershell" not in " ".join(cmd)


def test_cleanup_orphans_uses_public_ownership_aware_browser_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr(
        supervisor,
        "cleanup_orphaned_browser",
        lambda worker_id, port, profile_dir: calls.append((worker_id, port, profile_dir)) or False,
    )
    monkeypatch.setattr(supervisor.subprocess, "run", lambda *args, **kwargs: None)

    supervisor._cleanup_orphans(lambda message: None)

    assert calls == [
        (0, supervisor.BASE_CDP_PORT, supervisor.config.CHROME_WORKER_DIR / "worker-0")
    ]


def test_orphan_cleanup_kills_only_after_reservation_and_releases(tmp_path: Path, monkeypatch):
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    profile = tmp_path / "profile-0"
    listening = {chrome.BASE_CDP_PORT: True}
    killed = []
    monkeypatch.setattr(chrome, "_port_is_listening", lambda port: listening.get(port, False))

    def kill(port):
        killed.append(port)
        listening[port] = False

    monkeypatch.setattr(chrome, "_kill_on_port", kill)

    assert chrome.cleanup_orphaned_browser(0, chrome.BASE_CDP_PORT, profile) is True
    assert killed == [chrome.BASE_CDP_PORT]
    reservation = chrome._acquire_browser_reservation(0, chrome.BASE_CDP_PORT, profile)
    reservation.release()


def test_orphan_cleanup_contender_fails_closed_without_kill(tmp_path: Path, monkeypatch):
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    profile = tmp_path / "profile-0"
    owner = chrome._acquire_browser_reservation(0, chrome.BASE_CDP_PORT, profile)
    killed = []
    monkeypatch.setattr(chrome, "_kill_on_port", lambda port: killed.append(port))
    try:
        assert chrome.cleanup_orphaned_browser(0, chrome.BASE_CDP_PORT, profile) is False
        assert killed == []
    finally:
        owner.release()


def test_orphan_cleanup_failure_releases_reservation(tmp_path: Path, monkeypatch):
    from applypilot.apply import chrome

    monkeypatch.setenv("APPLYPILOT_BROWSER_LOCK_DIR", str(tmp_path / "locks"))
    profile = tmp_path / "profile-0"
    monkeypatch.setattr(
        chrome,
        "_kill_on_port",
        lambda port: (_ for _ in ()).throw(RuntimeError("kill failed")),
    )

    assert chrome.cleanup_orphaned_browser(0, chrome.BASE_CDP_PORT, profile) is False
    reservation = chrome._acquire_browser_reservation(0, chrome.BASE_CDP_PORT, profile)
    reservation.release()
