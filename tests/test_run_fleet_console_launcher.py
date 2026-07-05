from pathlib import Path


def test_fleet_console_launcher_uses_invocation_checkout():
    script = Path("run-fleet-console.ps1").read_text(encoding="utf-8")

    assert '$RepoRoot  = "C:/Users/JStal/OneDrive/Documents/New project/ApplyPilot"' not in script
    assert "$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path" in script
    assert '$env:PYTHONPATH = Join-Path $RepoRoot "src"' in script


def test_fleet_console_launcher_prints_source_branch_and_commit():
    script = Path("run-fleet-console.ps1").read_text(encoding="utf-8")

    assert "Source checkout:" in script
    assert "git rev-parse --abbrev-ref HEAD" in script
    assert "git rev-parse --short HEAD" in script
