from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def test_railway_entrypoint_runs_canonical_v3_worker_fail_closed() -> None:
    script = (REPO / "deploy" / "entrypoint.sh").read_text(encoding="utf-8")

    for required in (
        "require_env DATABASE_URL",
        "require_env DEEPSEEK_API_KEY",
        "require_env APPLYPILOT_WORKER_ID",
        "require_env APPLYPILOT_RELEASE_VERSION",
        'require_file "${APPLYPILOT_DIR:-/data/applypilot}/profile.json"',
        'require_file "${APPLYPILOT_DIR:-/data/applypilot}/resume.pdf"',
        'exec applypilot-fleet-apply "${worker_args[@]}"',
        '--dsn "$DATABASE_URL"',
        '--worker-id "$APPLYPILOT_WORKER_ID"',
        '--machine-owner "$FLEET_MACHINE_OWNER"',
    ):
        assert required in script

    assert "applypilot.apply.container_worker" not in script
    assert "${APPLYPILOT_WORKER_ID:-0}" not in script


def test_docker_contract_documents_external_release_identity_and_assets() -> None:
    dockerfile = (REPO / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (REPO / ".dockerignore").read_text(encoding="utf-8")

    assert ".git" in dockerignore.splitlines()
    assert "APPLYPILOT_RELEASE_VERSION" in dockerfile
    assert "APPLYPILOT_WORKER_ID" in dockerfile
    assert "/data/applypilot" in dockerfile


def test_railway_runbook_documents_fail_closed_release_contract() -> None:
    runbook = (REPO / "docs" / "railway-canonical-worker-deployment.md").read_text(
        encoding="utf-8"
    )

    for required in (
        "applypilot-fleet-apply",
        "APPLYPILOT_WORKER_ID",
        "APPLYPILOT_RELEASE_VERSION",
        "fleet_config.pinned_worker_version",
        "/data/applypilot",
        "profile.json",
        "resume.pdf",
        "Keep global and ATS lane pauses enabled",
        "Apply schema migrations",
        "fresh heartbeat",
    ):
        assert required in runbook
