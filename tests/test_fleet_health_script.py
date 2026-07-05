from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_fleet_health_script_covers_current_fleet_topology() -> None:
    script = (REPO / "fleet-health.ps1").read_text(encoding="utf-8")

    for text in (
        "100.90.104.99",
        "rstal@tarpon",
        "backoffice@gggtower",
        "palomaperez@palomas-macbook-air",
        "worker_heartbeat",
        "remote_commands",
        "discovered_postings",
        "search_tasks",
        "Get-ScheduledTask",
        "pip check",
        "ssh",
    ):
        assert text in script
