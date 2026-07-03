"""The orphan Playwright-MCP cleanup must never invoke PowerShell on POSIX (macOS fleet
worker) and must keep the existing PowerShell path on Windows."""
import sys

from applypilot.apply import supervisor


def test_orphan_kill_cmd_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cmd = supervisor._orphan_kill_cmd()
    assert cmd[0] == "powershell"
    assert "_npx|playwright|modelcontextprotocol|@playwright" in " ".join(cmd)


def test_orphan_kill_cmd_posix_uses_pkill(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    cmd = supervisor._orphan_kill_cmd()
    assert cmd == ["pkill", "-f", "_npx|playwright|modelcontextprotocol|@playwright"]
    assert "powershell" not in " ".join(cmd)
