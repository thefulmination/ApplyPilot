from __future__ import annotations

import importlib.util
from pathlib import Path
import uuid

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import dict_row

from applypilot.brain import schema
from applypilot.fleet import pg_roles


def _bootstrap_script_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "bootstrap-fleet-pg-roles.py"
    specification = importlib.util.spec_from_file_location("bootstrap_fleet_pg_roles", script)
    assert specification is not None and specification.loader is not None
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _bootstrap_topology() -> pg_roles.BootstrapTopology:
    return pg_roles.BootstrapTopology(
        database_owner_role="postgres",
        controller_role="brain_policy_controller",
        verifier_role="brain_schema_verifier",
        migrator_role="brain_schema_migrator",
        retired_admin_roles=(),
        infrastructure_superuser_roles=("postgres",),
    )


WRITER = "brain_candidate_writer_login"
READER = "brain_candidate_reader_login"
HASHES = tuple(character * 64 for character in "abcdef0123456789")


def _dsn_for(dsn: str, role: str) -> str:
    return make_conninfo(dsn, user=role)


def _set_password(conn, dsn: str, role: str) -> None:
    password = conninfo_to_dict(dsn).get("password")
    if password:
        conn.execute(sql.SQL("ALTER ROLE {} PASSWORD {}").format(sql.Identifier(role), sql.Literal(password)))


@pytest.fixture
def authority_pg(fleet_db):
    with psycopg.connect(fleet_db, row_factory=dict_row) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS public.fleet_desired_state(machine_owner text primary key)")
        for column in (
            "ats_canary_worker_id", "ats_canary_version", "linkedin_canary_worker_id", "linkedin_canary_version",
        ):
            conn.execute(f"ALTER TABLE public.fleet_config ADD COLUMN IF NOT EXISTS {column} text")
        for role in (
            "brain_schema_migrator",
            "brain_status_reader",
            "brain_policy_controller",
            "brain_candidate_reader",
            "brain_candidate_writer",
        ):
            conn.execute(
                sql.SQL(
                    "DO $$ BEGIN CREATE ROLE {} NOLOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE "
                    "NOREPLICATION NOBYPASSRLS; EXCEPTION WHEN duplicate_object THEN NULL; END $$"
                ).format(sql.Identifier(role))
            )
        conn.execute(
            "DO $$ BEGIN CREATE ROLE brain_schema_verifier LOGIN; "
            "EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        _set_password(conn, fleet_db, "brain_schema_verifier")
        conn.execute("GRANT brain_schema_migrator TO postgres")
        conn.execute("GRANT USAGE, CREATE ON SCHEMA public TO brain_schema_migrator")
        conn.execute("GRANT CREATE ON DATABASE postgres TO brain_schema_migrator")
        conn.execute("GRANT SELECT ON ALL TABLES IN SCHEMA public TO brain_schema_migrator WITH GRANT OPTION")
        for relation in (
            "fleet_config", "fleet_decision_policies", "apply_queue", "linkedin_queue", "workers",
            "worker_heartbeat", "fleet_worker_principals", "fleet_desired_state", "rate_governor",
        ):
            conn.execute(
                sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE public.{} TO brain_schema_migrator WITH GRANT OPTION")
                .format(sql.Identifier(relation))
            )
        for login, capability in ((WRITER, "brain_candidate_writer"), (READER, "brain_candidate_reader")):
            conn.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(login)))
            conn.execute(
                sql.SQL("CREATE ROLE {} LOGIN INHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS")
                .format(sql.Identifier(login))
            )
            _set_password(conn, fleet_db, login)
            conn.execute(sql.SQL("GRANT {} TO {}").format(sql.Identifier(capability), sql.Identifier(login)))
        conn.commit()
        schema.ensure_brain_schema_v4(conn)
        conn.commit()
    yield fleet_db
    with psycopg.connect(fleet_db, row_factory=dict_row) as conn:
        conn.execute("DROP SCHEMA IF EXISTS brain_archive CASCADE")
        relations = conn.execute(
            "SELECT c.relname,c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND left(c.relname,6)='brain_' "
            "AND c.relkind IN ('r','p','v','m','f') ORDER BY CASE WHEN c.relkind IN ('v','m') THEN 0 ELSE 1 END"
        ).fetchall()
        for relation in relations:
            object_type = {
                "v": sql.SQL("VIEW"),
                "m": sql.SQL("MATERIALIZED VIEW"),
                "f": sql.SQL("FOREIGN TABLE"),
            }.get(relation["relkind"], sql.SQL("TABLE"))
            conn.execute(
                sql.SQL("DROP {} IF EXISTS public.{} CASCADE").format(
                    object_type,
                    sql.Identifier(relation["relname"]),
                )
            )
        functions = conn.execute(
            "SELECT p.proname,pg_get_function_identity_arguments(p.oid) AS arguments FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname='public' AND left(p.proname,6)='brain_'"
        ).fetchall()
        for function in functions:
            conn.execute(sql.SQL("DROP FUNCTION public.{}({}) CASCADE").format(
                sql.Identifier(function["proname"]), sql.SQL(function["arguments"])
            ))
        for login in (WRITER, READER):
            conn.execute(sql.SQL("DROP OWNED BY {}; DROP ROLE {}").format(sql.Identifier(login), sql.Identifier(login)))
        conn.commit()


