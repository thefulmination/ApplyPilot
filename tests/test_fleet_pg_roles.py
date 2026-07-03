"""fleet_worker role: remote (Tailscale) workers get DML on the fleet tables and nothing
else — no superuser, no DDL. Exercises the EXACT tables the apply worker writes."""
import psycopg
import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import pg_roles


def _worker_dsn(fleet_db: str) -> str:
    # fleet_db is postgresql://postgres@127.0.0.1:<port>/postgres (trust auth locally)
    return fleet_db.replace("postgres@", "fleet_worker@", 1)


def test_role_can_dml_fleet_tables(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        pg_roles.ensure_fleet_worker_role(conn, "test-pw-1")
    with psycopg.connect(_worker_dsn(fleet_db)) as wconn:
        with wconn.cursor() as cur:
            cur.execute("SELECT paused FROM fleet_config WHERE id = 1")
            assert cur.fetchone() is not None
            cur.execute("UPDATE fleet_config SET paused = paused WHERE id = 1")
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, machine_owner, home_ip, role, state, last_beat) "
                "VALUES ('mac-test', 't', '0.0.0.0', 'apply', 'idle', now())")
        wconn.commit()


def test_role_cannot_ddl(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        pg_roles.ensure_fleet_worker_role(conn, "test-pw-1")
    with psycopg.connect(_worker_dsn(fleet_db)) as wconn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with wconn.cursor() as cur:
                cur.execute("CREATE TABLE mac_worker_should_fail (id int)")


def test_rerun_is_idempotent_password_rotation(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        pg_roles.ensure_fleet_worker_role(conn, "pw-old")
        pg_roles.ensure_fleet_worker_role(conn, "pw-new")  # must not raise
    with psycopg.connect(_worker_dsn(fleet_db)) as wconn:  # role still connects + works
        with wconn.cursor() as cur:
            cur.execute("SELECT 1 FROM fleet_config WHERE id = 1")
            assert cur.fetchone() == (1,)
