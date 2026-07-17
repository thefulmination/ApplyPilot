from __future__ import annotations

from pathlib import Path
import uuid

import pytest

psycopg = pytest.importorskip("psycopg")
from psycopg import sql  # noqa: E402
from psycopg.conninfo import conninfo_to_dict, make_conninfo  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402

from applypilot.apply import pgqueue  # noqa: E402


REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = (
    REPO_ROOT
    / "src"
    / "applypilot"
    / "fleet"
    / "migrations"
    / "20260717_002_lane_specific_canary_pins.sql"
)
WORKER_APIS = (
    "fleet_worker_lease_ats(text,text,integer,text,integer)",
    "fleet_worker_lease_linkedin(text,text,text,integer,text)",
    "fleet_worker_admission_snapshot()",
    "fleet_worker_version_status(text,text)",
    "fleet_worker_lease_compute()",
    "fleet_worker_lease_search()",
)


@pytest.fixture
def lane_canary_db(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        conn.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
        conn.execute(
            "UPDATE public.fleet_config SET "
            "pinned_worker_version=NULL,canary_worker_id=NULL,canary_version=NULL,"
            "ats_canary_worker_id=NULL,ats_canary_version=NULL,"
            "linkedin_canary_worker_id=NULL,linkedin_canary_version=NULL WHERE id=1"
        )
        conn.commit()
    return fleet_db


def _expected(conn, worker_id: str, contract: str) -> str | None:
    return conn.execute(
        "SELECT public.fleet_worker_expected_version(%s,%s) AS version",
        (worker_id, contract),
    ).fetchone()["version"]


def _status(conn, worker_id: str, reported: str | None) -> dict:
    return conn.execute(
        "SELECT public.fleet_worker_version_status(%s,%s) AS status",
        (worker_id, reported),
    ).fetchone()["status"]


def test_ats_and_linkedin_pins_are_simultaneous_and_cross_lane_isolated(lane_canary_db):
    with pgqueue.connect(lane_canary_db) as conn:
        conn.execute(
            "UPDATE public.fleet_config SET pinned_worker_version='fleet-v1',"
            "ats_canary_worker_id='ats-worker',ats_canary_version='ats-v2',"
            "linkedin_canary_worker_id='linkedin-worker',linkedin_canary_version='linkedin-v3' "
            "WHERE id=1"
        )
        conn.execute(
            "INSERT INTO public.worker_heartbeat(worker_id,role,sw_version,last_beat) VALUES"
            "('ats-worker','apply','ats-v2',now()),"
            "('linkedin-worker','linkedin','linkedin-v3',now())"
        )

        assert _expected(conn, "ats-worker", "apply") == "ats-v2"
        assert _expected(conn, "linkedin-worker", "linkedin") == "linkedin-v3"
        assert _expected(conn, "ats-worker", "linkedin") == "fleet-v1"
        assert _expected(conn, "linkedin-worker", "apply") == "fleet-v1"
        assert _status(conn, "ats-worker", "ats-v2") == {
            "expected_version": "ats-v2",
            "matches": True,
            "sw_version": "ats-v2",
        }
        assert _status(conn, "linkedin-worker", "linkedin-v3") == {
            "expected_version": "linkedin-v3",
            "matches": True,
            "sw_version": "linkedin-v3",
        }


def test_generic_pin_remains_compute_and_discovery_staged_rollout(lane_canary_db):
    with pgqueue.connect(lane_canary_db) as conn:
        conn.execute(
            "UPDATE public.fleet_config SET pinned_worker_version='fleet-v1',"
            "canary_worker_id='generic-worker',canary_version='generic-v2',"
            "ats_canary_worker_id='generic-worker',ats_canary_version='ats-v3',"
            "linkedin_canary_worker_id='generic-worker',linkedin_canary_version='linkedin-v4' "
            "WHERE id=1"
        )
        conn.execute(
            "INSERT INTO public.worker_heartbeat(worker_id,role,sw_version,last_beat) "
            "VALUES('generic-worker','compute','generic-v2',now())"
        )

        assert _expected(conn, "generic-worker", "apply") == "ats-v3"
        assert _expected(conn, "generic-worker", "linkedin") == "linkedin-v4"
        assert _expected(conn, "generic-worker", "compute") == "generic-v2"
        assert _expected(conn, "generic-worker", "discovery") == "generic-v2"
        assert _status(conn, "generic-worker", "generic-v2")["matches"] is True

        conn.execute(
            "UPDATE public.worker_heartbeat SET role='discovery' WHERE worker_id='generic-worker'"
        )
        assert _status(conn, "generic-worker", "generic-v2")["matches"] is True
        assert _expected(conn, "ats-only-worker", "compute") == "fleet-v1"
        assert _expected(conn, "linkedin-only-worker", "discovery") == "fleet-v1"


def test_null_and_partial_lane_pins_fall_back_without_disabling_workers(lane_canary_db):
    with pgqueue.connect(lane_canary_db) as conn:
        row = conn.execute(
            "SELECT ats_canary_worker_id,ats_canary_version,"
            "linkedin_canary_worker_id,linkedin_canary_version FROM public.fleet_config WHERE id=1"
        ).fetchone()
        assert dict(row) == {
            "ats_canary_worker_id": None,
            "ats_canary_version": None,
            "linkedin_canary_worker_id": None,
            "linkedin_canary_version": None,
        }
        assert _expected(conn, "worker", "apply") is None
        assert _status(conn, "worker", "unversioned") == {
            "expected_version": None,
            "matches": True,
            "sw_version": "unversioned",
        }

        conn.execute(
            "UPDATE public.fleet_config SET pinned_worker_version='fleet-v1',"
            "ats_canary_worker_id='worker',ats_canary_version=NULL,"
            "linkedin_canary_worker_id=NULL,linkedin_canary_version='orphan-version' WHERE id=1"
        )
        assert _expected(conn, "worker", "apply") == "fleet-v1"
        assert _expected(conn, "worker", "linkedin") == "fleet-v1"

        conn.execute(
            "UPDATE public.fleet_config SET canary_worker_id='worker',canary_version='generic-v2' "
            "WHERE id=1"
        )
        assert _expected(conn, "worker", "apply") == "generic-v2"
        assert _expected(conn, "worker", "linkedin") == "generic-v2"


def test_worker_api_signatures_security_and_grants_survive_replacement(lane_canary_db):
    with pgqueue.connect(lane_canary_db) as conn:
        before = conn.execute(
            "SELECT p.oid::regprocedure::text AS signature,p.prosecdef,p.proconfig,p.proacl::text AS acl "
            "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND p.oid=ANY(%s::regprocedure[]) ORDER BY signature",
            (list(WORKER_APIS),),
        ).fetchall()
        conn.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
        after = conn.execute(
            "SELECT p.oid::regprocedure::text AS signature,p.prosecdef,p.proconfig,p.proacl::text AS acl,"
            "EXISTS(SELECT 1 FROM pg_catalog.aclexplode(p.proacl) item "
            "WHERE item.grantee=0 AND item.privilege_type='EXECUTE') AS public_execute,"
            "pg_catalog.pg_get_functiondef(p.oid) AS definition "
            "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND p.oid=ANY(%s::regprocedure[]) ORDER BY signature",
            (list(WORKER_APIS),),
        ).fetchall()

        assert [dict(row) for row in before] == [
            {key: row[key] for key in ("signature", "prosecdef", "proconfig", "acl")}
            for row in after
        ]
        assert len(after) == len(WORKER_APIS)
        for row in after:
            assert row["prosecdef"] is True
            assert row["proconfig"] == ["search_path=pg_catalog, public"]
            assert row["public_execute"] is False
            assert "fleet_worker_expected_version" in row["definition"]

        helper = conn.execute(
            "SELECT p.prosecdef,p.proconfig,p.proacl,"
            "EXISTS(SELECT 1 FROM pg_catalog.aclexplode(p.proacl) acl "
            "WHERE acl.grantee=0 AND acl.privilege_type='EXECUTE') AS public_execute "
            "FROM pg_catalog.pg_proc p "
            "WHERE p.oid='public.fleet_worker_expected_version(text,text)'::regprocedure"
        ).fetchone()
        assert helper["prosecdef"] is True
        assert helper["proconfig"] == ["search_path=pg_catalog, public"]
        assert helper["proacl"] is not None
        assert helper["public_execute"] is False


def test_non_superuser_api_owner_can_invoke_helper_without_exposing_it(lane_canary_db):
    suffix = uuid.uuid4().hex[:12]
    api_owner = f"lane_api_owner_{suffix}"
    worker_role = f"lane_worker_{suffix}"
    worker_id = f"lane-worker-{suffix}"
    password = f"lane-worker-password-{suffix}"
    owner_name = None

    try:
        with pgqueue.connect(lane_canary_db) as conn:
            owner_name = conn.execute("SELECT current_user AS name").fetchone()["name"]
            conn.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOINHERIT").format(sql.Identifier(api_owner)))
            conn.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(worker_role), sql.Literal(password))
            )
            conn.execute(
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(conninfo_to_dict(lane_canary_db)["dbname"]),
                    sql.Identifier(worker_role),
                )
            )
            conn.execute(
                "INSERT INTO public.workers(worker_id,machine_owner,validated) "
                "VALUES(%s,'lane-owner',TRUE)",
                (worker_id,),
            )
            conn.execute(
                "INSERT INTO public.worker_heartbeat(worker_id,machine_owner,role,state,sw_version,last_beat) "
                "VALUES(%s,'lane-owner','apply','idle','ats-v2',now())",
                (worker_id,),
            )
            conn.execute(
                "INSERT INTO public.fleet_worker_principals(role_name,worker_id,contract) "
                "VALUES(%s,%s,'apply')",
                (worker_role, worker_id),
            )
            conn.execute(
                "UPDATE public.fleet_config SET pinned_worker_version='fleet-v1',"
                "ats_canary_worker_id=%s,ats_canary_version='ats-v2' WHERE id=1",
                (worker_id,),
            )
            conn.execute(
                sql.SQL(
                    "ALTER FUNCTION public.fleet_worker_version_status(TEXT,TEXT) OWNER TO {}"
                ).format(sql.Identifier(api_owner))
            )
            conn.execute(
                sql.SQL(
                    "GRANT SELECT ON public.fleet_worker_principals,public.worker_heartbeat TO {}"
                ).format(sql.Identifier(api_owner))
            )
            conn.execute(
                sql.SQL(
                    "GRANT USAGE ON SCHEMA public TO {}; "
                    "GRANT EXECUTE ON FUNCTION public.fleet_worker_version_status(TEXT,TEXT) TO {}"
                ).format(sql.Identifier(worker_role), sql.Identifier(worker_role))
            )
            conn.execute(MIGRATION_PATH.read_text(encoding="utf-8"))
            metadata = conn.execute(
                "SELECT owner.rolname AS owner_name FROM pg_catalog.pg_proc function "
                "JOIN pg_catalog.pg_roles owner ON owner.oid=function.proowner "
                "WHERE function.oid='public.fleet_worker_version_status(text,text)'::regprocedure"
            ).fetchone()
            assert metadata["owner_name"] == api_owner
            conn.commit()

        params = conninfo_to_dict(lane_canary_db)
        params.update(user=worker_role, password=password)
        with psycopg.connect(make_conninfo(**params), row_factory=dict_row) as worker:
            status = worker.execute(
                "SELECT public.fleet_worker_version_status(NULL,NULL) AS status"
            ).fetchone()["status"]
            assert status == {
                "expected_version": "ats-v2",
                "matches": True,
                "sw_version": "ats-v2",
            }
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                worker.execute(
                    "SELECT public.fleet_worker_expected_version(%s,'apply')",
                    (worker_id,),
                )
    finally:
        if owner_name is not None:
            with pgqueue.connect(lane_canary_db) as conn:
                conn.execute(
                    "DELETE FROM public.fleet_worker_principals WHERE role_name=%s",
                    (worker_role,),
                )
                conn.execute("DELETE FROM public.workers WHERE worker_id=%s", (worker_id,))
                conn.execute(
                    sql.SQL("REASSIGN OWNED BY {} TO {}").format(
                        sql.Identifier(api_owner), sql.Identifier(owner_name)
                    )
                )
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(api_owner)))
                conn.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(worker_role)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(worker_role)))
                conn.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(api_owner)))
                conn.commit()
