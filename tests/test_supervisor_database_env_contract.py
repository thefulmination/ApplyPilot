"""Deployment-only database environment contracts for root supervisors."""

from pathlib import Path


REPO = Path(__file__).resolve().parents[1]

LIBPQ_SESSION_ENV_VARS = (
    "PGAPPNAME",
    "PGCHANNELBINDING",
    "PGCLIENTENCODING",
    "PGCONNECT_TIMEOUT",
    "PGDATABASE",
    "PGDATESTYLE",
    "PGGEQO",
    "PGGSSDELEGATION",
    "PGGSSENCMODE",
    "PGGSSLIB",
    "PGHOST",
    "PGHOSTADDR",
    "PGKRBSRVNAME",
    "PGLOADBALANCEHOSTS",
    "PGLOCALEDIR",
    "PGMAXPROTOCOLVERSION",
    "PGMINPROTOCOLVERSION",
    "PGOPTIONS",
    "PGPASSFILE",
    "PGPASSWORD",
    "PGPORT",
    "PGREQUIREAUTH",
    "PGREQUIREPEER",
    "PGREQUIRESSL",
    "PGSERVICE",
    "PGSERVICEFILE",
    "PGSYSCONFDIR",
    "PGSSLCERT",
    "PGSSLCERTMODE",
    "PGSSLCOMPRESSION",
    "PGSSLCRL",
    "PGSSLCRLDIR",
    "PGSSLKEY",
    "PGSSLMAXPROTOCOLVERSION",
    "PGSSLMINPROTOCOLVERSION",
    "PGSSLMODE",
    "PGSSLNEGOTIATION",
    "PGSSLROOTCERT",
    "PGSSLSNI",
    "PGTARGETSESSIONATTRS",
    "PGTZ",
    "PGUSER",
)


def test_supervisor_helpers_share_strict_fleet_dsn_contract() -> None:
    for name in ("fleet-agent-query.py", "fleet-agent-update-gate.py", "fleet-agent-version.py"):
        script = (REPO / name).read_text(encoding="utf-8")
        assert "from fleet_agent_env import require_fleet_pg_dsn" in script
        assert "APPLYPILOT_FLEET_DSN" not in script
        assert "DATABASE_URL" not in script

    contract = (REPO / "fleet_agent_env.py").read_text(encoding="utf-8")
    assert set(LIBPQ_SESSION_ENV_VARS) == {
        line.strip().strip('",')
        for line in contract.splitlines()
        if line.strip().startswith('"PG') and line.strip().strip('",') != "PG_CONFIG"
    }
    assert "PG_CONFIG" not in contract
    assert "APPLYPILOT_FLEET_DSN" in contract
    assert "DATABASE_URL" in contract


def test_entrypoint_rejects_complete_libpq_and_legacy_fleet_environment() -> None:
    script = (REPO / "deploy/entrypoint.sh").read_text(encoding="utf-8")
    for name in (*LIBPQ_SESSION_ENV_VARS, "APPLYPILOT_FLEET_DSN"):
        assert f"  {name}\n" in script
    assert "PG_CONFIG" not in script
