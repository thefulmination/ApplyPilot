from pathlib import Path


def test_fleet_worker_launcher_never_uses_default_zero_home_ip():
    script = Path("run-fleet-worker.ps1").read_text(encoding="utf-8")

    assert "Resolve-FleetHomeIp" in script
    assert "Refusing to start worker" in script
    assert "--home-ip" in script
    assert "--machine-owner" in script
    assert "FLEET_MACHINE_OWNER = $Label" in script
    assert "--worker-id \"$WorkerId\" --chrome-slot $Slot --agent $Agent @margs @fargs" not in script


def test_fleet_worker_launcher_can_detect_tailscale_ip_when_env_missing():
    script = Path("run-fleet-worker.ps1").read_text(encoding="utf-8")

    assert "tailscale.exe" in script
    assert "ip -4" in script
    assert "100.*" in script
