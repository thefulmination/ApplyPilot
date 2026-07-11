from pathlib import Path


def test_keepalive_refuses_hard_fault_relaunch_without_explicit_reconciliation():
    script = (Path(__file__).parents[1] / "keepalive-apply.ps1").read_text(encoding="utf-8")

    assert "keepalive.hard-fault.json" in script
    assert "lifecycle-faults" in script
    assert "fault-*.json" in script
    assert 'if ($env:APPLYPILOT_RECONCILE_HARD_FAULT -ne "1")' in script
    assert "refusing supervisor relaunch" in script
    assert "exit 3" in script
    assert script.index("if ($hardFaultFiles.Count -gt 0)") < script.index(
        'Log "LAUNCH supervisor'
    )
    assert "Remove-Item $hardFaultMarker" not in script
