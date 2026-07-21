from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_fleet_agent_autoupdate_checks_pin_before_fast_forward() -> None:
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")

    for text in (
        "fleet-agent-version.py",
        "pinned_worker_version",
        "$pinnedVersion",
        "$targetVersion",
        "git rev-parse",
        "^{tree}",
        "remote tree $targetVersion is not pinned",
        "UPDATE BLOCKED: pinned version",
    ):
        assert text in script


def test_worker_spawn_and_respawn_are_blocked_by_lifecycle_faults() -> None:
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")

    assert "function Assert-NoLifecycleFaults" in script
    assert "keepalive.hard-fault.json" in script
    assert "lifecycle-faults" in script
    assert 'Get-ChildItem -LiteralPath $faultDir -Filter "fault-*.json"' in script
    assert "$env:TEMP" in script
    assert script.count("Assert-NoLifecycleFaults") >= 3
    assert "operator reconciliation" in script
