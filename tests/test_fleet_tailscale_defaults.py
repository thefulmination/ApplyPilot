from pathlib import Path
import tomllib


REPO = Path(__file__).resolve().parents[1]
OLD_HOME_IP = "192.168.1.187"
TAILSCALE_HOME_IP = "100.90.104.99"

FILES_WITH_FLEET_HOME_DEFAULTS = [
    "fleet-agent.ps1",
    "register-fleet-tasks.ps1",
    "setup-fleet-worker.ps1",
    "setup-fleet-discovery.ps1",
    "setup-fleet-machine.ps1",
    "load-canary-home.ps1",
    "load-canary-remote.ps1",
]


def test_fleet_scripts_default_to_tailscale_home_ip():
    offenders = []
    for rel in FILES_WITH_FLEET_HOME_DEFAULTS:
        text = (REPO / rel).read_text(encoding="utf-8")
        if OLD_HOME_IP in text:
            offenders.append(rel)
        assert TAILSCALE_HOME_IP in text, f"{rel} should mention the fleet Tailscale host"

    assert not offenders, f"stale LAN home IP still present in: {', '.join(offenders)}"


def test_project_pins_jobspy_numpy_requirement():
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    deps = data["project"]["dependencies"]
    assert "numpy==1.26.3" in deps
    assert any(dep.startswith("tls-client") for dep in deps)


def test_discovery_bootstrap_checks_dependency_consistency():
    text = (REPO / "setup-fleet-discovery.ps1").read_text(encoding="utf-8")
    assert "--no-deps python-jobspy" in text
    assert "-m pip check" in text


def test_watchdog_task_wrapper_cleans_stale_watchdog_children():
    text = (REPO / "register-fleet-tasks.ps1").read_text(encoding="utf-8")

    assert "Stop-StaleWatchdogProcesses" in text
    assert "applypilot-fleet-watchdog.exe" in text
    assert "watchdog-task.ps1" in text
