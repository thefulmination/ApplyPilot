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
        '$HOME/applypilot-fleet',
        "git status --short --branch",
        "$script:RemoteFailures",
        "Invoke-CheckedNative",
        "git status --porcelain --untracked-files=no",
        "APPLY: stashing tracked local changes before sync",
        "git stash push -m",
        "git stash push failed with exit",
        "git fetch `$remoteName `$branch",
        "git fetch \"`$remote_name\"",
        '"myfork", "origin", "homebundle"',
        "for remote_name in myfork origin homebundle",
        "ERROR: could not fetch branch `$branch from myfork/origin/homebundle",
        "refs/heads/`$branch",
        "refs/remotes/`$candidate",
        ".applypilot/fleet-worker.env",
        "ERROR: could not fetch branch",
        "git checkout -b",
        "git merge --ff-only `$remoteRef",
        "git merge --ff-only",
        "stop ApplyPilotFleet tasks/processes before reinstall",
        "Stop-ScheduledTask",
        "Stop-Process",
        "applypilot-fleet-*",
        "~pplypilot*",
        "Remove-Item -LiteralPath",
        "find . -maxdepth 1 -name '~pplypilot*'",
        "if [ -d \"`$site_dir\" ]; then",
        "pip install -e .",
        "pip install -e . failed with exit",
        "register-fleet-tasks.ps1",
        "start ApplyPilotFleet tasks",
        "Start-ScheduledTask",
        "fleet-health.ps1",
        "pass -RunHealth to include it",
        "EncodedCommand",
        "Fleet reconcile failed on",
    ):
        assert text in script

    assert '$body = $body -replace "`r`n", "`n"' in script

    assert "192.168.1.187" not in script
    assert '$HOME/ApplyPilot' not in script
    assert 'git fetch "$remote_name"' not in script
    assert "checkout -B" not in script
    assert "git fetch --all" not in script
    assert "-like '*$repoLiteral*'" not in script


def test_readme_documents_reconcile_as_repair_not_normal_deploy() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    assert "fleet-agent.ps1 -AutoUpdate" in readme
    assert "Invoke-FleetReconcile.ps1" in readme
    assert "bootstrap/repair" in readme
