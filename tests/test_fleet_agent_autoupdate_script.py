from pathlib import Path
import shutil
import subprocess


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

    assert '$RecoveryOnly -and [string]::IsNullOrWhiteSpace($pinnedVersion)' in script
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

    pwsh = shutil.which("pwsh")
    if not pwsh:
        return
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

    result = subprocess.run(
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(wrapper_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)


def test_worker_spawn_and_respawn_are_blocked_by_lifecycle_faults() -> None:
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")

    assert "function Assert-NoLifecycleFaults" in script
    assert "keepalive.hard-fault.json" in script
    assert "lifecycle-faults" in script
    assert 'Get-ChildItem -LiteralPath $faultDir -Filter "fault-*.json"' in script
    assert "$env:TEMP" in script
    assert script.count("Assert-NoLifecycleFaults") >= 3
    assert "operator reconciliation" in script
