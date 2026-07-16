import os
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
        ("", 0, False),
        ("malformed", 0, False),
        ("OK|wrong-label|{role}|||", 0, False),
        ("OK|m4|wrong-role|||", 0, False),
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
        "if ($env:BLACKOUT_OUTPUT) { Write-Output $env:BLACKOUT_OUTPUT }\n"
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