def _seed_authority(conn, *, predecessor_deny: bool = True):
    conn.execute("SET session_replication_role=replica")
    conn.execute("SET ROLE brain_schema_migrator")
    for index, artifact_hash in enumerate(HASHES[:6]):
        conn.execute(
            "INSERT INTO public.brain_artifacts(request_id,artifact_hash,media_type,byte_length,schema_version,location) "
            "VALUES(%s,%s,'application/json',1,1,'test') ON CONFLICT DO NOTHING",
            (f"v4-artifact-{index}", artifact_hash),
        )
    source_id = conn.execute(
        "INSERT INTO public.brain_migration_sources(source_namespace,source_fingerprint,byte_length,schema_metadata) "
        "VALUES('v4','a' || repeat('0',63),1,'{}'::jsonb) RETURNING migration_source_id"
    ).fetchone()["migration_source_id"]
    run_id = conn.execute(
        "INSERT INTO public.brain_migration_runs(migration_source_id,source_namespace,run_key) "
        "VALUES(%s,'v4','run') RETURNING migration_run_id", (source_id,)
    ).fetchone()["migration_run_id"]
    started = conn.execute(
        "INSERT INTO public.brain_migration_run_events(migration_run_id,source_namespace,event_type,actor_id) "
        "VALUES(%s,'v4','started','owner') RETURNING migration_run_event_id", (run_id,)
    ).fetchone()["migration_run_event_id"]
    completed = conn.execute(
        "INSERT INTO public.brain_migration_run_events(migration_run_id,source_namespace,event_type,actor_id,supersedes_run_event_id) "
        "VALUES(%s,'v4','completed','owner',%s) RETURNING migration_run_event_id", (run_id, started)
    ).fetchone()["migration_run_event_id"]
    parity_run_id = conn.execute(
        "INSERT INTO public.brain_parity_runs(migration_run_id,source_namespace,definition_version,completed_run_event_id,"
        "report_artifact_hash,final_delta_receipt_hash,writer_freeze_receipt_hash,started_at) "
        "VALUES(%s,'v4',1,%s,%s,%s,%s,now()) RETURNING parity_run_id",
        (run_id, completed, HASHES[0], HASHES[1], HASHES[2]),
    ).fetchone()["parity_run_id"]
    incarnation = uuid.uuid4()
    scope_id = conn.execute(
        "INSERT INTO public.brain_authority_scope_state("
        "owner_id,campaign_id,recommendation_lane,execution_channel,execution_scope,authority_epoch,database_incarnation_id,"
        "migration_run_id,source_namespace,parity_run_id,definition_version,report_artifact_hash) "
        "VALUES('owner-a','campaign-a','core_fit','ats','host:example.test',7,%s,%s,'v4',%s,1,%s) "
        "RETURNING authority_scope_id",
        (incarnation, run_id, parity_run_id, HASHES[0]),
    ).fetchone()["authority_scope_id"]
    conn.execute(
        "INSERT INTO public.brain_authority_transition_events(authority_scope_id,event_type,authority_epoch,database_incarnation_id,actor_id) "
        "VALUES(%s,'granted',7,%s,'owner')", (scope_id, incarnation)
    )
    receipt_id = conn.execute(
        "INSERT INTO public.brain_graph_approval_receipts(authority_scope_id,authority_epoch,database_incarnation_id,"
        "graph_snapshot_id,approval_state,approval_artifact_hash,predecessor_deny_receipt_hash) "
        "VALUES(%s,7,%s,'graph-snapshot-a','approved',%s,%s) RETURNING graph_approval_receipt_id",
        (scope_id, incarnation, HASHES[3], HASHES[4] if predecessor_deny else None),
    ).fetchone()["graph_approval_receipt_id"]
    if predecessor_deny:
        conn.execute(
            "INSERT INTO public.brain_immutable_artifact_references(artifact_hash,reference_type,subject_id) "
            "VALUES(%s,'predecessor_deny_receipt','graph-snapshot-a')", (HASHES[4],)
        )
    conn.execute("RESET ROLE")
    conn.execute("SET session_replication_role=origin")
    conn.commit()
    return {"incarnation": incarnation, "receipt_id": receipt_id, "scope_id": scope_id}


