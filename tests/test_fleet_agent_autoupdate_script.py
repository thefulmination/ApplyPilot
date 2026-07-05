from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_fleet_agent_autoupdate_checks_pin_before_fast_forward() -> None:
    script = (REPO / "fleet-agent.ps1").read_text(encoding="utf-8")

    for text in (
        "fleet-agent-version.py",
        "pinned_worker_version",
        "$pinnedVersion",
        "$targetVersion",
        "git rev-parse",
        "^{tree}",
        "remote tree $targetVersion is not pinned",
        "UPDATE BLOCKED: pinned version",
    ):
        assert text in script
