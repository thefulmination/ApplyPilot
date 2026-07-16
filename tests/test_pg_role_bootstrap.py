"""Live disposable PostgreSQL coverage for one-time role bootstrap."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import sys

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from applypilot.apply import pgqueue
from applypilot.fleet import pg_roles


RETIRED_ADMIN = "bootstrap_retired_admin_test"
OWNER = "bootstrap_database_owner_test"
CONTROLLER = "bootstrap_controller_test"
CONTROLLER_PASSWORD = "bootstrap-controller-password"
VERIFIER = "bootstrap_verifier_test"
MIGRATOR = "bootstrap_migrator_test"
WORKER_ROLE = "bootstrap_worker_test"
WORKER_ID = "bootstrap-worker-node"
WORKER_PASSWORD = "bootstrap-worker-password"


def _bootstrap_script_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-fleet-pg-roles.py"
    spec = importlib.util.spec_from_file_location("applypilot_bootstrap_script", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_bootstrap_crash_after_commit_leaves_precommit_receipt_in_doubt(
    tmp_path: Path, monkeypatch
) -> None:
    module = _bootstrap_script_module()
    receipt_path = tmp_path / "receipt.json"
    rollback_path = tmp_path / "rollback.sql"
    committed = False

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_bootstrap(_conn, _password, *, topology, evidence_writer):
        nonlocal committed
        inventory = {
            "prepared_at": "2001-02-03T04:05:06+00:00",
            "infrastructure_superuser_roles": topology.infrastructure_superuser_roles,
        }
        rollback_sql = "SELECT 1;\n"
        evidence_writer(inventory, rollback_sql)
        committed = True
        return pg_roles.BootstrapReceipt(
            database_name="postgres",
            session_user="postgres",
            topology={"infrastructure_superuser_roles": ("postgres",)},
            inventory=inventory,
            effective_connect_grantees=(),
            rollback_sql=rollback_sql,
            escalation_required=False,
            bootstrapped_at="2001-02-03T04:05:06+00:00",
        )

    monkeypatch.setattr(module.psycopg, "connect", lambda *_args, **_kwargs: FakeConnection())
    monkeypatch.setattr(module, "bootstrap_database_roles", fake_bootstrap)
    monkeypatch.setattr(module, "_replace_durable", lambda *_args: (_ for _ in ()).throw(OSError("crash")))
    monkeypatch.setenv("APPLYPILOT_ADMIN_PG_DSN", "secret-admin-dsn")
    monkeypatch.setenv("APPLYPILOT_CONTROLLER_PG_PASSWORD", "secret-controller-password")
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "bootstrap-fleet-pg-roles.py",
            "--database-owner-role",
            "owner",
            "--controller-role",
            "controller",
            "--verifier-role",
            "verifier",
            "--migrator-role",
            "migrator",
            "--retired-admin-role",
            "legacy",
            "--infrastructure-superuser-role",
            "postgres",
            "--receipt-path",
            str(receipt_path),
            "--rollback-sql",
            str(rollback_path),
        ],
    )
    with pytest.raises(OSError, match="crash"):
        module.main()
    assert committed is True
    prepared = json.loads(receipt_path.read_text(encoding="utf-8"))
    assert prepared["status"] == "prepared_before_database_mutation"
    assert prepared["escalation_required"] is True
    assert prepared["in_doubt"] is True
    assert "secret" not in receipt_path.read_text(encoding="utf-8")


def _dsn(base: str, *, user: str, password: str) -> str:
    params = conninfo_to_dict(base)
    params.update(user=user, password=password)
    return make_conninfo(**params)


def test_live_bootstrap_hands_off_to_non_superuser_controller_and_reconciles(
    fleet_db: str, tmp_path: Path
) -> None:
    rollback_path = tmp_path / "bootstrap-rollback.sql"
    receipt_path = tmp_path / "bootstrap-receipt.json"
    with pgqueue.connect(fleet_db) as root:
        root.execute(
            sql.SQL(
                "CREATE ROLE {} LOGIN SUPERUSER CREATEDB CREATEROLE REPLICATION BYPASSRLS"
            ).format(sql.Identifier(RETIRED_ADMIN))
        )
        root.commit()

        def durable_evidence(inventory, rollback_sql: str) -> None:
            with rollback_path.open("x", encoding="utf-8") as stream:
                stream.write(rollback_sql)
                stream.flush()
                os.fsync(stream.fileno())
            with receipt_path.open("x", encoding="utf-8") as stream:
                stream.write(str(inventory["prepared_at"]))
                stream.flush()
                os.fsync(stream.fileno())

        try:
            receipt = pg_roles.bootstrap_database_roles(
                root,
                CONTROLLER_PASSWORD,
                topology=pg_roles.BootstrapTopology(
                    database_owner_role=OWNER,
                    controller_role=CONTROLLER,
                    verifier_role=VERIFIER,
                    migrator_role=MIGRATOR,
                    retired_admin_roles=(RETIRED_ADMIN,),
                    infrastructure_superuser_roles=("postgres",),
                ),
                evidence_writer=durable_evidence,
            )
            assert receipt.escalation_required is False
            assert rollback_path.stat().st_size > 0
            assert receipt_path.stat().st_size > 0
            postgres_effective = next(
                row for row in receipt.effective_connect_grantees if row["role_name"] == "postgres"
            )
            assert postgres_effective["reconnect_capable"] is True
            assert postgres_effective["superuser"] is True

            controller_dsn = _dsn(fleet_db, user=CONTROLLER, password=CONTROLLER_PASSWORD)
            with psycopg.connect(controller_dsn, row_factory=dict_row) as controller:
                controller.execute(
                    "INSERT INTO public.workers(worker_id,machine_owner,public_ip,validated,capabilities) "
                    "VALUES(%s,'bootstrap','1.1.1.1',TRUE,'{}'::jsonb)",
                    (WORKER_ID,),
                )
                controller.execute(
                    "INSERT INTO public.worker_heartbeat(worker_id,machine_owner,home_ip,role,state,sw_version,last_beat) "
                    "VALUES(%s,'bootstrap','1.1.1.1','compute','idle','v1',now())",
                    (WORKER_ID,),
                )
                controller.commit()
                worker_receipt = pg_roles.ensure_fleet_worker_role(
                    controller,
                    WORKER_PASSWORD,
                    role=WORKER_ROLE,
                    worker_id=WORKER_ID,
                    contract="compute",
                    regrant_manifest=pg_roles.RegrantManifest(
                        database_owner_role=OWNER,
                        controller_roles=(CONTROLLER,),
                        verifier_roles=(VERIFIER,),
                        retired_admin_roles=(RETIRED_ADMIN,),
                        infrastructure_superuser_roles=("postgres",),
                        expected_service_roles=("postgres",),
                    ),
                    evidence_writer=lambda inventory, rollback: (
                        inventory["prepared_at"],
                        rollback,
                    ),
                )
                assert worker_receipt.contract == "compute"
                attributes = controller.execute(
                    "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                    "FROM pg_roles WHERE rolname=%s",
                    (CONTROLLER,),
                ).fetchone()
                assert attributes == {
                    "rolsuper": False,
                    "rolcreatedb": False,
                    "rolcreaterole": True,
                    "rolreplication": False,
                    "rolbypassrls": False,
                }
                retired = controller.execute(
                    "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                    "FROM pg_roles WHERE rolname=%s",
                    (RETIRED_ADMIN,),
                ).fetchone()
                assert not any(retired.values())

            root.execute(worker_receipt.rollback_sql)
            root.commit()
            root.execute(receipt.rollback_sql)
            root.commit()
            database_owner = root.execute(
                "SELECT owner.rolname FROM pg_database d JOIN pg_roles owner ON owner.oid=d.datdba "
                "WHERE d.datname=current_database()"
            ).fetchone()["rolname"]
            assert database_owner == "postgres"
            for role_name in (CONTROLLER, VERIFIER, MIGRATOR, OWNER):
                assert root.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role_name,)).fetchone() is None
            restored = root.execute(
                "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                "FROM pg_roles WHERE rolname=%s",
                (RETIRED_ADMIN,),
            ).fetchone()
            assert all(restored.values())
        finally:
            root.rollback()
            root.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO postgres").format(
                    sql.Identifier(conninfo_to_dict(fleet_db)["dbname"])
                )
            )
            pg_roles._transfer_application_ownership(root.cursor(), new_owner_role="postgres")
            root.execute("DELETE FROM public.fleet_worker_principals WHERE role_name=%s", (WORKER_ROLE,))
            for role_name in (WORKER_ROLE, CONTROLLER, VERIFIER, MIGRATOR, OWNER, RETIRED_ADMIN):
                if root.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role_name,)).fetchone():
                    root.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role_name)))
                    root.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role_name)))
            root.commit()