def _publish(
    conn,
    seed,
    *,
    epoch: int = 7,
    incarnation=None,
    receipt_id=None,
    candidate_id: str = "decision-a",
    semantic_hash: str = "b" * 64,
    envelope_id: str = "envelope-a",
):
    return conn.execute(
        "SELECT public.brain_publish_v4_candidate("
        "'owner-a','campaign-a','core_fit','ats','host:example.test',%s,%s,"
        "%s,%s,%s,%s,%s,%s) AS candidate_id",
        (
            epoch,
            incarnation or seed["incarnation"],
            candidate_id,
            semantic_hash,
            HASHES[3],
            envelope_id,
            HASHES[5],
            receipt_id or seed["receipt_id"],
        ),
    ).fetchone()["candidate_id"]


def _assert_42501(conn, statement: str) -> None:
    with pytest.raises(psycopg.errors.InsufficientPrivilege) as error:
        conn.execute(statement)
    assert error.value.sqlstate == "42501"
    conn.rollback()


def test_candidate_writer_is_limited_to_bounded_publish(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        seed = _seed_authority(owner)
    with psycopg.connect(_dsn_for(authority_pg, WRITER), row_factory=dict_row) as writer:
        _assert_42501(writer, "INSERT INTO public.brain_v4_candidate_decisions(candidate_decision_id) VALUES('forged')")
        _assert_42501(writer, "CREATE TABLE public.writer_rogue(id integer)")
        _assert_42501(writer, "SELECT public.brain_controller_transition_policy('p','active','ats')")
        _assert_42501(writer, "INSERT INTO public.apply_queue(url) VALUES('https://forged.test')")
        _assert_42501(writer, "INSERT INTO public.brain_graph_approval_receipts(authority_scope_id,authority_epoch,database_incarnation_id,graph_snapshot_id,approval_state,approval_artifact_hash) VALUES(1,1,'00000000-0000-0000-0000-000000000000','x','approved',repeat('0',64))")
        _assert_42501(writer, "SELECT public.fleet_worker_lease_ats('x','x',1,'x',1)")
        assert _publish(writer, seed) == "decision-a"
        writer.commit()
        with pytest.raises(psycopg.errors.UniqueViolation):
            _publish(
                writer,
                seed,
                candidate_id="decision-b",
                semantic_hash="c" * 64,
                envelope_id="envelope-b",
            )
        writer.rollback()
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        assert owner.execute(
            "SELECT count(*) AS count FROM public.brain_graph_approval_consumptions"
        ).fetchone()["count"] == 1


def test_candidate_reconcile_removes_preexisting_memberships_and_authority(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        seed = _seed_authority(owner)
        owner.execute("CREATE ROLE candidate_authority_parent NOLOGIN")
        owner.execute("GRANT candidate_authority_parent TO brain_candidate_writer")
        owner.execute(
            "GRANT INSERT ON public.apply_queue TO candidate_authority_parent, brain_candidate_writer"
        )
        owner.execute(
            "GRANT EXECUTE ON FUNCTION public.fleet_worker_lease_ats(TEXT,TEXT,INTEGER,TEXT,INTEGER) "
            "TO candidate_authority_parent, brain_candidate_writer"
        )
        owner.commit()
        with pytest.raises(RuntimeError) as verification_error:
            schema.verify_brain_schema_v4(owner)
        assert "candidate role memberships retained" in str(verification_error.value)
        assert "candidate table ACL contract mismatch" in str(verification_error.value)
        assert "candidate function ACL contract mismatch" in str(verification_error.value)
        pg_roles.ensure_brain_candidate_roles(
            owner,
            reader_approved_grantees=(READER,),
            writer_approved_grantees=(WRITER,),
        )
        schema.verify_brain_schema_v4(owner)
    with psycopg.connect(_dsn_for(authority_pg, WRITER), row_factory=dict_row) as writer:
        _assert_42501(writer, "INSERT INTO public.apply_queue(url) VALUES('https://forged.test')")
        _assert_42501(writer, "SELECT public.fleet_worker_lease_ats('x','x',1,'x',1)")
        assert _publish(writer, seed) == "decision-a"
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        assert owner.execute(
            "SELECT count(*) AS count FROM pg_auth_members membership JOIN pg_roles member ON member.oid=membership.member "
            "WHERE member.rolname='brain_candidate_writer'"
        ).fetchone()["count"] == 0


def test_authority_scope_public_acl_and_replay_guards(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        seed = _seed_authority(owner, predecessor_deny=False)
        with pytest.raises(psycopg.errors.UniqueViolation):
            owner.execute(
                "INSERT INTO public.brain_authority_scope_state("
                "owner_id,campaign_id,recommendation_lane,execution_channel,execution_scope,authority_epoch,database_incarnation_id,"
                "migration_run_id,source_namespace,parity_run_id,definition_version,report_artifact_hash) "
                "SELECT owner_id,campaign_id,recommendation_lane,execution_channel,execution_scope,8,database_incarnation_id,"
                "migration_run_id,source_namespace,parity_run_id,definition_version,report_artifact_hash "
                "FROM public.brain_authority_scope_state WHERE authority_scope_id=%s", (seed["scope_id"],)
            )
        owner.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            owner.execute(
                "INSERT INTO public.brain_authority_scope_state("
                "owner_id,campaign_id,recommendation_lane,execution_channel,execution_scope,authority_epoch,database_incarnation_id,"
                "migration_run_id,source_namespace,parity_run_id,definition_version,report_artifact_hash) "
                "SELECT owner_id,campaign_id,recommendation_lane,execution_channel,'global',8,database_incarnation_id,"
                "migration_run_id,source_namespace,parity_run_id,definition_version,report_artifact_hash "
                "FROM public.brain_authority_scope_state WHERE authority_scope_id=%s", (seed["scope_id"],)
            )
        owner.rollback()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            owner.execute(
                "INSERT INTO public.brain_authority_scope_state("
                "owner_id,campaign_id,recommendation_lane,execution_channel,execution_scope,authority_epoch,database_incarnation_id,"
                "migration_run_id,source_namespace,parity_run_id,definition_version,report_artifact_hash) "
                "SELECT owner_id,campaign_id,recommendation_lane,execution_channel,'host:bad-lineage',8,database_incarnation_id,"
                "migration_run_id,source_namespace,parity_run_id,2,report_artifact_hash "
                "FROM public.brain_authority_scope_state WHERE authority_scope_id=%s", (seed["scope_id"],)
            )
        owner.rollback()
        for table in (
            "brain_authority_scope_state", "brain_authority_transition_events", "brain_v4_candidate_decisions",
            "brain_v4_decision_envelopes", "brain_graph_approval_receipts", "brain_graph_approval_consumptions",
            "brain_immutable_artifact_references",
        ):
            assert not owner.execute(
                "SELECT EXISTS (SELECT 1 FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) acl "
                "WHERE c.oid=%s::regclass AND acl.grantee=0) AS allowed",
                (f"public.{table}",),
            ).fetchone()["allowed"]
        table_acls = owner.execute(
            "SELECT n.nspname,c.relname,role.rolname AS grantee,acl.privilege_type "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "CROSS JOIN LATERAL aclexplode(c.relacl) acl JOIN pg_roles role ON role.oid=acl.grantee "
            "WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_' "
            "AND c.relkind IN ('r','p','v','m','f') AND acl.grantee IN ("
            "SELECT oid FROM pg_roles WHERE rolname IN ('brain_candidate_reader','brain_candidate_writer'))",
        ).fetchall()
        assert {(row["nspname"], row["relname"], row["grantee"], row["privilege_type"]) for row in table_acls} == {
            ("public", "brain_authority_scope_state", "brain_candidate_reader", "SELECT"),
            ("public", "brain_v4_candidate_decisions", "brain_candidate_reader", "SELECT"),
            ("public", "brain_v4_decision_envelopes", "brain_candidate_reader", "SELECT"),
            ("public", "brain_immutable_artifact_references", "brain_candidate_reader", "SELECT"),
        }
        function_acls = owner.execute(
            "SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) AS arguments,"
            "role.rolname AS grantee,acl.privilege_type FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "CROSS JOIN LATERAL aclexplode(p.proacl) acl JOIN pg_roles role ON role.oid=acl.grantee "
            "WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_' AND acl.grantee IN ("
            "SELECT oid FROM pg_roles WHERE rolname IN ('brain_candidate_reader','brain_candidate_writer'))"
        ).fetchall()
        assert {
            (row["nspname"], row["proname"], row["arguments"], row["grantee"], row["privilege_type"])
            for row in function_acls
        } == {
            (
                "public",
                "brain_publish_v4_candidate",
                "requested_owner_id text, requested_campaign_id text, requested_recommendation_lane text, "
                "requested_execution_channel text, requested_execution_scope text, requested_authority_epoch bigint, "
                "requested_database_incarnation_id uuid, requested_candidate_decision_id text, "
                "requested_semantic_content_hash text, requested_candidate_artifact_hash text, "
                "requested_envelope_id text, requested_envelope_artifact_hash text, "
                "requested_graph_approval_receipt_id bigint",
                "brain_candidate_writer",
                "EXECUTE",
            ),
        }
        assert not owner.execute(
            "SELECT EXISTS (SELECT 1 FROM pg_proc p CROSS JOIN LATERAL aclexplode(p.proacl) acl "
            "WHERE p.oid='public.brain_publish_v4_candidate(text,text,text,text,text,bigint,uuid,text,text,text,text,text,bigint)'::regprocedure "
            "AND acl.grantee=0) AS allowed"
        ).fetchone()["allowed"]
    with psycopg.connect(_dsn_for(authority_pg, WRITER), row_factory=dict_row) as writer:
        with pytest.raises(psycopg.Error, match="authority epoch"):
            _publish(writer, seed, epoch=6)
        writer.rollback()
        with pytest.raises(psycopg.Error, match="database incarnation"):
            _publish(writer, seed, incarnation=uuid.uuid4())
        writer.rollback()
        with pytest.raises(psycopg.Error, match="predecessor deny receipt"):
            _publish(writer, seed)


def test_published_artifact_references_are_durable_and_immutable(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        seed = _seed_authority(owner)
    with psycopg.connect(_dsn_for(authority_pg, WRITER), row_factory=dict_row) as writer:
        assert _publish(writer, seed) == "decision-a"
        writer.commit()
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        rows = owner.execute(
            "SELECT reference_type,subject_id FROM public.brain_immutable_artifact_references "
            "WHERE subject_id IN ('decision-a','envelope-a','graph-snapshot-a') ORDER BY reference_type,subject_id"
        ).fetchall()
        assert {(row["reference_type"], row["subject_id"]) for row in rows} == {
            ("candidate_payload", "decision-a"),
            ("decision_envelope", "envelope-a"),
            ("graph_approval_receipt", "graph-snapshot-a"),
            ("predecessor_deny_receipt", "graph-snapshot-a"),
        }
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            owner.execute(
                "UPDATE public.brain_immutable_artifact_references SET subject_id='forged' "
                "WHERE reference_type='candidate_payload'"
            )
        owner.rollback()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState):
            owner.execute("DELETE FROM public.brain_immutable_artifact_references WHERE reference_type='candidate_payload'")


def test_v4_verification_rejects_dropped_immutable_trigger(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        owner.execute(
            "DROP TRIGGER brain_immutable_artifact_references_immutable "
            "ON public.brain_immutable_artifact_references"
        )
        owner.commit()
        with pytest.raises(RuntimeError, match="v4 immutable trigger"):
            schema.verify_brain_schema_v4(owner)


def test_v4_verification_rejects_weakened_publish_function(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        owner.execute("SET ROLE brain_schema_migrator")
        owner.execute(
            "CREATE OR REPLACE FUNCTION public.brain_publish_v4_candidate("
            "requested_owner_id TEXT,requested_campaign_id TEXT,requested_recommendation_lane TEXT,"
            "requested_execution_channel TEXT,requested_execution_scope TEXT,requested_authority_epoch BIGINT,"
            "requested_database_incarnation_id UUID,requested_candidate_decision_id TEXT,"
            "requested_semantic_content_hash TEXT,requested_candidate_artifact_hash TEXT,"
            "requested_envelope_id TEXT,requested_envelope_artifact_hash TEXT,"
            "requested_graph_approval_receipt_id BIGINT) RETURNS TEXT LANGUAGE plpgsql AS $$ "
            "BEGIN RETURN requested_candidate_decision_id; END; $$"
        )
        owner.execute(
            "ALTER FUNCTION public.brain_publish_v4_candidate("
            "TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT,UUID,TEXT,TEXT,TEXT,TEXT,TEXT,BIGINT) "
            "SECURITY INVOKER RESET ALL"
        )
        owner.execute("RESET ROLE")
        owner.commit()
        with pytest.raises(RuntimeError, match="candidate publish procedure"):
            schema.verify_brain_schema_v4(owner)


def test_v4_verification_rejects_weakened_scope_constraint(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        owner.execute(
            "ALTER TABLE public.brain_authority_scope_state "
            "DROP CONSTRAINT brain_authority_scope_state_execution_scope_check"
        )
        owner.commit()
        with pytest.raises(RuntimeError, match="authority scope constraint"):
            schema.verify_brain_schema_v4(owner)


def test_v4_verification_rejects_tampered_column_and_lineage_foreign_key(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        owner.execute(
            "ALTER TABLE public.brain_authority_scope_state ALTER COLUMN report_artifact_hash DROP NOT NULL"
        )
        owner.execute(
            "ALTER TABLE public.brain_authority_scope_state "
            "DROP CONSTRAINT brain_authority_scope_state_parity_fk"
        )
        owner.commit()
        with pytest.raises(RuntimeError) as verification_error:
            schema.verify_brain_schema_v4(owner)
        assert "v4 column contract mismatch" in str(verification_error.value)
        assert "v4 foreign-key lineage contract mismatch" in str(verification_error.value)


def test_v4_verification_rejects_effective_public_table_authority(authority_pg):
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        owner.execute("GRANT SELECT ON TABLE public.apply_queue TO PUBLIC")
        owner.commit()
        with pytest.raises(RuntimeError, match="effective table authority exposure.*apply_queue"):
            with owner.transaction():
                with owner.cursor() as cur:
                    schema._verify_v4_contract(cur)
        owner.execute("REVOKE SELECT ON TABLE public.apply_queue FROM PUBLIC")
        owner.commit()
        schema.verify_brain_schema_v4(owner)


def test_v4_verification_rejects_effective_public_fleet_authority(authority_pg):
    _bootstrap_script_module()
    with psycopg.connect(authority_pg, row_factory=dict_row) as owner:
        owner.execute(
            "GRANT EXECUTE ON FUNCTION public.fleet_worker_lease_ats(TEXT,TEXT,INTEGER,TEXT,INTEGER) TO PUBLIC"
        )
        owner.commit()
        with pytest.raises(RuntimeError, match="effective function authority exposure.*fleet_worker_lease_ats"):
            schema.verify_brain_schema_v4(owner)
        with pytest.raises(RuntimeError, match="effective function authority exposure.*fleet_worker_lease_ats"):
            with owner.transaction():
                with owner.cursor() as cur:
                    schema.ensure_brain_schema_v5_in_transaction(cur)
        owner.execute(
            "REVOKE EXECUTE ON FUNCTION public.fleet_worker_lease_ats(TEXT,TEXT,INTEGER,TEXT,INTEGER) FROM PUBLIC"
        )
        owner.commit()
        with owner.transaction():
            with owner.cursor() as cur:
                schema.ensure_brain_schema_v5_in_transaction(cur)
        schema.verify_brain_schema_v5(owner)


def test_v4_migration_bytes_have_fixed_committed_checksum():
    assert schema._schema_v4_checksum() == schema._EXPECTED_V4_CHECKSUM
