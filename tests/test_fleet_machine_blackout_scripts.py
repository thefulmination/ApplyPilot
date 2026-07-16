import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]


def _supported_powershells() -> list[str]:
    return [shell for name in ("powershell.exe", "pwsh") if (shell := shutil.which(name))]


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
    assert "blackout query diagnostic" in result.stderr
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
        ("BLOCKED|m4|all|policy||reason", 0, "BLOCKED"),
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
        ("KEEP|m4|all|||error", 0),
        ("malformed", 0),
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
        "if ($AutoUpdate) { Invoke-AutoUpdate",
        "$blackout =",
        "$procs = Get-LocalWorkers",
        "generation $lastGen->$gen : restarting",
        "$lastGen = $gen",
        "Start-Process powershell.exe",
        "(scale-down / offload)",
    ):
        assert skip_tick < script.index(mutation, guard)


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
