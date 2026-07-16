import os
from pathlib import Path
import shutil
import subprocess
import sys

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


def _ps_quote(value) -> str:
    return str(value).replace("'", "''")


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
        "import os\nprint(os.environ['VERSION_LINE'])\n",
        encoding="utf-8",
    )
    (seed / "fleet-blackout-query.py").write_text("old\n", encoding="utf-8")
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


def _run_recovery_update(tmp_path: Path, changed_path: str) -> tuple[subprocess.CompletedProcess, Path]:
    worker, target_version = _recovery_update_fixture(tmp_path, changed_path)
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")
    update_function = _powershell_function(script, "Invoke-AutoUpdate")
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
        "$UpdateEverySec = 0\n"
        "$script:lastUpdateCheck = [datetime]::MinValue\n"
        "$script:updatePending = $false\n"
        "$script:updBranch = $null\n"
        "$script:updRemote = $null\n"
        f"{recovery_declaration}\n"
        "function Log-Update { param([string]$msg, [string]$color = 'Gray') }\n"
        "function Get-ShortSha([string]$sha) { $sha }\n"
        f"function Get-LocalWorkers {{ Add-Content '{_ps_quote(mutation_marker)}' 'workers'; @() }}\n"
        f"{update_function}\n"
        "Invoke-AutoUpdate -RecoveryOnly | Out-Null\n"
        "exit 0\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["VERSION_LINE"] = f"OK|0.3.0+git.tree.0000000|{target_version}|"
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    assert not mutation_marker.exists(), (result.stdout, result.stderr)
    return result, worker


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
        ("", 0, False),
        ("malformed", 0, False),
        ("OK|wrong-label|{role}|||", 0, False),
        ("OK|m4|wrong-role|||", 0, False),
        ("KEEP|m4|{role}|||error\nOK|m4|{role}|||", 0, False),
        ("OK|m4|{role}|||\nBLOCKED|m4|{role}|policy||reason", 0, False),
        ("OK|m4|{role}|||", 7, False),
    ],
)
def test_new_worker_launcher_requires_exact_successful_ok_status(
    tmp_path,
    script_name,
    role,
    output,
    exit_code,
    allowed,
):
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh is required for launcher guard tests")
    script = (REPO / script_name).read_text(encoding="utf-8")
    function = _powershell_function(script, "Test-MachineBlackout")
    shim = tmp_path / "blackout-shim.ps1"
    shim.write_text(
        "param([Parameter(ValueFromRemainingArguments=$true)]$Ignored)\n"
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

    result = subprocess.run(
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )

    assert (result.returncode == 0) is allowed, (result.stdout, result.stderr)
    if not allowed:
        assert result.returncode == 42, (result.stdout, result.stderr)


@pytest.mark.parametrize(
    ("output", "exit_code", "expected_state"),
    [
        ("OK|m4|all|||", 0, "OK"),
        ("BLOCKED|m4|all|policy|2099-01-01T00:00:00Z|", 0, "BLOCKED"),
        ("BLOCKED|m4|all|policy|2099-01-01T00:00:00+00:00|reason", 0, "BLOCKED"),
        ("BLOCKED|m4|all|policy||reason", 0, "KEEP"),
        ("BLOCKED|m4|all| |2099-01-01T00:00:00Z|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|2099-01-01T00:00:00|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|not-a-date|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|2020-01-01T00:00:00Z|reason", 0, "KEEP"),
        ("BLOCKED|m4|all|policy|2099-01-01T00:00:00Z|reason|extra", 0, "KEEP"),
        ("blocked|m4|all|policy|2099-01-01T00:00:00Z|reason", 0, "KEEP"),
        ("BLOCKED|M4|all|policy|2099-01-01T00:00:00Z|reason", 0, "KEEP"),
        ("BLOCKED|m4|apply|policy|2099-01-01T00:00:00Z|reason", 0, "KEEP"),
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
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh is required for fleet-agent guard tests")
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
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
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
    pwsh = shutil.which("pwsh")
    if not pwsh:
        pytest.skip("pwsh is required for fleet-agent guard tests")
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
        [pwsh, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(harness)],
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
        assert dependency in script[script.index("$selfFiles = @") : script.index("$script:lastUpdateCheck")]


def test_fleet_agent_applies_allowlisted_recovery_without_worker_mutation(tmp_path):
    result, worker = _run_recovery_update(tmp_path, "fleet-blackout-query.py")

    assert result.returncode == 1, (result.stdout, result.stderr)
    assert (worker / "fleet-blackout-query.py").read_text(encoding="utf-8") == "recovery\n"


def test_fleet_agent_rejects_broader_recovery_update(tmp_path):
    result, worker = _run_recovery_update(tmp_path, "src/applypilot/unrelated.py")

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
