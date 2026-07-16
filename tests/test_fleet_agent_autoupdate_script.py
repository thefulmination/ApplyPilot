from pathlib import Path
import shutil
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]


def _powershell_function(script: str, name: str) -> str:
    start = script.index(f"function {name}")
    opening = script.index("{", start)
    depth = 0
    for index in range(opening, len(script)):
        if script[index] == "{":
            depth += 1
        elif script[index] == "}":
            depth -= 1
            if depth == 0:
                return script[start : index + 1]
    raise AssertionError(f"unterminated PowerShell function {name}")


def _production_powershells() -> list[str]:
    windows_powershell = shutil.which("powershell.exe")
    assert windows_powershell, "Windows PowerShell 5.1 is required for production shell tests"
    shells = [windows_powershell]
    pwsh = shutil.which("pwsh")
    if pwsh:
        shells.append(pwsh)
    return shells


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

    assert "[string]::IsNullOrWhiteSpace($vf[2])" in script
    assert '$RecoveryOnly -and [string]::IsNullOrWhiteSpace($pinnedVersion)' not in script
    assert 'git rev-parse "$remote/$branch"' in script
    assert 'git rev-parse "$target^{tree}"' in script
    assert 'git diff --name-only $local $target' in script
    assert 'git merge --ff-only --quiet $target' in script
    assert "$mergedHead -cne $target" in script


def test_fleet_agent_production_task_wrapper_propagates_exit_code(tmp_path) -> None:
    script = (REPO / "register-fleet-tasks.ps1").read_text(encoding="utf-8")
    start = script.index('$agentWrapperContent = @"')
    end = script.index('\n"@', start)
    wrapper = script[script.index("\n", start) + 1 : end]

    invocation = "& '$fleetAgentPs1' -Label $Machine$($agentAutoUpdate)"
    assert invocation in wrapper
    assert wrapper.index(invocation) < wrapper.index("exit `$LASTEXITCODE")

    child = tmp_path / "fleet-agent.ps1"
    child.write_text("exit 1\n", encoding="utf-8")
    rendered = (
        wrapper.replace("`$ErrorActionPreference", "$ErrorActionPreference")
        .replace("`$env", "$env")
        .replace("`$LASTEXITCODE", "$LASTEXITCODE")
        .replace("'$effectiveDsn'", "'unused'")
        .replace("'$repo'", f"'{tmp_path}'")
        .replace("'$fleetAgentPs1'", f"'{child}'")
        .replace("$Machine$($agentAutoUpdate)", "m4")
    )
    wrapper_path = tmp_path / "fleet-agent-task.ps1"
    wrapper_path.write_text(rendered, encoding="utf-8")

    for shell in _production_powershells():
        result = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper_path)],
            capture_output=True,
            text=True,
            timeout=30,
        )

        assert result.returncode == 1, (shell, result.stdout, result.stderr)


def test_production_fleet_scripts_parse_under_supported_powershells() -> None:
    for shell in _production_powershells():
        for path in (REPO / "fleet-agent.ps1", REPO / "register-fleet-tasks.ps1"):
            quoted_path = str(path).replace("'", "''")
            parser = (
                "$errors = $null; $tokens = $null; "
                "$null = [System.Management.Automation.Language.Parser]::ParseFile("
                f"'{quoted_path}', [ref]$tokens, [ref]$errors); "
                "if ($errors.Count) { $errors | ForEach-Object { Write-Error $_ }; exit 1 }"
            )
            result = subprocess.run(
                [shell, "-NoProfile", "-Command", parser],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, (shell, path, result.stdout, result.stderr)


def test_restart_marker_controls_post_merge_worker_restart() -> None:
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")

    assert "$agentStartHead" in script
    assert "$updateRestartMarker" in script
    assert "Write-UpdateRestartMarker $target" in script
    assert "Complete-PendingUpdateRestart" in script
    assert "pyproject.toml changed; automatic update deferred" in script

    merge = script.index("git merge --ff-only --quiet $target")
    post_policy = script.index("Get-MachineBlackoutStatus \"all\"", merge)
    completion = script.index("Complete-PendingUpdateRestart", post_policy)
    stop = script.index("Stop-Process", completion)
    assert merge < post_policy < completion < stop


@pytest.mark.parametrize(
    ("wrapper", "accepted"),
    [
        (
            "& 'C:\\ApplyPilot\\fleet-agent.ps1' -Label m4 -AutoUpdate\n",
            False,
        ),
        (
            "& 'C:\\ApplyPilot\\fleet-agent.ps1' -Label m4 -AutoUpdate\n"
            "Write-Host 'intervening command'\n"
            "exit $LASTEXITCODE\n",
            False,
        ),
        (
            "& 'C:\\ApplyPilot\\fleet-agent.ps1' -Label m4 -AutoUpdate\nexit $LASTEXITCODE\n",
            True,
        ),
    ],
    ids=["old-wrapper", "ambiguous-wrapper", "corrected-wrapper"],
)
def test_installed_fleet_agent_wrapper_guard(tmp_path, wrapper: str, accepted: bool) -> None:
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    guard = _powershell_function(script, "Test-InstalledAgentWrapper")
    wrapper_dir = tmp_path / ".fleet-logs" / "_task-wrappers"
    wrapper_dir.mkdir(parents=True)
    installed_wrapper = wrapper.replace("C:\\ApplyPilot", str(tmp_path))
    (wrapper_dir / "fleet-agent-task.ps1").write_text(installed_wrapper, encoding="utf-8")
    harness = tmp_path / "wrapper-guard-harness.ps1"
    quoted_tmp = str(tmp_path).replace("'", "''")
    harness.write_text(
        f"$repo = '{quoted_tmp}'\n"
        "function Log-Update { param([string]$msg, [string]$color = 'Gray') }\n"
        f"{guard}\n"
        "Write-Output (Test-InstalledAgentWrapper)\n",
        encoding="utf-8",
    )

    for shell in _production_powershells():
        result = subprocess.run(
            [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (shell, result.stdout, result.stderr)
        assert result.stdout.strip() == str(accepted), (shell, result.stdout, result.stderr)


def test_recovery_allowlist_is_narrow_but_can_deliver_task_registration_fix() -> None:
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    start = script.index("$recoveryFiles = @")
    end = script.index("$script:lastUpdateCheck", start)
    allowlist = script[start:end]

    assert '"register-fleet-tasks.ps1"' in allowlist
    assert '"src/applypilot/fleet/apply_worker_main.py"' not in allowlist


def test_worker_spawn_and_respawn_are_blocked_by_lifecycle_faults() -> None:
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")

    assert "function Assert-NoLifecycleFaults" in script
    assert "keepalive.hard-fault.json" in script
    assert "lifecycle-faults" in script
    assert 'Get-ChildItem -LiteralPath $faultDir -Filter "fault-*.json"' in script
    assert "$env:TEMP" in script
    assert script.count("Assert-NoLifecycleFaults") >= 3
    assert "operator reconciliation" in script
