from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_daily_unguard_requires_explicit_force_and_scheduled_wrapper_is_safe():
    script = (ROOT / "fleet-daily-unguard.ps1").read_text(encoding="utf-8")
    wrapper = (ROOT / ".fleet-logs" / "_task-wrappers" / "applypilot-daily-unguard-wrapper.ps1").read_text(
        encoding="utf-8"
    )

    assert "[switch]$Force" in script
    assert "if (-not $Force)" in script
    assert "explicit -Force is required" in script
    assert "-Force" not in wrapper
