import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[1]


def _production_shells() -> list[str]:
    windows_powershell = shutil.which("powershell.exe")
    assert windows_powershell, "Windows PowerShell 5.1 is required"
    shells = [windows_powershell]
    pwsh = shutil.which("pwsh")
    if pwsh:
        shells.append(pwsh)
    return shells


def _supported_powershells() -> list[str]:
    return _production_shells()


def _primary_production_shell() -> str:
    return _production_shells()[0]


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


def _ps_quote(value) -> str:
    return str(value).replace("'", "''")


def _install_corrected_agent_wrapper(repo: Path) -> None:
    wrapper_dir = repo / ".fleet-logs" / "_task-wrappers"
    wrapper_dir.mkdir(parents=True, exist_ok=True)
    (wrapper_dir / "fleet-agent-task.ps1").write_text(
        f"& '{_ps_quote(repo / 'fleet-agent.ps1')}' -Label m4 -AutoUpdate\nexit $LASTEXITCODE\n",
        encoding="utf-8",
    )


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
        text=True,
    )
    return result.stdout.strip()


def _recovery_update_fixture(tmp_path: Path, changed_path: str) -> tuple[Path, str]:
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    worker = tmp_path / "worker"
    seed.mkdir()
    _git(seed, "init", "--initial-branch=main")
    _git(seed, "config", "user.email", "fleet-test@example.com")
    _git(seed, "config", "user.name", "Fleet Test")
    (seed / "fleet-agent-version.py").write_text(
        "import os\n"
        "from pathlib import Path\n"
        "counter = Path(os.environ['VERSION_COUNT_FILE'])\n"
        "count = int(counter.read_text()) if counter.exists() else 0\n"
        "counter.write_text(str(count + 1))\n"
        "if count and int(os.environ.get('SECOND_VERSION_EXIT', '0')):\n"
        "    raise SystemExit(int(os.environ['SECOND_VERSION_EXIT']))\n"
        "key = 'VERSION_LINE' if count == 0 else 'SECOND_VERSION_LINE'\n"
        "print(os.environ[key])\n",
        encoding="utf-8",
    )
    (seed / "fleet-blackout-query.py").write_text("old\n", encoding="utf-8")
    (seed / ".gitignore").write_text(".fleet-logs/\n", encoding="utf-8")
    _git(seed, "add", ".")
    _git(seed, "commit", "-m", "initial")
    _git(tmp_path, "init", "--bare", "--initial-branch=main", str(remote))
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-u", "origin", "main")
    _git(tmp_path, "clone", str(remote), str(worker))

    changed_paths = ["fleet-blackout-query.py"]
    if changed_path not in changed_paths:
        changed_paths.append(changed_path)
    for path in changed_paths:
        target_file = seed / path
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("recovery\n", encoding="utf-8")
    _git(seed, "add", *changed_paths)
    _git(seed, "commit", "-m", "target")
    _git(seed, "push", "origin", "main")
    target_tree = _git(seed, "rev-parse", "HEAD^{tree}")
    return worker, f"0.3.0+git.tree.{target_tree[:7]}"


