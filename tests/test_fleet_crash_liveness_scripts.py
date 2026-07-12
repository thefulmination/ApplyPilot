from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_crash_liveness_runner_is_bounded_and_evidence_only() -> None:
    script = (REPO / "run-fleet-crash-liveness.ps1").read_text(encoding="utf-8")

    for text in (
        "crash-liveness",
        '"--execute"',
        "[int]$Limit = 25",
        "Start-Sleep -Seconds $Interval",
        "never\n# retries jobs",
        "cannot be negative",
        "crash-liveness.log",
        "Add-Content -LiteralPath $LogPath",
    ):
        assert text in script


def test_home_task_registration_includes_crash_liveness() -> None:
    script = (REPO / "register-fleet-tasks.ps1").read_text(encoding="utf-8")

    for text in (
        '"${TaskPrefix}CrashLiveness"',
        "run-fleet-crash-liveness.ps1",
        "crash-liveness-task",
        "New-TimeSpan -Minutes 30",
        "ExecutionTimeLimit (New-TimeSpan -Minutes 9)",
        "evidence-only",
    ):
        assert text in script


def test_isolated_crash_liveness_registration_does_not_touch_workers() -> None:
    script = (REPO / "register-fleet-crash-liveness.ps1").read_text(encoding="utf-8")

    for text in (
        "ApplyPilotFleet-CrashLiveness",
        "run-fleet-crash-liveness.ps1",
        "RunLevel Limited",
        "never retries or resolves applications",
        "$Unregister",
        "crash-liveness.log",
    ):
        assert text in script
