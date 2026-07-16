"""Fail-closed database environment contract for root fleet supervisors."""

from __future__ import annotations

from collections.abc import Mapping


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

FORBIDDEN_DATABASE_ENV_VARS = (
    "APPLYPILOT_FLEET_DSN",
    "DATABASE_URL",
    "DATABASE_PUBLIC_URL",
    "DATABASE_PRIVATE_URL",
    "POSTGRES_URL",
    "POSTGRES_PUBLIC_URL",
    "POSTGRES_PRIVATE_URL",
    *LIBPQ_SESSION_ENV_VARS,
)


def require_fleet_pg_dsn(environ: Mapping[str, str]) -> str:
    """Return only FLEET_PG_DSN and reject every competing connection source."""
    ambient = sorted(name for name in FORBIDDEN_DATABASE_ENV_VARS if name in environ)
    if ambient:
        raise RuntimeError("forbidden ambient database variables: " + ", ".join(ambient))
    dsn = environ.get("FLEET_PG_DSN", "").strip()
    if not dsn:
        raise RuntimeError("FLEET_PG_DSN is required")
    return dsn
