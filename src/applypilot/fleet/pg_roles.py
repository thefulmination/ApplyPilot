"""Least-privilege PG role for REMOTE fleet workers (the Mac / any offsite box).

The home box connects as `postgres` (superuser, local pgpass). Remote workers connect
as `fleet_worker` instead: LOGIN + DML on the fleet tables in the CURRENT database —
no superuser, no DDL, no CREATEROLE, no other databases. Applied idempotently by the
home-box hardening script (setup-fleet-pg-tailscale.ps1); re-running with a new
password rotates the credential (the remote kill switch)."""
from __future__ import annotations

from psycopg import sql

DEFAULT_ROLE = "fleet_worker"

_GRANTS = (
    "GRANT CONNECT ON DATABASE {db} TO {role}",
    "GRANT USAGE ON SCHEMA public TO {role}",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
    # Tables the superuser creates LATER (schema migrations) stay usable without re-running:
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {role}",
)


def ensure_fleet_worker_role(conn, password: str, *, role: str = DEFAULT_ROLE) -> None:
    """Idempotently create/refresh the remote-worker role on conn's CURRENT database."""
    r = sql.Identifier(role)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        verb = "ALTER" if cur.fetchone() else "CREATE"
        # CREATE/ALTER ROLE are utility statements: no server-side params -> sql.Literal.
        cur.execute(sql.SQL(
            f"{verb} ROLE {{}} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {{}}"
        ).format(r, sql.Literal(password)))
        # NOTE: conn may use a dict_row (or other non-tuple) row_factory (e.g. via
        # applypilot.apply.pgqueue.connect), so index by column name, not position.
        cur.execute("SELECT current_database() AS current_database")
        row = cur.fetchone()
        dbname = row["current_database"] if isinstance(row, dict) else row[0]
        db = sql.Identifier(dbname)
        for stmt in _GRANTS:
            cur.execute(sql.SQL(stmt.replace("{db}", "{0}").replace("{role}", "{1}")).format(db, r))
    conn.commit()
