from pathlib import Path


def test_fleet_console_launcher_uses_invocation_checkout():
    script = Path("run-fleet-console.ps1").read_text(encoding="utf-8")

    assert '$RepoRoot  = "C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot"' not in script
    assert "$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path" in script
    assert '$env:PYTHONPATH = Join-Path $RepoRoot "src"' in script
