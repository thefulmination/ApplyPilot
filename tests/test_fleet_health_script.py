from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_fleet_health_script_covers_current_fleet_topology() -> None:
    script = (REPO / "fleet-health.ps1").read_text(encoding="utf-8")

    for text in (
        "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5",
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
