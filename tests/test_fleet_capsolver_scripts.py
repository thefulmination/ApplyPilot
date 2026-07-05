from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def _text(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def test_run_fleet_worker_fails_closed_without_capsolver_readiness() -> None:
    script = _text("run-fleet-worker.ps1")

    assert "fleet-capsolver-check" in script
    assert "CapSolver fleet readiness failed" in script
    assert "Refusing to start worker" in script
    assert "applypilot.exe" in script


def test_fleet_agent_reports_capsolver_readiness_in_preflight() -> None:
    script = _text("fleet-agent.ps1")

    assert "fleet-capsolver-check" in script
    assert "CapSolver readiness" in script
    assert "applypilot.exe" in script
    assert "$pf +=" in script


def test_worker_setup_persists_capsolver_key_for_apply_workers() -> None:
    script = _text("setup-fleet-worker.ps1")

    assert "CAPSOLVER_API_KEY" in script
    assert "[Environment]::SetEnvironmentVariable(\"CAPSOLVER_API_KEY\"" in script
    assert "InstallDir \".applypilot\"" in script
    assert ".env" in script


def test_machine_setup_persists_capsolver_key_for_codex_bridge_and_workers() -> None:
    script = _text("setup-fleet-machine.ps1")

    assert "CAPSOLVER_API_KEY" in script
    assert "[Environment]::SetEnvironmentVariable(\"CAPSOLVER_API_KEY\"" in script
    assert "InstallDir \".applypilot\"" in script
    assert ".env" in script
