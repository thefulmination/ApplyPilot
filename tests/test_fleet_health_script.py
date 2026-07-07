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
        "apply_queue lease blockers",
        "already_applied_dedup",
        "lease_candidate_before_governor",
        "dedup_suppressed_terminal",
        "remote_commands",
        "fleet_machine_blackout active",
        "allow_patterns",
        "block_patterns",
        "discovered_postings",
        "search_tasks",
        "Get-ScheduledTask",
        "pip check",
        "ssh",
        "fleet-capsolver-check --json",
        "CapSolver readiness",
        "Version drift",
        "pinned_worker_version",
        "sw_version",
        "role IN ('apply', 'compute', 'discovery')",
        "last_beat > now() - interval '5 minutes'",
        "Stale worker versions",
        "last_beat <= now() - interval '5 minutes'",
        "git status --short --branch",
        "git rev-parse --short HEAD",
    ):
        assert text in script


def test_check_fleet_ready_script_fails_closed_on_apply_blockers() -> None:
    script = (REPO / "check-fleet-ready.ps1").read_text(encoding="utf-8")

    for text in (
        "VerifyLive",
        "verify-live exit=",
        "fleet_config",
        "agent_availability",
        "blocked_until > now()",
        "remote_commands",
        "acked_at IS NULL",
        "fleet_desired_state",
        "desired_workers",
        "fresh LinkedIn worker heartbeat",
        'r["role"] == "linkedin"',
        "linkedin_queue",
        "apply_queue",
        "approved_batch IS NOT NULL",
        "sys.exit(2 if blockers else 0)",
        "AllowPaused",
    ):
        assert text in script