def _run_recovery_update(
    tmp_path: Path,
    shell: str,
    changed_path: str,
    *,
    pinned_version: str | None = None,
    repinned_version: str | None = None,
    second_version_exit: int = 0,
) -> tuple[subprocess.CompletedProcess, Path]:
    worker, target_version = _recovery_update_fixture(tmp_path, changed_path)
    _install_corrected_agent_wrapper(worker)
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    functions = "\n".join(
        _powershell_function(script, name)
        for name in (
            "Get-PinnedWorkerVersion",
            "Confirm-PinnedWorkerVersion",
            "Test-InstalledAgentWrapper",
            "Invoke-AutoUpdate",
        )
    )
    recovery_start = script.index("$recoveryFiles = @")
    recovery_end = script.index("\n$script:lastUpdateCheck", recovery_start)
    recovery_declaration = script[recovery_start:recovery_end]
    harness = tmp_path / "recovery-update-harness.ps1"
    mutation_marker = tmp_path / "worker-mutation.txt"
    harness.write_text(
        f"Set-Location '{_ps_quote(worker)}'\n"
        f"$repo = '{_ps_quote(worker)}'\n"
        "$Label = 'm4'\n"
        f"$py = '{_ps_quote(sys.executable)}'\n"
        f"$updateRestartMarker = '{_ps_quote(tmp_path / 'recovery-restart-marker.sha')}'\n"
        "$UpdateEverySec = 0\n"
        "$script:lastUpdateCheck = [datetime]::MinValue\n"
        "$script:updatePending = $false\n"
        "$script:updBranch = $null\n"
        "$script:updRemote = $null\n"
        f"{recovery_declaration}\n"
        "function Log-Update { param([string]$msg, [string]$color = 'Gray') }\n"
        "function Get-ShortSha([string]$sha) { $sha }\n"
        f"function Get-LocalWorkers {{ Add-Content '{_ps_quote(mutation_marker)}' 'workers'; @() }}\n"
        f"{functions}\n"
        "Invoke-AutoUpdate -RecoveryOnly | Out-Null\n"
        "exit 0\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    effective_pin = target_version if pinned_version is None else pinned_version
    env["VERSION_LINE"] = f"OK|0.3.0+git.tree.0000000|{effective_pin}|"
    effective_repin = effective_pin if repinned_version is None else repinned_version
    env["SECOND_VERSION_LINE"] = f"OK|0.3.0+git.tree.0000000|{effective_repin}|"
    env["SECOND_VERSION_EXIT"] = str(second_version_exit)
    env["VERSION_COUNT_FILE"] = str(tmp_path / "version-count.txt")
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert not mutation_marker.exists(), (result.stdout, result.stderr)
    return result, worker


def _run_update_harness(
    tmp_path: Path,
    shell: str,
    *,
    pinned_version: str = "0.3.0+git.tree.ccccccc",
    repinned_version: str | None = None,
    second_version_exit: int = 0,
    fresh_policy_state: str = "OK",
    fresh_policy_line: str = "OK|m4|all|||",
    fail_git_command: str = "",
) -> tuple[subprocess.CompletedProcess, Path]:
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    functions = "\n".join(
        _powershell_function(script, name)
        for name in (
            "Get-PinnedWorkerVersion",
            "Confirm-PinnedWorkerVersion",
            "Test-InstalledAgentWrapper",
            "Get-UpdateRestartTarget",
            "Write-UpdateRestartMarker",
            "Clear-UpdateRestartMarker",
            "Clear-UnappliedUpdateRestartMarker",
            "Complete-PendingUpdateRestart",
            "Invoke-AutoUpdate",
        )
    )
    declarations_start = script.index("$recoveryFiles = @")
    declarations_end = script.index("\n$script:lastUpdateCheck", declarations_start)
    declarations = script[declarations_start:declarations_end]
    mutation_marker = tmp_path / "worker-mutation.txt"
    py_shim = tmp_path / "python-shim.ps1"
    py_shim.write_text(
        "param([string]$ScriptName)\n"
        "if ($ScriptName -eq 'fleet-agent-version.py') {\n"
        "  $count = if (Test-Path -LiteralPath $env:VERSION_COUNT_FILE) { "
        "[int](Get-Content -LiteralPath $env:VERSION_COUNT_FILE) } else { 0 }\n"
        "  Set-Content -LiteralPath $env:VERSION_COUNT_FILE -Value ($count + 1)\n"
        "  if ($count -gt 0 -and [int]$env:SECOND_VERSION_EXIT -ne 0) { "
        "exit ([int]$env:SECOND_VERSION_EXIT) }\n"
        "  if ($count -eq 0) { Write-Output $env:VERSION_LINE } "
        "else { Write-Output $env:SECOND_VERSION_LINE }\n"
        "  exit 0\n"
        "}\n"
        "if ($ScriptName -eq 'fleet-agent-update-gate.py') { Write-Output 'IDLE'; exit 0 }\n"
        "exit 1\n",
        encoding="utf-8",
    )
    harness = tmp_path / "update-harness.ps1"
    _install_corrected_agent_wrapper(tmp_path)
    harness.write_text(
        f"$repo = '{_ps_quote(tmp_path)}'\n"
        "$Label = 'm4'\n"
        f"$py = '{_ps_quote(py_shim)}'\n"
        f"$updateRestartMarker = '{_ps_quote(tmp_path / 'missing-restart-marker.sha')}'\n"
        "$UpdateEverySec = 0\n"
        "$script:lastUpdateCheck = [datetime]::MinValue\n"
        "$script:updatePending = $false\n"
        "$script:updBranch = $null\n"
        "$script:updRemote = $null\n"
        f"{declarations}\n"
        "function Log-Update { param([string]$msg, [string]$color = 'Gray') }\n"
        "function Get-ShortSha([string]$sha) { $sha }\n"
        "function git {\n"
        "  param([Parameter(ValueFromRemainingArguments=$true)][string[]]$GitArgs)\n"
        "  $key = $GitArgs -join ' '\n"
        "  $global:LASTEXITCODE = if ($key -eq $env:FAIL_GIT_COMMAND) { 17 } else { 0 }\n"
        "  if ($global:LASTEXITCODE -ne 0) { return }\n"
        "  switch ($key) {\n"
        "    'rev-parse --abbrev-ref HEAD' { 'main' }\n"
        "    'config branch.main.remote' { 'origin' }\n"
        "    'rev-parse origin/main' { 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' }\n"
        "    'rev-parse bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb^{tree}' { 'cccccccccccccccccccccccccccccccccccccccc' }\n"
        "    'rev-parse HEAD' { 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa' }\n"
        "    'diff --name-only aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb' { 'fleet-blackout-query.py' }\n"
        "  }\n"
        "}\n"
        "function Get-MachineBlackoutStatus {\n"
        f"  [pscustomobject]@{{ State = '{fresh_policy_state}'; Line = '{fresh_policy_line}' }}\n"
        "}\n"
        f"function Get-LocalWorkers {{ Add-Content '{_ps_quote(mutation_marker)}' 'workers'; @() }}\n"
        "function Slot-Of($proc) { 0 }\n"
        "function Start-Sleep { param([int]$Seconds) }\n"
        "function Stop-Process { param($Id, [switch]$Force, $ErrorAction) }\n"
        f"{functions}\n"
        "$expected = [pscustomobject]@{ State = 'OK'; Line = 'OK|m4|all|||' }\n"
        "$result = Invoke-AutoUpdate -ExpectedMachinePolicy $expected\n"
        "Write-Output \"result=$result\"\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["VERSION_LINE"] = f"OK|0.3.0+git.tree.aaaaaaa|{pinned_version}|"
    effective_repin = pinned_version if repinned_version is None else repinned_version
    env["SECOND_VERSION_LINE"] = f"OK|0.3.0+git.tree.aaaaaaa|{effective_repin}|"
    env["SECOND_VERSION_EXIT"] = str(second_version_exit)
    env["VERSION_COUNT_FILE"] = str(tmp_path / "version-count.txt")
    env["FAIL_GIT_COMMAND"] = fail_git_command
    result = subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    return result, mutation_marker


def _run_restart_sequence_harness(
    tmp_path: Path,
    shell: str,
    *,
    post_merge_state: str = "KEEP",
    complete_later: bool = False,
    restart_surviving_marker: bool = False,
    fail_merge: bool = False,
    post_merge_head: str = "target",
    gate: str = "IDLE",
) -> subprocess.CompletedProcess:
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    functions = "\n".join(
        _powershell_function(script, name)
        for name in (
            "Get-PinnedWorkerVersion",
            "Confirm-PinnedWorkerVersion",
            "Test-InstalledAgentWrapper",
            "Get-UpdateRestartTarget",
            "Write-UpdateRestartMarker",
            "Clear-UpdateRestartMarker",
            "Clear-UnappliedUpdateRestartMarker",
            "Complete-PendingUpdateRestart",
            "Invoke-AutoUpdate",
        )
    )
    declarations_start = script.index("$recoveryFiles = @")
    declarations_end = script.index("\n$script:lastUpdateCheck", declarations_start)
    declarations = script[declarations_start:declarations_end]
    marker = tmp_path / "fleet-agent-update-restart.sha"
    events = tmp_path / "events.txt"
    gate_counter = tmp_path / "gate-counter.txt"
    py_shim = tmp_path / "python-shim.ps1"
    py_shim.write_text(
        "param([string]$ScriptName)\n"
        "if ($ScriptName -eq 'fleet-agent-version.py') { "
        "Write-Output 'OK|0.3.0+git.tree.aaaaaaa|0.3.0+git.tree.ccccccc|'; exit 0 }\n"
        "if ($ScriptName -eq 'fleet-agent-update-gate.py') {\n"
        "  $count = if (Test-Path -LiteralPath $env:GATE_COUNT_FILE) { "
        "[int](Get-Content -LiteralPath $env:GATE_COUNT_FILE) } else { 0 }\n"
        "  Set-Content -LiteralPath $env:GATE_COUNT_FILE -Value ($count + 1)\n"
        f"  if ($count -eq 0) {{ Write-Output 'IDLE' }} else {{ Write-Output '{gate}' }}\n"
        "  exit 0\n"
        "}\n"
        "exit 1\n",
        encoding="utf-8",
    )
    target = "b" * 40
    local = "a" * 40
    initial_head = target if restart_surviving_marker else local
    agent_start = target if restart_surviving_marker else local
    setup_marker = (
        f"Set-Content -LiteralPath '{_ps_quote(marker)}' -Value '{target}' -NoNewline\n"
        if restart_surviving_marker
        else ""
    )
    operation = (
        "$policy = [pscustomobject]@{ State='OK'; Line='OK|m4|all|||' }\n"
        "$action = Complete-PendingUpdateRestart -MachinePolicy $policy\n"
        "Write-Output \"restart action=$action marker=$(Test-Path -LiteralPath $updateRestartMarker) stops=$script:stops\"\n"
        if restart_surviving_marker
        else (
            "$expected = [pscustomobject]@{ State='OK'; Line='OK|m4|all|||' }\n"
            "$result = Invoke-AutoUpdate -ExpectedMachinePolicy $expected\n"
            "Write-Output \"after-update result=$result marker=$(Test-Path -LiteralPath $updateRestartMarker) stops=$script:stops head=$script:head\"\n"
            + (
                "$later = [pscustomobject]@{ State='OK'; Line='OK|m4|all|||' }\n"
                "$action = Complete-PendingUpdateRestart -MachinePolicy $later\n"
                "Write-Output \"later action=$action marker=$(Test-Path -LiteralPath $updateRestartMarker) stops=$script:stops\"\n"
                if complete_later
                else ""
            )
        )
    )
    harness = tmp_path / "restart-sequence-harness.ps1"
    _install_corrected_agent_wrapper(tmp_path)
    harness.write_text(
        f"$repo = '{_ps_quote(tmp_path)}'\n"
        "$Label = 'm4'\n"
        f"$py = '{_ps_quote(py_shim)}'\n"
        f"$updateRestartMarker = '{_ps_quote(marker)}'\n"
        f"$agentStartHead = '{agent_start}'\n"
        "$UpdateEverySec = 0\n"
        "$script:lastUpdateCheck = [datetime]::MinValue\n"
        "$script:updatePending = $false\n"
        "$script:updBranch = $null\n"
        "$script:updRemote = $null\n"
        f"$script:head = '{initial_head}'\n"
        "$script:policyCalls = 0\n"
        "$script:stops = 0\n"
        "$script:mergeAttempted = $false\n"
        f"{declarations}\n"
        "function Log-Update { param([string]$msg, [string]$color = 'Gray') }\n"
        "function Get-ShortSha([string]$sha) { $sha }\n"
        "function Slot-Of($proc) { 0 }\n"
        "function Start-Sleep { param([int]$Seconds) }\n"
        f"function Get-LocalWorkers {{ Add-Content '{_ps_quote(events)}' 'enumerate'; "
        "@([pscustomobject]@{ ProcessId=123; CommandLine='--worker-id m4-0' }) }\n"
        f"function Stop-Process {{ param($Id, [switch]$Force, $ErrorAction); "
        f"$script:stops++; Add-Content '{_ps_quote(events)}' 'stop' }}\n"
        "function Get-MachineBlackoutStatus {\n"
        "  $script:policyCalls++\n"
        "  if ($script:policyCalls -eq 1) { return [pscustomobject]@{ State='OK'; Line='OK|m4|all|||' } }\n"
        f"  return [pscustomobject]@{{ State='{post_merge_state}'; Line='post-merge' }}\n"
        "}\n"
        "function git {\n"
        "  param([Parameter(ValueFromRemainingArguments=$true)][string[]]$GitArgs)\n"
        "  $key = $GitArgs -join ' '\n"
        "  $global:LASTEXITCODE = 0\n"
        "  switch ($key) {\n"
        "    'rev-parse --abbrev-ref HEAD' { 'main' }\n"
        "    'config branch.main.remote' { 'origin' }\n"
        f"    'rev-parse origin/main' {{ '{target}' }}\n"
        f"    'rev-parse {target}^{{tree}}' {{ '{'c' * 40}' }}\n"
        "    'rev-parse HEAD' {\n"
        + (
            "      if ($script:mergeAttempted) { $global:LASTEXITCODE = 17; return }\n"
            if post_merge_head == "failure"
            else (
                "      if ($script:mergeAttempted) { 'malformed'; return }\n"
                if post_merge_head == "malformed"
                else (
                    f"      if ($script:mergeAttempted) {{ '{'d' * 40}'; return }}\n"
                    if post_merge_head == "mismatch"
                    else ""
                )
            )
        )
        + "      $script:head\n"
        "    }\n"
        f"    'diff --name-only {local} {target}' {{ 'fleet-blackout-query.py' }}\n"
        f"    'merge --ff-only --quiet {target}' {{ $script:mergeAttempted = $true; "
        + ("$global:LASTEXITCODE = 17" if fail_merge else f"$script:head = '{target}'")
        + " }\n"
        "  }\n"
        "}\n"
        f"{functions}\n"
        f"{setup_marker}"
        f"{operation}",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["GATE_COUNT_FILE"] = str(gate_counter)
    return subprocess.run(
        [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


@pytest.mark.parametrize(
    ("script_name", "role"),
    [
        ("run-fleet-worker.ps1", "apply"),
        ("run-fleet-compute.ps1", "compute"),
        ("run-fleet-discovery.ps1", "discovery"),
    ],
)
@pytest.mark.parametrize(
    ("output", "exit_code", "allowed"),
    [
        ("OK|m4|{role}|||", 0, True),
        ("KEEP|m4|{role}|||error", 0, False),
        ("BLOCKED|m4|{role}|policy||reason", 0, False),
        ("KEEP|m4|{role}|||error\nOK|m4|{role}|||", 0, False),
        ("OK|m4|{role}|||\nBLOCKED|m4|{role}|policy||reason", 0, False),
        ("", 0, False),
        ("malformed", 0, False),
        ("OK|wrong-label|{role}|||", 0, False),
        ("OK|m4|wrong-role|||", 0, False),
        ("KEEP|m4|{role}|||error\nOK|m4|{role}|||", 0, False),
        ("OK|m4|{role}|||\nBLOCKED|m4|{role}|policy||reason", 0, False),
        ("OK|m4|{role}|||", 7, False),
    ],
)
@pytest.mark.parametrize("powershell", _supported_powershells())
def test_new_worker_launcher_requires_exact_successful_ok_status(
    tmp_path,
    script_name,
    role,
    output,
    exit_code,
    allowed,
    powershell,
):
    script = (REPO / script_name).read_text(encoding="utf-8")
    function = _powershell_function(script, "Test-MachineBlackout")
    shim = tmp_path / "blackout-shim.ps1"
    shim.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)]$Ignored)\n"
        "Write-Error $env:BLACKOUT_DIAGNOSTIC -ErrorAction Continue\n"
        "if ($env:BLACKOUT_OUTPUT) { $env:BLACKOUT_OUTPUT -split '\\r?\\n' | Write-Output }\n"
        "exit [int]$env:BLACKOUT_EXIT\n",
        encoding="utf-8",
    )
    harness = tmp_path / "guard-harness.ps1"
    harness.write_text(
        f"$ProjectRoot = '{_ps_quote(tmp_path)}'\n"
        "$Label = 'm4'\n"
        f"$py = '{_ps_quote(shim)}'\n"
        "$pyGuard = $py\n"
        f"{function}\n"
        f"$failure = Test-MachineBlackout '{role}'\n"
        "if ($null -eq $failure) { exit 0 }\n"
        "Write-Output $failure\n"
        "exit 42\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["BLACKOUT_OUTPUT"] = output.format(role=role)
    env["BLACKOUT_EXIT"] = str(exit_code)
    env["BLACKOUT_DIAGNOSTIC"] = "blackout query diagnostic"

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert (result.returncode == 0) is allowed, (result.stdout, result.stderr)
    assert "blackoutquerydiagnostic" in "".join(result.stderr.lower().split())
    if not allowed:
        assert result.returncode == 42, (result.stdout, result.stderr)

def test_launcher_guard_matrix_includes_every_available_supported_shell():
    shells = {Path(shell).stem.lower() for shell in _supported_powershells()}

    for name in ("powershell.exe", "pwsh"):
        if shutil.which(name):
            assert Path(name).stem.lower() in shells


@pytest.mark.parametrize(
    ("output", "exit_code", "expected_state"),
    [
        ("OK|m4|all|||", 0, "OK"),
        ("BLOCKED|m4|all|policy|2099-01-01T00:00:00+00:00|reason", 0, "BLOCKED"),
        ("BLOCKED|m4|all|policy|2099-01-01T00:00:00.123456+05:30|reason", 0, "BLOCKED"),
        ("BLOCKED|m4|all|policy||reason", 0, "KEEP"),
        ("BLOCKED|m4|all| |2099-01-01T00:00:00+00:00|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|2099-01-01T00:00:00|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|2099-01-01T00:00:00Z|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|January 1, 2099 12:00 AM +00:00|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|01/01/2099 00:00:00 +00:00|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|not-a-date|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|2020-01-01T00:00:00+00:00|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|2099-01-01T00:00:00+00:00|reason|extra", 0, "KEEP"),
        ("blocked|m4|all|policy|2099-01-01T00:00:00+00:00|reason", 0, "KEEP"),
        ("BLOCKED|M4|all|policy|2099-01-01T00:00:00+00:00|reason", 0, "KEEP"),
        ("BLOCKED|m4|apply|policy|2099-01-01T00:00:00+00:00|reason", 0, "KEEP"),
        ("", 0, "KEEP"),
        ("KEEP|m4|all|||error", 0, "KEEP"),
        ("malformed", 0, "KEEP"),
        ("KEEP|m4|all|||error\nOK|m4|all|||", 0, "KEEP"),
        ("OK|m4|all|||", 7, "KEEP"),
    ],
)
def test_fleet_agent_blackout_status_is_fail_closed(
    tmp_path,
    output,
    exit_code,
    expected_state,
):
    powershell = _primary_production_shell()
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    function = _powershell_function(script, "Get-MachineBlackoutStatus")
    shim = tmp_path / "blackout-shim.ps1"
    shim.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)]$Ignored)\n"
        "if ($env:BLACKOUT_OUTPUT) { $env:BLACKOUT_OUTPUT -split '\\r?\\n' | Write-Output }\n"
        "exit [int]$env:BLACKOUT_EXIT\n",
        encoding="utf-8",
    )
    harness = tmp_path / "agent-status-harness.ps1"
    harness.write_text(
        "$Label = 'm4'\n"
        f"$py = '{_ps_quote(shim)}'\n"
        f"{function}\n"
        "$status = Get-MachineBlackoutStatus 'all'\n"
        "Write-Output $status.State\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["BLACKOUT_OUTPUT"] = output
    env["BLACKOUT_EXIT"] = str(exit_code)

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == expected_state


@pytest.mark.parametrize(
    ("output", "exit_code"),
    [
        ("", 0),
        ("KEEP|m4|all|||error", 0),
        ("malformed", 0),
        ("KEEP|m4|all|||error\nOK|m4|all|||", 0),
        ("OK|m4|all|||", 7),
    ],
)
def test_fleet_agent_keeps_existing_state_when_blackout_status_is_uncertain(
    tmp_path,
    output,
    exit_code,
):
    powershell = _primary_production_shell()
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    function = _powershell_function(script, "Get-MachineBlackoutStatus")
    guard_start = script.index("  $machinePolicy = Get-MachineBlackoutStatus")
    guard_end = script.index("  $blackout =", guard_start)
    guard = script[guard_start:guard_end]
    shim = tmp_path / "blackout-shim.ps1"
    shim.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)]$Ignored)\n"
        "if ($env:BLACKOUT_OUTPUT) { $env:BLACKOUT_OUTPUT -split '\\r?\\n' | Write-Output }\n"
        "exit [int]$env:BLACKOUT_EXIT\n",
        encoding="utf-8",
    )
    harness = tmp_path / "agent-tick-harness.ps1"
    harness.write_text(
        "$Label = 'm4'\n"
        f"$py = '{_ps_quote(shim)}'\n"
        "$PollSec = 0\n"
        "$want = 2\n"
        "$mutations = 0\n"
        "function Start-Sleep { param([int]$Seconds) }\n"
        f"{function}\n"
        "for ($tick = 0; $tick -lt 1; $tick++) {\n"
        f"{guard}"
        "  $mutations++\n"
        "}\n"
        "Write-Output $mutations\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["BLACKOUT_OUTPUT"] = output
    env["BLACKOUT_EXIT"] = str(exit_code)

    result = subprocess.run(
        [powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.splitlines()[-1] == "0"


def test_fleet_agent_uncertain_blackout_guard_precedes_all_reconciliation_mutations():
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    guard = script.index('if ($machinePolicy.State -eq "KEEP")')
    skip_tick = script.index("continue", guard)

    for mutation in (
        "$blackout =",
        "$procs = Get-LocalWorkers",
        "generation $lastGen->$gen : restarting",
        "$lastGen = $gen",
        "Start-Process powershell.exe",
        "(scale-down / offload)",
    ):
        assert skip_tick < script.index(mutation, guard)


@pytest.mark.parametrize(
    ("fresh_state", "fresh_line", "expected_mutations", "expected_want"),
    [
        ("KEEP", "ERROR|blackout-query-exit=7", 0, -1),
        (
            "BLOCKED",
            "BLOCKED|m4|all|policy|2099-01-01T00:00:00+00:00|reason",
            1,
            0,
        ),
    ],
)
def test_fleet_agent_requeries_policy_after_update_before_reconciliation(
    tmp_path,
    fresh_state,
    fresh_line,
    expected_mutations,
    expected_want,
):
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    block_start = script.index("  $machinePolicy = Get-MachineBlackoutStatus")
    block_end = script.index("  $blackout =", block_start)
    block = script[block_start:block_end]
    query_shim = tmp_path / "fleet-query-shim.ps1"
    query_shim.write_text("Write-Output '2|codex|model|1'\n", encoding="utf-8")
    harness = tmp_path / "fresh-policy-harness.ps1"
    harness.write_text(
        "$Label = 'm4'\n"
        "$PollSec = 0\n"
        "$AutoUpdate = $true\n"
        f"$py = '{_ps_quote(query_shim)}'\n"
        "$script:policyCalls = 0\n"
        "$mutations = 0\n"
        "$want = -1\n"
        "function Start-Sleep { param([int]$Seconds) }\n"
        "function Invoke-AutoUpdate { param($ExpectedMachinePolicy) return $false }\n"
        "function Get-MachineBlackoutStatus {\n"
        "  $script:policyCalls++\n"
        "  if ($script:policyCalls -eq 1) { return [pscustomobject]@{ State='OK'; Line='OK|m4|all|||' } }\n"
        f"  return [pscustomobject]@{{ State='{fresh_state}'; Line='{fresh_line}' }}\n"
        "}\n"
        "for ($tick = 0; $tick -lt 1; $tick++) {\n"
        f"{block}"
        "  $mutations++\n"
        "}\n"
        "Write-Output \"mutations=$mutations want=$want calls=$script:policyCalls\"\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [_primary_production_shell(), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.splitlines()[-1] == (
        f"mutations={expected_mutations} want={expected_want} calls=2"
    )


def test_fleet_agent_keep_only_attempts_allowlisted_recovery_before_skipping_tick():
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    guard = script.index('if ($machinePolicy.State -eq "KEEP")')
    recovery = script.index("Invoke-AutoUpdate -RecoveryOnly", guard)
    skip_tick = script.index("continue", guard)

    assert guard < recovery < skip_tick
    for dependency in (
        '"fleet-blackout-query.py"',
        '"src/applypilot/fleet/machine_blackout.py"',
    ):
        assert dependency in script[
            script.index("$recoveryFiles = @") : script.index("$script:lastUpdateCheck")
        ]


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_fleet_agent_applies_allowlisted_recovery_without_worker_mutation(tmp_path, shell):
    result, worker = _run_recovery_update(tmp_path, shell, "fleet-blackout-query.py")

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert (worker / "fleet-blackout-query.py").read_text(encoding="utf-8") == "recovery\n"


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_fleet_agent_recovery_can_deliver_task_registration_fix(tmp_path, shell):
    result, worker = _run_recovery_update(tmp_path, shell, "register-fleet-tasks.ps1")

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert (worker / "register-fleet-tasks.ps1").read_text(encoding="utf-8") == "recovery\n"


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_fleet_agent_recovery_rejects_unpinned_target(tmp_path, shell):
    result, worker = _run_recovery_update(
        tmp_path,
        shell,
        "fleet-blackout-query.py",
        pinned_version="   ",
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (worker / "fleet-blackout-query.py").read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_normal_update_rejects_unpinned_target(tmp_path, shell):
    result, marker = _run_update_harness(tmp_path, shell, pinned_version="   ")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not marker.exists()
    assert "result=False" in result.stdout


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_post_merge_keep_preserves_workers_and_later_tick_completes_restart(
    tmp_path,
    shell,
):
    result = _run_restart_sequence_harness(
        tmp_path,
        shell,
        post_merge_state="KEEP",
        complete_later=True,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "after-update result=False marker=True stops=0" in result.stdout
    assert "later action=EXIT marker=False stops=1" in result.stdout


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_restart_surviving_marker_reconciles_under_current_agent(tmp_path, shell):
    result = _run_restart_sequence_harness(
        tmp_path,
        shell,
        restart_surviving_marker=True,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "restart action=RECONCILE marker=False stops=1" in result.stdout


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
@pytest.mark.parametrize("post_merge_state", ["OK", "BLOCKED"])
def test_old_agent_exits_after_immediate_controlled_restart(
    tmp_path,
    shell,
    post_merge_state,
):
    result = _run_restart_sequence_harness(
        tmp_path,
        shell,
        post_merge_state=post_merge_state,
    )

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert (tmp_path / "events.txt").read_text(encoding="utf-8").splitlines() == [
        "enumerate",
        "stop",
    ]
    assert not (tmp_path / "fleet-agent-update-restart.sha").exists()


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_post_merge_non_idle_gate_retains_marker_and_workers(tmp_path, shell):
    result = _run_restart_sequence_harness(
        tmp_path,
        shell,
        post_merge_state="OK",
        gate="BUSY",
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "after-update result=False marker=True stops=0" in result.stdout


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_merge_failure_clears_marker_without_stopping_workers(tmp_path, shell):
    result = _run_restart_sequence_harness(tmp_path, shell, fail_merge=True)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "after-update result=False marker=False stops=0" in result.stdout


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
@pytest.mark.parametrize("post_merge_head", ["failure", "malformed", "mismatch"])
def test_post_merge_head_ambiguity_retains_marker_and_workers(tmp_path, shell, post_merge_head):
    result = _run_restart_sequence_harness(
        tmp_path,
        shell,
        post_merge_head=post_merge_head,
        complete_later=True,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "after-update result=False marker=True stops=0" in result.stdout
    assert "later action=WAIT marker=True stops=0" in result.stdout


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
@pytest.mark.parametrize(
    ("repinned_version", "second_version_exit"),
    [
        ("0.3.0+git.tree.ddddddd", 0),
        ("   ", 0),
        ("0.3.0+git.tree.ccccccc", 17),
    ],
    ids=["changed", "blank", "query-failure"],
)
def test_normal_update_aborts_on_concurrent_repin(tmp_path, shell, repinned_version, second_version_exit):
    result, mutation_marker = _run_update_harness(
        tmp_path,
        shell,
        repinned_version=repinned_version,
        second_version_exit=second_version_exit,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not mutation_marker.exists()
    assert "result=False" in result.stdout
    assert not (tmp_path / "missing-restart-marker.sha").exists()


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_recovery_update_aborts_on_concurrent_repin(tmp_path, shell):
    result, worker = _run_recovery_update(
        tmp_path,
        shell,
        "fleet-blackout-query.py",
        repinned_version="0.3.0+git.tree.ddddddd",
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert (worker / "fleet-blackout-query.py").read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize(
    ("fresh_state", "fresh_line"),
    [
        ("KEEP", "ERROR|blackout-query-exit=1"),
        ("BLOCKED", "BLOCKED|m4|all|policy|2099-01-01T00:00:00+00:00|reason"),
    ],
)
@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_normal_update_revalidates_policy_before_worker_mutation(tmp_path, shell, fresh_state, fresh_line):
    result, marker = _run_update_harness(
        tmp_path,
        shell,
        fresh_policy_state=fresh_state,
        fresh_policy_line=fresh_line,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not marker.exists()
    assert "result=False" in result.stdout


@pytest.mark.parametrize(
    "failed_command",
    [
        "rev-parse origin/main",
        "rev-parse bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb^{tree}",
        "status --porcelain",
        "diff --name-only aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    ],
)
@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_normal_update_fails_closed_on_git_command_failure(tmp_path, shell, failed_command):
    result, marker = _run_update_harness(tmp_path, shell, fail_git_command=failed_command)

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not marker.exists()
    assert "result=False" in result.stdout


@pytest.mark.parametrize("shell", _production_shells(), ids=lambda value: Path(value).name)
def test_fleet_agent_rejects_broader_recovery_update(tmp_path, shell):
    result, worker = _run_recovery_update(tmp_path, shell, "src/applypilot/unrelated.py")

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert not (worker / "src/applypilot/unrelated.py").exists()
    assert (worker / "fleet-blackout-query.py").read_text(encoding="utf-8") == "old\n"


def test_fleet_agent_honors_machine_blackout():
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")

    assert "fleet-blackout-query.py" in script
    assert "machine blackout active; effective desired_workers 0" in script
    assert "$want = 0" in script


def test_compute_launcher_refuses_machine_blackout():
    script = (REPO / "run-fleet-compute.ps1").read_text(encoding="utf-8")

    assert "fleet-blackout-query.py" in script
    assert "Refusing to start compute workers" in script
    assert '-ceq $expected' in script


def test_discovery_launcher_refuses_machine_blackout():
    script = (REPO / "run-fleet-discovery.ps1").read_text(encoding="utf-8")

    assert "fleet-blackout-query.py" in script
    assert "Refusing to start discovery workers" in script
    assert '-ceq $expected' in script


def test_apply_launcher_refuses_machine_blackout():
    script = (REPO / "run-fleet-worker.ps1").read_text(encoding="utf-8")

    assert "fleet-blackout-query.py" in script
    assert "Refusing to start apply worker" in script
    assert '-ceq $expected' in script
