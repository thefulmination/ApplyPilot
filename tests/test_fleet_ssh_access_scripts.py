from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_windows_fleet_ssh_access_script_bootstraps_key_and_tailscale_only_sshd() -> None:
    script = (REPO / "setup-fleet-ssh-access.ps1").read_text(encoding="utf-8")

    for text in (
        ".ssh\\codex_fleet_ed25519",
        "ssh-keygen",
        "codex-fleet-access",
        "ApplyPilot fleet SSH public key",
        "OpenSSH.Server",
        "sshd",
        "Set-Service -Name sshd -StartupType Automatic",
        "Start-Service sshd",
        "authorized_keys",
        "administrators_authorized_keys",
        "100.64.0.0/10",
        "New-NetFirewallRule",
        "RemoteAddress $TailnetCidr",
        "BatchMode=yes",
        "StrictHostKeyChecking=accept-new",
        "rstal@tarpon",
        "backoffice@gggtower",
        "palomaperez@palomas-macbook-air",
    ):
        assert text in script

    assert "PasswordAuthentication yes" not in script
    assert "0.0.0.0/0" not in script


def test_mac_fleet_ssh_access_script_installs_public_key_without_private_material() -> None:
    script = (REPO / "setup-fleet-ssh-access-mac.sh").read_text(encoding="utf-8")

    for text in (
        "APPLYPILOT_FLEET_SSH_PUBLIC_KEY",
        "$HOME/.ssh/authorized_keys",
        "chmod 700 \"$HOME/.ssh\"",
        "chmod 600 \"$AUTH_KEYS\"",
        "systemsetup -setremotelogin on",
        "ssh-ed25519",
        "codex-fleet-access",
    ):
        assert text in script

    assert "codex_fleet_ed25519" not in script
    assert "PRIVATE KEY" not in script


def test_readme_documents_fleet_ssh_bootstrap_and_check_flow() -> None:
    readme = (REPO / "README.md").read_text(encoding="utf-8")

    for text in (
        "setup-fleet-ssh-access.ps1 -GenerateKey",
        "setup-fleet-ssh-access.ps1 -InstallPublicKey",
        "setup-fleet-ssh-access.ps1 -Check",
        "setup-fleet-ssh-access-mac.sh",
        "codex_fleet_ed25519",
        "Tailscale",
    ):
        assert text in readme
