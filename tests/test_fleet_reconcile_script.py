from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_reconcile_script_is_check_only_by_default_and_uses_tailscale_targets() -> None:
    script = (REPO / "Invoke-FleetReconcile.ps1").read_text(encoding="utf-8")

    for text in (
        "[switch]$Apply",
        "[switch]$RunHealth",
        "RemoteCommandTimeoutSeconds",
        "ConnectionAttempts=1",
        "ServerAliveInterval=5",
        "remote command timed out",
        "CHECK-ONLY",
        "rstal@tarpon",
        "backoffice@gggtower",
        "palomaperez@palomas-macbook-air",
        "git status --short --branch",
        "git fetch `$remoteName `$branch",
        "git fetch \"$remote_name\"",
        '"myfork", "origin", "homebundle"',
        "for remote_name in myfork origin homebundle",
        "refs/heads/`$branch",
        "refs/remotes/`$candidate",
        "git checkout -b",
        "git merge --ff-only",
        "stop ApplyPilotFleet tasks/processes before reinstall",
        "Stop-ScheduledTask",
        "Stop-Process",
        "pip install -e .",
        "register-fleet-tasks.ps1",
        "fleet-health.ps1",
        "pass -RunHealth to include it",
        "EncodedCommand",
    ):
        assert text in script

    assert "192.168.1.187" not in script
    assert "checkout -B" not in script
    assert "git fetch --all" not in script


def test_readme_documents_reconcile_as_repair_not_normal_deploy() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    assert "fleet-agent.ps1 -AutoUpdate" in readme
    assert "Invoke-FleetReconcile.ps1" in readme
    assert "bootstrap/repair" in readme
