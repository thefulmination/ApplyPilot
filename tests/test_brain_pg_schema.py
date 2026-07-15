from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import psycopg
import pytest
from psycopg import sql as pg_sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row

from applypilot.brain import schema
from applypilot.fleet import schema as fleet_schema


AUTHORITY_TABLES = {
    "brain_schema_versions",
    "brain_artifacts",
    "brain_artifact_locations",
    "brain_jobs",
    "brain_job_aliases",
    "brain_job_observations",
    "brain_label_events",
    "brain_pairwise_events",
    "brain_email_events",
    "brain_reviewed_outcomes",
    "brain_applications",
    "brain_application_events",
    "brain_decision_policies",
    "brain_policy_artifacts",
    "brain_policy_approvals",
    "brain_policy_gate_definitions",
    "brain_policy_release_gate_events",
    "brain_policy_transition_receipts",
    "brain_policy_activation_receipts",
    "brain_decision_identities",
    "brain_job_decisions",
    "brain_job_decisions_default",
    "brain_migration_sources",
    "brain_migration_runs",
    "brain_migration_run_events",
    "brain_migration_batches",
    "brain_migration_batch_events",
    "brain_migration_checkpoints",
    "brain_migration_quarantine",
    "brain_parity_definitions",
    "brain_parity_runs",
    "brain_parity_results",
    "brain_parity_run_events",
}
FUNCTIONS = (
    "brain_reject_mutation",
    "brain_register_decision",
    "brain_check_policy_lifecycle",
    "brain_check_supersession",
    "brain_reject_default_decision",
    "brain_check_parity_pass",
    "brain_check_archive_manifest",
)
TEST_ROLES = (
    "brain_schema_reader",
    "brain_create_role",
    "brain_broad_role",
    "brain_proxy_owner",
    "fleet_worker",
)
HASHES = tuple(character * 64 for character in "abcdef0123456789")


def _cleanup(dsn: str) -> None:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        conn.execute("DROP SCHEMA IF EXISTS brain_archive CASCADE")
        conn.execute("DROP TABLE IF EXISTS public.brain_job_decisions CASCADE")
        relations = conn.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND left(c.relname,6)='brain_' "
            "AND c.relkind IN ('r','p','v','m','f') ORDER BY c.relname DESC"
        ).fetchall()
        for relation in relations:
            conn.execute(
                pg_sql.SQL("DROP TABLE IF EXISTS {}.{} CASCADE").format(
                    pg_sql.Identifier("public"), pg_sql.Identifier(relation["relname"])
                )
            )
        for table in sorted(AUTHORITY_TABLES, reverse=True):
            conn.execute(f"DROP TABLE IF EXISTS public.{table} CASCADE")
        functions = conn.execute(
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS arguments "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND left(p.proname,6)='brain_'"
        ).fetchall()
        for function in functions:
            conn.execute(
                pg_sql.SQL("DROP FUNCTION {}.{}({}) CASCADE").format(
                    pg_sql.Identifier("public"),
                    pg_sql.Identifier(function["proname"]),
                    pg_sql.SQL(function["arguments"]),
                )
            )
        for role in TEST_ROLES:
            if conn.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) AS present", (role,)).fetchone()[
                "present"
            ]:
                conn.execute(f"REVOKE ALL PRIVILEGES ON SCHEMA public FROM {role}")
                conn.execute(f"REVOKE CREATE ON DATABASE postgres FROM {role}")
                conn.execute(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM {role}"
                )
                conn.execute(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {role}"
                )
                conn.execute(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM {role}"
                )
                conn.execute(f"DROP OWNED BY {role}")
                conn.execute(f"DROP ROLE {role}")
        conn.execute("ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON TABLES FROM PUBLIC")
        conn.execute("ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON SEQUENCES FROM PUBLIC")
        conn.execute("ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public REVOKE ALL ON FUNCTIONS FROM PUBLIC")
        conn.commit()


@pytest.fixture
def brain_pg(fleet_pg):
    with psycopg.connect(fleet_pg, row_factory=dict_row) as conn:
        conn.execute(
            "DO $$ BEGIN CREATE ROLE brain_schema_migrator NOLOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        conn.execute(
            "DO $$ BEGIN CREATE ROLE brain_schema_verifier LOGIN; EXCEPTION WHEN duplicate_object THEN NULL; END $$"
        )
        _set_role_password(conn, fleet_pg, "brain_schema_verifier")
        conn.execute("GRANT brain_schema_migrator TO postgres")
        conn.execute("GRANT USAGE, CREATE ON SCHEMA public TO brain_schema_migrator")
        conn.execute("GRANT CREATE ON DATABASE postgres TO brain_schema_migrator")
        for relation in ("fleet_config", "fleet_decision_policies", "apply_queue", "linkedin_queue"):
            if conn.execute("SELECT to_regclass(%s) AS relation", (f"public.{relation}",)).fetchone()["relation"]:
                conn.execute(
                    pg_sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE {}.{} TO brain_schema_migrator").format(
                        pg_sql.Identifier("public"), pg_sql.Identifier(relation)
                    )
                )
        conn.commit()
    _cleanup(fleet_pg)
    yield fleet_pg
    _cleanup(fleet_pg)


def _dsn_for(dsn: str, role: str) -> str:
    return make_conninfo(dsn, user=role)


def _set_role_password(conn, dsn: str, role: str) -> None:
    password = conninfo_to_dict(dsn).get("password")
    if password:
        conn.execute(pg_sql.SQL("ALTER ROLE {} PASSWORD {}").format(pg_sql.Identifier(role), pg_sql.Literal(password)))


def _fleet_snapshot(conn):
    tables = [
        row["table_name"]
        for row in conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' AND table_name NOT LIKE 'brain_%' ORDER BY table_name"
        ).fetchall()
    ]
    rows = {}
    for table in tables:
        rows[table] = conn.execute(
            pg_sql.SQL(
                "SELECT COALESCE(jsonb_agg(value ORDER BY value::text), '[]'::jsonb) AS rows "
                "FROM (SELECT to_jsonb(t) AS value FROM {}.{} t) snapshot_rows"
            ).format(pg_sql.Identifier("public"), pg_sql.Identifier(table))
        ).fetchone()["rows"]
    acls = conn.execute(
        "SELECT c.relname, c.relacl::text AS acl FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' "
        "AND left(c.relname, 6) <> 'brain_' ORDER BY c.relname",
    ).fetchall()
    return tables, rows, acls


def _grant_fleet_controller_contract(conn) -> None:
    for relation in ("fleet_config", "fleet_decision_policies", "apply_queue", "linkedin_queue"):
        if conn.execute("SELECT to_regclass(%s) AS relation", (f"public.{relation}",)).fetchone()["relation"]:
            conn.execute(
                pg_sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE {}.{} TO brain_schema_migrator").format(
                    pg_sql.Identifier("public"), pg_sql.Identifier(relation)
                )
            )


def _install_fixture_data(conn) -> None:
    schema.ensure_schema_v1(conn)
    with conn.transaction():
        for artifact_hash in HASHES:
            conn.execute(
                "INSERT INTO brain_artifacts "
                "(request_id, artifact_hash, media_type, byte_length, schema_version, location) "
                "VALUES (%s, %s, 'application/json', 1, 1, %s)",
                (f"fixture-{artifact_hash}", artifact_hash, f"memory://{artifact_hash}"),
            )
        conn.execute(
            "INSERT INTO brain_jobs (job_id, source_namespace, source_job_id, title) VALUES "
            "('job-1', 'legacy', '1', 'Engineer'), ('job-2', 'legacy', '2', 'Analyst')"
        )


def _insert_policy(conn, version: str = "ats-v1", lane: str = "ats") -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO brain_decision_policies (policy_version, lane, lifecycle) VALUES (%s, %s, 'draft')",
            (version, lane),
        )


def _insert_decision(
    conn,
    *,
    decision_id: str = "decision-1",
    source_id: str = "source-decision-1",
    policy: str = "ats-v1",
    lane: str = "ats",
    job_id: str = "job-1",
    input_hash: str = HASHES[1],
    verdict: str = "qualified",
    action: str = "apply",
) -> None:
    with conn.transaction():
        conn.execute(
            "INSERT INTO brain_job_decisions ("
            "decision_id, source_namespace, source_decision_id, job_id, policy_version, lane, "
            "qualification_score, qualification_floor, preference_score, outcome_score, final_score, "
            "qualification_verdict, action, confidence, input_hash, uncertainty, blockers, "
            "requirements, evidence_nodes, title_signals, explanation, expires_at) "
            "VALUES (%s, 'test', %s, %s, %s, %s, 0.8, 0.6, 0.7, 0.9, 0.8, %s, %s, 0.8, %s, "
            "'[]', '[]', '[]', '[]', '[]', 'because', "
            "CASE WHEN %s = 'apply' THEN now() + interval '1 day' ELSE NULL END)",
            (decision_id, source_id, job_id, policy, lane, verdict, action, input_hash, action),
        )


def test_fresh_install_true_noop_and_fleet_unchanged(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        fleet_schema.ensure_schema_v3(conn)
        _grant_fleet_controller_contract(conn)
        conn.commit()
        fleet_before = _fleet_snapshot(conn)
        conn.rollback()

        schema.ensure_schema_v1(conn)
        conn.execute(
            "INSERT INTO brain_jobs (job_id, source_namespace, source_job_id) VALUES ('preserved', 'test', 'preserved')"
        )
        conn.commit()
        before = conn.execute(
            "SELECT applied_at, migration_checksum FROM brain_schema_versions WHERE version = 1"
        ).fetchone()
        acl_before = conn.execute("SELECT relacl FROM pg_class WHERE oid = 'public.brain_jobs'::regclass").fetchone()[
            "relacl"
        ]
        conn.rollback()

        schema.ensure_schema_v1(conn)

        after = conn.execute(
            "SELECT applied_at, migration_checksum FROM brain_schema_versions WHERE version = 1"
        ).fetchone()
        acl_after = conn.execute("SELECT relacl FROM pg_class WHERE oid = 'public.brain_jobs'::regclass").fetchone()[
            "relacl"
        ]
        assert len(after["migration_checksum"]) == 64
        assert before == after
        assert acl_before == acl_after
        assert conn.execute("SELECT count(*) AS n FROM brain_jobs").fetchone()["n"] == 1
        assert _fleet_snapshot(conn) == fleet_before


def test_failed_install_rolls_back_completely_and_retries(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        conn.execute("CREATE TABLE brain_artifacts (wrong_column integer)")
        conn.commit()
        with pytest.raises(RuntimeError, match="existing brain objects"):
            schema.ensure_schema_v1(conn)
        assert conn.info.transaction_status == TransactionStatus.IDLE
        assert conn.execute("SELECT to_regclass('public.brain_schema_versions') AS name").fetchone()["name"] is None
        conn.rollback()
        conn.execute("DROP TABLE brain_artifacts")
        conn.commit()
        schema.ensure_schema_v1(conn)
        assert conn.info.transaction_status == TransactionStatus.IDLE


def test_helpers_require_idle_and_timeout_leaves_connection_reusable(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        conn.execute("CREATE TEMP TABLE caller_work (value integer)")
        conn.execute("INSERT INTO caller_work VALUES (1)")
        with pytest.raises(RuntimeError, match="idle connection"):
            schema.ensure_schema_v1(conn)
        assert conn.execute("SELECT value FROM caller_work").fetchone()["value"] == 1
        conn.rollback()

    with psycopg.connect(brain_pg, row_factory=dict_row) as holder:
        holder.execute("SELECT pg_advisory_lock(hashtext('applypilot:brain:schema:v1'))")
        with psycopg.connect(brain_pg, row_factory=dict_row) as waiter:
            with pytest.raises(TimeoutError, match="migration lock"):
                schema.ensure_schema_v1(waiter, lock_timeout_seconds=0.05)
            assert waiter.info.transaction_status == TransactionStatus.IDLE
            assert waiter.execute("SELECT 1 AS value").fetchone()["value"] == 1
            waiter.rollback()
        holder.execute("SELECT pg_advisory_unlock(hashtext('applypilot:brain:schema:v1'))")


def test_generic_create_role_cannot_migrate(brain_pg, monkeypatch):
    monkeypatch.delenv("APPLYPILOT_BRAIN_MIGRATION_ROLE", raising=False)
    with psycopg.connect(brain_pg, row_factory=dict_row) as owner:
        owner.execute("CREATE ROLE brain_create_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        _set_role_password(owner, brain_pg, "brain_create_role")
        owner.execute("GRANT USAGE, CREATE ON SCHEMA public TO brain_create_role")
        owner.execute("GRANT CREATE ON DATABASE postgres TO brain_create_role")
        owner.commit()
    with psycopg.connect(_dsn_for(brain_pg, "brain_create_role"), row_factory=dict_row) as conn:
        with pytest.raises(RuntimeError, match="migration requires"):
            schema.ensure_schema_v1(conn)
        assert conn.info.transaction_status == TransactionStatus.IDLE
    with psycopg.connect(brain_pg, row_factory=dict_row) as owner:
        assert owner.execute("SELECT to_regclass('public.brain_schema_versions') AS name").fetchone()["name"] is None


def test_exact_configured_migration_role_is_allowed(brain_pg, monkeypatch):
    with psycopg.connect(brain_pg, row_factory=dict_row) as owner:
        owner.execute("CREATE ROLE brain_schema_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        _set_role_password(owner, brain_pg, "brain_schema_reader")
        owner.execute("GRANT brain_schema_migrator TO brain_schema_reader")
        owner.commit()
    monkeypatch.setenv("APPLYPILOT_BRAIN_MIGRATION_ROLE", "brain_schema_reader")
    with psycopg.connect(_dsn_for(brain_pg, "brain_schema_reader"), row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        assert conn.info.transaction_status == TransactionStatus.IDLE


def test_deep_verification_rejects_malformed_same_column_schema(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        conn.execute("ALTER TABLE brain_job_decisions DROP CONSTRAINT brain_job_decisions_apply_expiry")
        conn.commit()
        with pytest.raises(RuntimeError, match="missing constraint.*apply_expiry"):
            schema.verify_schema_v1(conn)
        assert conn.info.transaction_status == TransactionStatus.IDLE


def test_deep_verification_rejects_same_name_constraint_and_index_tampering(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        conn.execute("ALTER TABLE brain_job_decisions DROP CONSTRAINT brain_job_decisions_apply_expiry")
        conn.execute("ALTER TABLE brain_job_decisions ADD CONSTRAINT brain_job_decisions_apply_expiry CHECK (true)")
        conn.commit()
        with pytest.raises(RuntimeError, match="apply_expiry definition mismatch"):
            schema.verify_schema_v1(conn)

    _cleanup(brain_pg)
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        conn.execute("DROP INDEX brain_decision_policies_one_active_per_lane")
        conn.execute(
            "CREATE UNIQUE INDEX brain_decision_policies_one_active_per_lane "
            "ON brain_decision_policies(lane, lifecycle)"
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="index.*signature mismatch"):
            schema.verify_schema_v1(conn)


def test_deep_verification_rejects_not_valid_constraint_and_wrong_trigger_binding(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        conn.execute("ALTER TABLE brain_job_decisions DROP CONSTRAINT brain_job_decisions_apply_expiry")
        conn.execute(
            "ALTER TABLE brain_job_decisions ADD CONSTRAINT brain_job_decisions_apply_expiry "
            "CHECK ((action <> 'apply') OR expires_at > created_at) NOT VALID"
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="apply_expiry is not validated"):
            schema.verify_schema_v1(conn)

    _cleanup(brain_pg)
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        conn.execute("DROP TRIGGER brain_artifacts_append_only ON brain_artifacts")
        conn.execute(
            "CREATE TRIGGER brain_artifacts_append_only BEFORE UPDATE OR DELETE ON brain_artifacts "
            "FOR EACH ROW EXECUTE FUNCTION brain_check_archive_manifest()"
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="binding or enabled state mismatch"):
            schema.verify_schema_v1(conn)


def test_deep_verification_rejects_non_migration_object_owner(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as owner:
        schema.ensure_schema_v1(owner)
        owner.execute("CREATE ROLE brain_schema_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        owner.commit()
    with psycopg.connect(brain_pg, row_factory=dict_row) as owner:
        owner.execute("ALTER TABLE brain_jobs OWNER TO postgres")
        owner.commit()
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        with pytest.raises(RuntimeError, match="ownership mismatch.*brain_jobs"):
            schema.verify_schema_v1(conn)
        assert conn.info.transaction_status == TransactionStatus.IDLE


def test_exact_enums_nullable_expiry_aliases_and_lossless_decision_fields(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        _insert_policy(conn)
        schema.ensure_policy_partition(conn, "ats-v1")
        conn.execute(
            "INSERT INTO brain_job_aliases "
            "(job_id, source_namespace, source_database_fingerprint, source_item_id, source_url, alias_type) "
            "VALUES ('job-1', 'review-db', %s, '42', 'https://example.test/42', 'source_id')",
            (HASHES[0],),
        )
        conn.execute(
            "INSERT INTO brain_jobs (job_id,source_namespace,source_job_id,title) VALUES "
            "('job-3','legacy','3','Third'),('job-4','legacy','4','Fourth')"
        )
        conn.commit()
        for decision_id, job_id, verdict, action, input_hash in (
            ("qualified", "job-1", "qualified", "apply", HASHES[2]),
            ("unqualified", "job-2", "unqualified", "reject", HASHES[3]),
            ("uncertain", "job-3", "uncertain", "review", HASHES[4]),
        ):
            _insert_decision(
                conn,
                decision_id=decision_id,
                source_id=decision_id,
                job_id=job_id,
                input_hash=input_hash,
                verdict=verdict,
                action=action,
            )
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_job_decisions (decision_id, source_namespace, source_decision_id, "
                    "job_id, policy_version, lane, qualification_verdict, action, input_hash, expires_at) "
                    "VALUES ('bad-expiry', 'test', 'bad-expiry', 'job-4', 'ats-v1', 'ats', "
                    "'qualified', 'apply', %s, now() - interval '1 second')",
                    (HASHES[5],),
                )
        assert (
            conn.execute("SELECT count(*) AS n FROM brain_job_decisions WHERE expires_at IS NULL").fetchone()["n"] == 2
        )


def test_label_and_pairwise_events_preserve_unresolved_source_endpoints(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        conn.execute(
            "INSERT INTO brain_label_events "
            "(source_namespace, source_event_id, source_item_id, source_item_url, project, method, "
            "confidence, weight, label_name, label_value, occurred_at, raw_artifact_hash, event_metadata) "
            "VALUES ('review', 'label-1', 'missing-1', 'https://missing/1', 'applypilot', 'manual', "
            "0.9, 2, 'fit', 'true', now(), %s, '{\"raw\":true}')",
            (HASHES[0],),
        )
        conn.execute(
            "INSERT INTO brain_pairwise_events "
            "(source_namespace, source_event_id, left_source_item_id, left_source_url, "
            "right_source_item_id, right_source_url, project, method, confidence, weight, "
            "preference, occurred_at, raw_artifact_hash, event_metadata) "
            "VALUES ('review', 'pair-1', 'missing-left', 'https://missing/left', "
            "'missing-right', 'https://missing/right', 'applypilot', 'manual', 0.8, 3, "
            "'left', now(), %s, '{\"raw\":true}')",
            (HASHES[1],),
        )
        conn.commit()
        assert conn.execute("SELECT job_id, source_item_id FROM brain_label_events").fetchone() == {
            "job_id": None,
            "source_item_id": "missing-1",
        }


def test_outcome_email_fk_and_stable_applications(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        email_id = conn.execute(
            "INSERT INTO brain_email_events "
            "(source_namespace, source_event_id, job_id, event_type, occurred_at) "
            "VALUES ('gmail', 'mail-1', 'job-1', 'reply', now()) RETURNING email_event_id"
        ).fetchone()["email_event_id"]
        conn.execute(
            "INSERT INTO brain_reviewed_outcomes "
            "(source_namespace, source_event_id, job_id, email_event_id, review_status, normalized_stage, "
            "weight, reviewer, reason, created_at, reviewed_at, updated_at) "
            "VALUES ('review', 'outcome-1', 'job-1', %s, 'confirmed', 'interview', 1.5, "
            "'owner', 'manual', now(), now(), now())",
            (email_id,),
        )
        conn.execute(
            "INSERT INTO brain_applications "
            "(application_id, job_id, source_namespace, source_application_id, source_channel, lane) "
            "VALUES ('app-1', 'job-1', 'legacy', '1', 'email', NULL)"
        )
        conn.execute(
            "INSERT INTO brain_application_events "
            "(application_id, source_namespace, source_event_id, source_channel, event_type, occurred_at) "
            "VALUES ('app-1', 'legacy', 'submitted-1', 'email', 'submitted', now())"
        )
        conn.commit()
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_reviewed_outcomes "
                    "(source_namespace, source_event_id, job_id, email_event_id, review_status, "
                    "normalized_stage, weight, reviewer, created_at, reviewed_at, updated_at) "
                    "VALUES ('review', 'bad', 'job-1', -1, 'confirmed', 'offer', 1, 'owner', now(), now(), now())"
                )


def _attach_policy_contract(conn, policy: str) -> dict[str, int]:
    roles = (
        "qualification_model",
        "preference_model",
        "outcome_model",
        "knowledge_graph",
        "label_snapshot",
        "pairwise_snapshot",
        "outcome_snapshot",
        "config",
        "metrics",
        "replay",
    )
    with conn.transaction():
        conn.execute(
            "INSERT INTO brain_artifact_locations "
            "(artifact_hash, backend, bucket_or_container, object_key, provider_version_id, provider_checksum, "
            "storage_immutable, encryption_mode, encryption_key_id, durability, verified_at) "
            "VALUES (%s, 's3', 'protected', %s, %s, %s, TRUE, 'customer_managed', 'key-v1', 'verified', now()) "
            "ON CONFLICT DO NOTHING",
            (HASHES[0], f"reports/{policy}", f"version-{policy}", HASHES[0]),
        )
        for index, role in enumerate(roles):
            conn.execute(
                "INSERT INTO brain_policy_artifacts (policy_version, artifact_role, artifact_hash) VALUES (%s, %s, %s)",
                (policy, role, HASHES[index]),
            )
        for approval in ("validated", "canary", "active", "retired"):
            conn.execute(
                "INSERT INTO brain_policy_approvals (policy_version, approval_type, approved_by) "
                "VALUES (%s, %s, 'owner')",
                (policy, approval),
            )
        receipts = {}
        definitions = conn.execute(
            "SELECT lifecycle, gate_name FROM brain_policy_gate_definitions "
            "WHERE definition_version=1 AND lane=(SELECT lane FROM brain_decision_policies WHERE policy_version=%s) "
            "ORDER BY lifecycle, gate_name",
            (policy,),
        ).fetchall()
        lane = conn.execute("SELECT lane FROM brain_decision_policies WHERE policy_version=%s", (policy,)).fetchone()[
            "lane"
        ]
        for definition in definitions:
            receipts[definition["lifecycle"]] = conn.execute(
                "INSERT INTO brain_policy_release_gate_events "
                "(policy_version, lane, lifecycle, gate_name, gate_state, checked_by, report_artifact_hash) "
                "VALUES (%s, %s, %s, %s, 'passed', 'owner', %s) RETURNING gate_event_id",
                (policy, lane, definition["lifecycle"], definition["gate_name"], HASHES[0]),
            ).fetchone()["gate_event_id"]
    return receipts


def test_policy_artifact_roles_lifecycle_gates_and_one_active_per_lane(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        fleet_schema.ensure_schema_v3(conn)
        _grant_fleet_controller_contract(conn)
        conn.commit()
        _install_fixture_data(conn)
        _insert_policy(conn)
        conn.execute("SET LOCAL ROLE brain_schema_migrator")
        with pytest.raises(psycopg.errors.CheckViolation, match="latest mandatory gate"):
            with conn.transaction():
                conn.execute("SELECT brain_transition_policy('ats-v1', 'validated')")
        _attach_policy_contract(conn, "ats-v1")
        conn.execute("RESET ROLE")
        conn.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
        conn.commit()
        with conn.transaction():
            conn.execute("SET LOCAL ROLE brain_schema_migrator")
            for lifecycle in ("validated", "canary", "active"):
                conn.execute("SELECT brain_transition_policy(%s, %s)", ("ats-v1", lifecycle))
        _insert_policy(conn, "ats-v2", "ats")
        _attach_policy_contract(conn, "ats-v2")
        conn.commit()
        with conn.transaction():
            conn.execute("SET LOCAL ROLE brain_schema_migrator")
            for lifecycle in ("validated", "canary", "active"):
                conn.execute("SELECT brain_transition_policy(%s, %s)", ("ats-v2", lifecycle))
        assert conn.execute(
            "SELECT policy_version,lifecycle FROM brain_decision_policies ORDER BY policy_version"
        ).fetchall() == [
            {"policy_version": "ats-v1", "lifecycle": "retired"},
            {"policy_version": "ats-v2", "lifecycle": "active"},
        ]


def test_artifacts_evidence_migration_parity_and_archive_are_immutable(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        run_id, batch_ids = _insert_migration_run_and_batches(conn, 1)
        batch_id = batch_ids[0]
        _claim_batch(conn, batch_id)
        completed_event_id = _complete_batch(conn, batch_id)
        _checkpoint_batch(conn, batch_id, completed_event_id)
        conn.execute(
            "INSERT INTO brain_migration_quarantine "
            "(migration_run_id,source_namespace,migration_batch_id,batch_ordinal,source_table,source_key,"
            "reason_code,unresolved_evidence) "
            "VALUES (%s,'sqlite',%s,1,'jobs','42','missing_alias','{\"source_id\":42}')",
            (run_id, batch_id),
        )
        conn.execute(
            "INSERT INTO brain_artifact_locations "
            "(artifact_hash,backend,bucket_or_container,object_key,provider_version_id,provider_checksum,"
            "storage_immutable,encryption_mode,encryption_key_id,durability,verified_at) "
            "VALUES (%s,'s3','archive','objects/a','v1',%s,TRUE,'provider_managed','aws/s3','verified',now())",
            (HASHES[0], HASHES[0]),
        )
        conn.execute(
            "INSERT INTO brain_archive.brain_archive_manifests "
            "(retry_identity, source_relation, artifact_hash, row_count) "
            "VALUES ('immutable-archive', 'brain_job_decisions', %s, 0)",
            (HASHES[0],),
        )
        conn.commit()
        for relation in (
            "brain_artifacts",
            "brain_migration_sources",
            "brain_migration_runs",
            "brain_migration_run_events",
            "brain_migration_batches",
            "brain_migration_batch_events",
            "brain_migration_checkpoints",
            "brain_migration_quarantine",
            "brain_archive.brain_archive_manifests",
        ):
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
                with conn.transaction():
                    conn.execute(f"DELETE FROM {relation}")


def test_ledgers_decisions_identities_and_policy_receipts_are_immutable(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        _insert_policy(conn)
        _attach_policy_contract(conn, "ats-v1")
        schema.ensure_policy_partition(conn, "ats-v1")
        _insert_decision(conn)
        conn.execute(
            "INSERT INTO brain_label_events "
            "(source_namespace, source_event_id, job_id, project, method, label_name, label_value, occurred_at) "
            "VALUES ('review', 'immutable-label', 'job-1', 'applypilot', 'manual', 'fit', 'true', now())"
        )
        conn.commit()
        for relation in (
            "brain_label_events",
            "brain_job_decisions",
            "brain_decision_identities",
            "brain_policy_artifacts",
            "brain_policy_approvals",
            "brain_policy_release_gate_events",
        ):
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
                with conn.transaction():
                    conn.execute(f"DELETE FROM {relation}")


def test_supersession_requires_same_subject_and_prevents_forks(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        first = conn.execute(
            "INSERT INTO brain_label_events "
            "(source_namespace, source_event_id, job_id, project, method, label_name, label_value, occurred_at) "
            "VALUES ('review', 'first', 'job-1', 'applypilot', 'manual', 'fit', 'true', now()) "
            "RETURNING label_event_id"
        ).fetchone()["label_event_id"]
        conn.commit()
        with pytest.raises(psycopg.errors.CheckViolation, match="subject/source mismatch"):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_label_events "
                    "(source_namespace, source_event_id, job_id, project, method, label_name, label_value, "
                    "occurred_at, supersedes_label_event_id) "
                    "VALUES ('review', 'wrong', 'job-2', 'applypilot', 'manual', 'fit', 'false', now(), %s)",
                    (first,),
                )
        with conn.transaction():
            conn.execute(
                "INSERT INTO brain_label_events "
                "(source_namespace, source_event_id, job_id, project, method, label_name, label_value, "
                "occurred_at, supersedes_label_event_id) "
                "VALUES ('review', 'second', 'job-1', 'applypilot', 'manual', 'fit', 'false', now(), %s)",
                (first,),
            )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_label_events "
                    "(source_namespace, source_event_id, job_id, project, method, label_name, label_value, "
                    "occurred_at, supersedes_label_event_id) "
                    "VALUES ('review', 'fork', 'job-1', 'applypilot', 'manual', 'fit', 'null', now(), %s)",
                    (first,),
                )


def test_policy_partition_routing_detach_and_global_decision_identity(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        _insert_policy(conn, "ats-v1", "ats")
        _insert_policy(conn, "ats-v2", "ats")
        first = schema.ensure_policy_partition(conn, "ats-v1")
        second = schema.ensure_policy_partition(conn, "ats-v2")
        assert first != second
        assert first == schema.ensure_policy_partition(conn, "ats-v1")
        schema.verify_schema_v1(conn)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
            with conn.transaction():
                conn.execute(
                    pg_sql.SQL("TRUNCATE TABLE {}.{}").format(pg_sql.Identifier("public"), pg_sql.Identifier(first))
                )
        _insert_decision(conn)
        assert (
            conn.execute("SELECT tableoid::regclass::text AS partition FROM brain_job_decisions").fetchone()[
                "partition"
            ]
            == first
        )
        conn.rollback()
        with pytest.raises(psycopg.errors.UniqueViolation):
            _insert_decision(
                conn,
                decision_id="decision-1",
                source_id="other-source",
                policy="ats-v2",
                input_hash=HASHES[2],
            )
        with conn.transaction():
            conn.execute(f"ALTER TABLE brain_job_decisions DETACH PARTITION {second}")
        assert (
            conn.execute("SELECT relispartition FROM pg_class WHERE oid = %s::regclass", (second,)).fetchone()[
                "relispartition"
            ]
            is False
        )


def test_migration_batches_support_skip_locked_disjoint_claims(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        run_id, _ = _insert_migration_run_and_batches(conn)
        conn.commit()
        with conn.transaction():
            first = conn.execute(
                "SELECT migration_batch_id FROM brain_migration_batches WHERE migration_run_id=%s "
                "ORDER BY migration_batch_id FOR UPDATE SKIP LOCKED LIMIT 1",
                (run_id,),
            ).fetchone()["migration_batch_id"]
            assert first is not None


def test_public_fleet_worker_and_broad_default_grants_are_removed(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        conn.execute("CREATE ROLE fleet_worker LOGIN")
        conn.execute("CREATE ROLE brain_broad_role LOGIN")
        conn.execute("GRANT ALL ON SCHEMA public TO fleet_worker, brain_broad_role")
        conn.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
            "GRANT ALL ON TABLES TO fleet_worker, brain_broad_role"
        )
        conn.execute("ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public GRANT SELECT ON TABLES TO PUBLIC")
        conn.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE postgres IN SCHEMA public "
            "GRANT EXECUTE ON FUNCTIONS TO fleet_worker, brain_broad_role"
        )
        conn.commit()
        schema.ensure_schema_v1(conn)
        rows = conn.execute(
            "SELECT c.relname, "
            "has_table_privilege('fleet_worker', c.oid, 'SELECT,INSERT,UPDATE,DELETE') AS fleet, "
            "has_table_privilege('brain_broad_role', c.oid, 'SELECT,INSERT,UPDATE,DELETE') AS broad "
            "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND left(c.relname, 6)='brain_' AND c.relkind IN ('r','p')"
        ).fetchall()
        assert rows
        assert all(not row["fleet"] and not row["broad"] for row in rows)
        assert conn.execute(
            "SELECT bool_and(COALESCE(r.rolname,'PUBLIC')='brain_schema_verifier' "
            "AND acl.privilege_type='SELECT') AS valid FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) acl "
            "LEFT JOIN pg_roles r ON r.oid=acl.grantee WHERE n.nspname='public' "
            "AND left(c.relname,6)='brain_' AND acl.grantee<>c.relowner"
        ).fetchone()["valid"]
        assert (
            conn.execute(
                "SELECT count(*) AS n FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                "WHERE n.nspname='public' AND left(p.proname, 6)='brain_' AND "
                "(has_function_privilege('fleet_worker', p.oid, 'EXECUTE') OR "
                "has_function_privilege('brain_broad_role', p.oid, 'EXECUTE'))"
            ).fetchone()["n"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT has_schema_privilege('fleet_worker', 'brain_archive', 'USAGE,CREATE') AS exposed"
            ).fetchone()["exposed"]
            is False
        )
        conn.rollback()
        schema.verify_schema_v1(conn)


def _insert_migration_run_and_batches(conn, batch_count: int = 2) -> tuple[int, list[int]]:
    conn.execute("SET LOCAL ROLE brain_schema_migrator")
    source_id = conn.execute(
        "INSERT INTO brain_migration_sources "
        "(source_namespace, source_fingerprint, byte_length, schema_metadata) "
        "VALUES ('sqlite', %s, 1, '{}') RETURNING migration_source_id",
        (HASHES[0],),
    ).fetchone()["migration_source_id"]
    run_id = conn.execute(
        "INSERT INTO brain_migration_runs (migration_source_id, source_namespace, run_key, metadata) "
        "VALUES (%s, 'sqlite', 'phase1-import', '{}') RETURNING migration_run_id",
        (source_id,),
    ).fetchone()["migration_run_id"]
    conn.execute(
        "INSERT INTO brain_migration_run_events "
        "(migration_run_id, source_namespace, event_type, actor_id) "
        "VALUES (%s, 'sqlite', 'started', 'controller')",
        (run_id,),
    )
    batch_ids = []
    for index in range(batch_count):
        batch_ids.append(
            conn.execute(
                "INSERT INTO brain_migration_batches "
                "(migration_run_id, source_namespace, source_table, batch_ordinal, key_start, key_end) "
                "VALUES (%s, 'sqlite', 'jobs', %s, %s, %s) "
                "RETURNING migration_batch_id",
                (run_id, index + 1, f"{index * 100 + 1:03}", f"{(index + 1) * 100:03}"),
            ).fetchone()["migration_batch_id"]
        )
        conn.execute(
            "INSERT INTO brain_migration_batch_events "
            "(migration_run_id, source_namespace, source_table, migration_batch_id, batch_ordinal, "
            "event_type, attempt) VALUES (%s, 'sqlite', 'jobs', %s, %s, 'pending', 0)",
            (run_id, batch_ids[-1], index + 1),
        )
    return run_id, batch_ids


def _batch_row(conn, batch_id: int):
    return conn.execute(
        "SELECT b.*, e.migration_batch_event_id AS head_event_id, e.attempt AS head_attempt "
        "FROM brain_migration_batches b JOIN LATERAL ("
        "SELECT migration_batch_event_id, attempt FROM brain_migration_batch_events "
        "WHERE migration_batch_id=b.migration_batch_id ORDER BY migration_batch_event_id DESC LIMIT 1"
        ") e ON TRUE WHERE b.migration_batch_id=%s",
        (batch_id,),
    ).fetchone()


def _claim_batch(conn, batch_id: int, worker: str = "worker-1") -> int:
    batch = _batch_row(conn, batch_id)
    return conn.execute(
        "INSERT INTO brain_migration_batch_events "
        "(migration_run_id, source_namespace, source_table, migration_batch_id, batch_ordinal, event_type, "
        "attempt, worker_id, lease_expires_at, supersedes_batch_event_id) "
        "VALUES (%s,%s,%s,%s,%s,'claimed',%s,%s,now()+interval '1 minute',%s) "
        "RETURNING migration_batch_event_id",
        (
            batch["migration_run_id"],
            batch["source_namespace"],
            batch["source_table"],
            batch_id,
            batch["batch_ordinal"],
            batch["head_attempt"] + 1,
            worker,
            batch["head_event_id"],
        ),
    ).fetchone()["migration_batch_event_id"]


def _complete_batch(conn, batch_id: int, worker: str = "worker-1") -> int:
    batch = _batch_row(conn, batch_id)
    return conn.execute(
        "INSERT INTO brain_migration_batch_events "
        "(migration_run_id, source_namespace, source_table, migration_batch_id, batch_ordinal, event_type, "
        "attempt, worker_id, source_count, target_count, canonical_batch_hash, supersedes_batch_event_id) "
        "VALUES (%s,%s,%s,%s,%s,'completed',%s,%s,100,100,%s,%s) RETURNING migration_batch_event_id",
        (
            batch["migration_run_id"],
            batch["source_namespace"],
            batch["source_table"],
            batch_id,
            batch["batch_ordinal"],
            batch["head_attempt"],
            worker,
            HASHES[1],
            batch["head_event_id"],
        ),
    ).fetchone()["migration_batch_event_id"]


def _checkpoint_batch(conn, batch_id: int, completed_event_id: int) -> None:
    batch = _batch_row(conn, batch_id)
    conn.execute(
        "INSERT INTO brain_migration_checkpoints "
        "(migration_run_id,source_namespace,source_table,batch_ordinal,last_key,migration_batch_id,"
        "migration_batch_event_id,canonical_checkpoint_hash) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            batch["migration_run_id"],
            batch["source_namespace"],
            batch["source_table"],
            batch["batch_ordinal"],
            batch["key_end"],
            batch_id,
            completed_event_id,
            HASHES[1],
        ),
    )


def _complete_run(conn, run_id: int) -> int:
    head = conn.execute(
        "SELECT migration_run_event_id FROM brain_migration_run_events WHERE migration_run_id=%s "
        "ORDER BY migration_run_event_id DESC LIMIT 1",
        (run_id,),
    ).fetchone()["migration_run_event_id"]
    return conn.execute(
        "INSERT INTO brain_migration_run_events "
        "(migration_run_id,source_namespace,event_type,actor_id,supersedes_run_event_id) "
        "VALUES (%s,'sqlite','completed','controller',%s) RETURNING migration_run_event_id",
        (run_id, head),
    ).fetchone()["migration_run_event_id"]


def _protect_artifact(conn, artifact_hash: str, suffix: str) -> None:
    conn.execute(
        "INSERT INTO brain_artifact_locations "
        "(artifact_hash,backend,bucket_or_container,object_key,provider_version_id,provider_checksum,"
        "storage_immutable,encryption_mode,encryption_key_id,durability,verified_at) "
        "VALUES (%s,'s3','protected',%s,%s,%s,TRUE,'customer_managed','key-v1','verified',now()) "
        "ON CONFLICT DO NOTHING",
        (artifact_hash, f"receipts/{suffix}", f"version-{suffix}", artifact_hash),
    )


def test_real_shape_import_preserves_unclear_pairwise_and_null_outcome_weight(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        pairwise_id = conn.execute(
            "INSERT INTO brain_pairwise_events "
            "(source_namespace, source_event_id, left_job_id, right_job_id, project, method, preference, occurred_at) "
            "VALUES ('sqlite', 'pair-unclear', 'job-1', 'job-2', 'applypilot', 'import', 'unclear', now()) "
            "RETURNING pairwise_event_id"
        ).fetchone()["pairwise_event_id"]
        email_id = conn.execute(
            "INSERT INTO brain_email_events "
            "(source_namespace, source_event_id, job_id, event_type, occurred_at) "
            "VALUES ('gmail', 'mail-null-weight', 'job-1', 'reply', now()) RETURNING email_event_id"
        ).fetchone()["email_event_id"]
        outcome_id = conn.execute(
            "INSERT INTO brain_reviewed_outcomes "
            "(source_namespace, source_event_id, job_id, email_event_id, review_status, normalized_stage, "
            "weight, reviewer, created_at, reviewed_at, updated_at) "
            "VALUES ('sqlite', 'outcome-null-weight', 'job-1', %s, 'confirmed', 'interview', "
            "NULL, 'owner', now(), now(), now()) RETURNING reviewed_outcome_id",
            (email_id,),
        ).fetchone()["reviewed_outcome_id"]
        assert (
            conn.execute(
                "SELECT preference FROM brain_pairwise_events WHERE pairwise_event_id=%s", (pairwise_id,)
            ).fetchone()["preference"]
            == "unclear"
        )
        assert (
            conn.execute(
                "SELECT weight FROM brain_reviewed_outcomes WHERE reviewed_outcome_id=%s", (outcome_id,)
            ).fetchone()["weight"]
            is None
        )


def test_migration_definitions_are_immutable_and_events_record_run_and_batch_progress(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        run_id, batch_ids = _insert_migration_run_and_batches(conn, 1)
        _claim_batch(conn, batch_ids[0])
        completed_id = _complete_batch(conn, batch_ids[0])
        _checkpoint_batch(conn, batch_ids[0], completed_id)
        _complete_run(conn, run_id)
        conn.commit()
        for relation in (
            "brain_migration_runs",
            "brain_migration_run_events",
            "brain_migration_batches",
            "brain_migration_batch_events",
        ):
            with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
                with conn.transaction():
                    conn.execute(f"UPDATE {relation} SET created_at=created_at")


def test_composite_migration_lineage_and_checkpoint_completion_are_enforced(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        run_id, batch_ids = _insert_migration_run_and_batches(conn, 1)
        other_source = conn.execute(
            "INSERT INTO brain_migration_sources "
            "(source_namespace, source_fingerprint, byte_length, schema_metadata) "
            "VALUES ('sqlite', %s, 1, '{}') RETURNING migration_source_id",
            (HASHES[2],),
        ).fetchone()["migration_source_id"]
        other_run = conn.execute(
            "INSERT INTO brain_migration_runs (migration_source_id, source_namespace, run_key, metadata) "
            "VALUES (%s, 'sqlite', 'other', '{}') RETURNING migration_run_id",
            (other_source,),
        ).fetchone()["migration_run_id"]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_migration_batch_events "
                    "(migration_run_id,source_namespace,source_table,migration_batch_id,batch_ordinal,event_type,"
                    "attempt,worker_id,lease_expires_at,supersedes_batch_event_id) "
                    "VALUES (%s,'sqlite','jobs',%s,1,'claimed',1,'worker',now()+interval '1 minute',1)",
                    (other_run, batch_ids[0]),
                )
        pending_id = _batch_row(conn, batch_ids[0])["head_event_id"]
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_migration_checkpoints "
                    "(migration_run_id,source_namespace,source_table,batch_ordinal,last_key,migration_batch_id,"
                    "migration_batch_event_id,committed_event_type,canonical_checkpoint_hash) "
                    "VALUES (%s,'sqlite','jobs',1,'100',%s,%s,'completed',%s)",
                    (run_id, batch_ids[0], pending_id, HASHES[1]),
                )


def test_parity_pass_requires_clean_results_and_exact_pass_event(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        run_id, batch_ids = _insert_migration_run_and_batches(conn, 1)
        _claim_batch(conn, batch_ids[0])
        completed_batch = _complete_batch(conn, batch_ids[0])
        _checkpoint_batch(conn, batch_ids[0], completed_batch)
        completed_run = _complete_run(conn, run_id)
        for artifact_hash, suffix in zip(HASHES[:3], ("report", "delta", "freeze"), strict=True):
            _protect_artifact(conn, artifact_hash, suffix)
        parity_id = conn.execute(
            "INSERT INTO brain_parity_runs "
            "(migration_run_id,source_namespace,definition_version,completed_run_event_id,report_artifact_hash,"
            "final_delta_receipt_hash,writer_freeze_receipt_hash,started_at) "
            "VALUES (%s,'sqlite',1,%s,%s,%s,%s,now()) RETURNING parity_run_id",
            (run_id, completed_run, HASHES[0], HASHES[1], HASHES[2]),
        ).fetchone()["parity_run_id"]
        definitions = conn.execute(
            "SELECT check_key,relation_name FROM brain_parity_definitions WHERE definition_version=1 ORDER BY check_key"
        ).fetchall()
        for definition in definitions:
            mismatch = definition["check_key"] == "jobs"
            conn.execute(
                "INSERT INTO brain_parity_results "
                "(migration_run_id,source_namespace,parity_run_id,definition_version,check_key,table_name,"
                "check_type,source_count,target_count,source_hash,target_hash,mismatch_count,unresolved_count,"
                "report_artifact_hash) VALUES (%s,'sqlite',%s,1,%s,%s,'canonical',2,%s,%s,%s,%s,%s,%s)",
                (
                    run_id,
                    parity_id,
                    definition["check_key"],
                    definition["relation_name"],
                    1 if mismatch else 2,
                    HASHES[1],
                    HASHES[2] if mismatch else HASHES[1],
                    1 if mismatch else 0,
                    1 if mismatch else 0,
                    HASHES[0],
                ),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="parity pass requires"):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_parity_run_events "
                    "(migration_run_id,source_namespace,parity_run_id,event_type,actor_id) "
                    "VALUES (%s,'sqlite',%s,'passed','verifier')",
                    (run_id, parity_id),
                )
        clean_parity_id = conn.execute(
            "INSERT INTO brain_parity_runs "
            "(migration_run_id,source_namespace,definition_version,completed_run_event_id,report_artifact_hash,"
            "final_delta_receipt_hash,writer_freeze_receipt_hash,started_at) "
            "VALUES (%s,'sqlite',1,%s,%s,%s,%s,now()) RETURNING parity_run_id",
            (run_id, completed_run, HASHES[0], HASHES[1], HASHES[2]),
        ).fetchone()["parity_run_id"]
        for definition in definitions:
            conn.execute(
                "INSERT INTO brain_parity_results "
                "(migration_run_id,source_namespace,parity_run_id,definition_version,check_key,table_name,"
                "check_type,source_count,target_count,source_hash,target_hash,report_artifact_hash) "
                "VALUES (%s,'sqlite',%s,1,%s,%s,'canonical',2,2,%s,%s,%s)",
                (
                    run_id,
                    clean_parity_id,
                    definition["check_key"],
                    definition["relation_name"],
                    HASHES[1],
                    HASHES[1],
                    HASHES[0],
                ),
            )
        passed_event = conn.execute(
            "INSERT INTO brain_parity_run_events "
            "(migration_run_id,source_namespace,parity_run_id,event_type,actor_id) "
            "VALUES (%s,'sqlite',%s,'passed','verifier') RETURNING parity_run_event_id",
            (run_id, clean_parity_id),
        ).fetchone()["parity_run_event_id"]
        assert passed_event > 0


def test_policy_transition_graph_metadata_immutability_gate_reruns_and_retirement(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        _insert_policy(conn)
        _attach_policy_contract(conn, "ats-v1")
        failed = conn.execute(
            "INSERT INTO brain_policy_release_gate_events "
            "(policy_version,lane,lifecycle,gate_name,gate_state,checked_by,report_artifact_hash,mismatch_count) "
            "VALUES ('ats-v1','ats','validated','parity','failed','owner',%s,1) RETURNING gate_event_id",
            (HASHES[0],),
        ).fetchone()["gate_event_id"]
        passed = conn.execute(
            "INSERT INTO brain_policy_release_gate_events "
            "(policy_version,lane,lifecycle,gate_name,gate_state,checked_by,report_artifact_hash) "
            "VALUES ('ats-v1','ats','validated','parity','passed','owner',%s) RETURNING gate_event_id",
            (HASHES[0],),
        ).fetchone()["gate_event_id"]
        assert failed != passed
        conn.execute("SET LOCAL ROLE brain_schema_migrator")
        with conn.transaction():
            conn.execute("SELECT brain_transition_policy('ats-v1', 'validated')")
        with pytest.raises(psycopg.errors.CheckViolation, match="immutable after draft"):
            with conn.transaction():
                conn.execute("UPDATE brain_decision_policies SET lane='linkedin' WHERE policy_version='ats-v1'")
        with pytest.raises(psycopg.errors.CheckViolation, match="transition"):
            with conn.transaction():
                conn.execute(
                    "UPDATE brain_decision_policies SET lifecycle='active', activated_at=now() "
                    "WHERE policy_version='ats-v1'"
                )


def test_append_only_truncate_and_deep_catalog_tampering_are_rejected(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="append-only"):
            with conn.transaction():
                conn.execute("TRUNCATE brain_artifact_locations")
        conn.execute("ALTER TABLE brain_artifacts DISABLE TRIGGER brain_artifacts_append_only")
        conn.commit()
        with pytest.raises(RuntimeError, match="trigger.*enabled"):
            schema.verify_schema_v1(conn)


def test_deep_verifier_rejects_noop_function_replacement(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        conn.execute(
            "CREATE OR REPLACE FUNCTION brain_reject_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN PERFORM 1; RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE='55000'; END $$"
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="catalog contract hash mismatch"):
            schema.verify_schema_v1(conn)


def test_schema_version_ledger_rejects_future_and_interrupted_versions(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        conn.execute("SET LOCAL ROLE brain_schema_migrator")
        conn.execute(
            "CREATE TABLE brain_schema_versions (version integer PRIMARY KEY, migration_name text, "
            "migration_checksum text, applied_at timestamptz, applied_by text)"
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="ledger exists but is empty"):
            schema.ensure_schema_v1(conn)
        conn.execute("SET LOCAL ROLE brain_schema_migrator")
        conn.execute("DROP TABLE brain_schema_versions")
        conn.commit()
        schema.ensure_schema_v1(conn)
        conn.execute("ALTER TABLE brain_schema_versions DISABLE TRIGGER brain_schema_versions_append_only")
        conn.execute(
            "INSERT INTO brain_schema_versions "
            "(version,migration_name,migration_checksum,applied_at,applied_by) "
            "VALUES (2,'future','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',now(),current_user)"
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="unsupported or non-contiguous"):
            schema.verify_schema_v1(conn)


def test_superuser_without_exact_migration_identity_is_denied(brain_pg, monkeypatch):
    monkeypatch.setenv("APPLYPILOT_BRAIN_MIGRATION_ROLE", "postgres")
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        conn.execute("REVOKE brain_schema_migrator FROM postgres")
        conn.commit()
        with pytest.raises(RuntimeError, match="fixed role brain_schema_migrator"):
            schema.ensure_schema_v1(conn)
        conn.execute("GRANT brain_schema_migrator TO postgres")
        conn.commit()


def test_explicit_read_only_verifier_acl_and_schema_create_revocation(brain_pg, monkeypatch):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        conn.execute("CREATE ROLE brain_schema_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        conn.execute("CREATE ROLE brain_broad_role LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE")
        conn.execute("GRANT CREATE ON SCHEMA public TO brain_schema_reader, brain_broad_role")
        conn.commit()
    monkeypatch.setenv("APPLYPILOT_BRAIN_VERIFIER_ROLE", "brain_schema_reader")
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        assert conn.execute(
            "SELECT has_schema_privilege('brain_schema_verifier', 'public', 'USAGE') AS usage, "
            "has_schema_privilege('brain_schema_verifier', 'public', 'CREATE') AS create_privilege, "
            "has_table_privilege('brain_schema_verifier', 'brain_jobs', 'SELECT') AS read, "
            "has_table_privilege('brain_schema_verifier', 'brain_jobs', 'INSERT,UPDATE,DELETE,TRUNCATE') AS write"
        ).fetchone() == {"usage": True, "create_privilege": False, "read": True, "write": False}
        assert (
            conn.execute(
                "SELECT has_schema_privilege('brain_broad_role', 'public', 'CREATE') AS create_privilege"
            ).fetchone()["create_privilege"]
            is False
        )
        conn.rollback()
        schema.verify_schema_v1(conn)


def test_partition_creation_fails_closed_when_default_contains_policy_rows(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        _insert_policy(conn)
        assert (
            conn.execute("SELECT to_regclass('public.brain_job_decisions_default') AS relation").fetchone()["relation"]
            is None
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="no partition"):
            _insert_decision(conn)
        conn.execute("CREATE TABLE brain_job_decisions_default PARTITION OF brain_job_decisions DEFAULT")
        conn.commit()
        _insert_decision(conn)
        with pytest.raises(RuntimeError, match="default partition contains"):
            schema.ensure_policy_partition(conn, "ats-v1")


def test_concurrent_installers_and_batch_claims_are_serialized_and_disjoint(brain_pg):
    install_barrier = Barrier(2)

    def install() -> None:
        with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
            install_barrier.wait()
            schema.ensure_schema_v1(conn)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: install(), range(2)))

    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        run_id, _ = _insert_migration_run_and_batches(conn)
        conn.commit()

    claim_barrier = Barrier(2)

    def claim(worker: str) -> int:
        with psycopg.connect(brain_pg, row_factory=dict_row) as conn, conn.transaction():
            conn.execute("SET LOCAL ROLE brain_schema_migrator")
            claimed = conn.execute(
                "WITH candidate AS ("
                " SELECT b.migration_run_id,b.source_namespace,b.source_table,b.migration_batch_id,b.batch_ordinal,"
                " head.migration_batch_event_id,head.attempt FROM brain_migration_batches b"
                " JOIN LATERAL (SELECT e.migration_batch_event_id,e.attempt,e.event_type,e.lease_expires_at "
                " FROM brain_migration_batch_events e WHERE e.migration_batch_id=b.migration_batch_id "
                " ORDER BY e.migration_batch_event_id DESC LIMIT 1) head ON TRUE"
                " WHERE b.migration_run_id=%s AND (head.event_type IN ('pending','failed','quarantined')"
                " OR (head.event_type='claimed' AND head.lease_expires_at<=now()))"
                " ORDER BY b.migration_batch_id FOR UPDATE OF b SKIP LOCKED LIMIT 1"
                ") INSERT INTO brain_migration_batch_events "
                "(migration_run_id,source_namespace,source_table,migration_batch_id,batch_ordinal,event_type,attempt,"
                "worker_id,lease_expires_at,supersedes_batch_event_id) "
                "SELECT migration_run_id,source_namespace,source_table,migration_batch_id,batch_ordinal,'claimed',"
                "attempt+1,%s,now()+interval '1 minute',migration_batch_event_id "
                "FROM candidate RETURNING migration_batch_id",
                (run_id, worker),
            ).fetchone()["migration_batch_id"]
            claim_barrier.wait()
            return claimed

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, ("worker-1", "worker-2")))
    assert len(set(claims)) == 2


def test_email_outcome_job_lineage_and_unresolved_label_supersession_identity(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        email_id = conn.execute(
            "INSERT INTO brain_email_events "
            "(source_namespace, source_event_id, job_id, event_type, occurred_at) "
            "VALUES ('gmail', 'mail-job-1', 'job-1', 'reply', now()) RETURNING email_event_id"
        ).fetchone()["email_event_id"]
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_reviewed_outcomes "
                    "(source_namespace, source_event_id, job_id, email_event_id, review_status, normalized_stage, "
                    "reviewer, created_at, reviewed_at, updated_at) "
                    "VALUES ('review', 'wrong-job', 'job-2', %s, 'confirmed', 'interview', "
                    "'owner', now(), now(), now())",
                    (email_id,),
                )
        first = conn.execute(
            "INSERT INTO brain_label_events "
            "(source_namespace, source_event_id, source_item_id, source_item_url, project, method, "
            "label_name, label_value, occurred_at) "
            "VALUES ('review', 'unresolved-1', 'missing-1', 'https://missing/1', 'applypilot', "
            "'manual', 'fit', 'true', now()) RETURNING label_event_id"
        ).fetchone()["label_event_id"]
        with pytest.raises(psycopg.errors.CheckViolation, match="subject/source mismatch"):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_label_events "
                    "(source_namespace,source_event_id,source_item_id,source_item_url,project,method,"
                    "label_name,label_value,occurred_at,supersedes_label_event_id) "
                    "VALUES ('review','unresolved-url-change','missing-1','https://missing/changed','applypilot',"
                    "'manual','fit','false',now(),%s)",
                    (first,),
                )
        with pytest.raises(psycopg.errors.CheckViolation, match="subject/source mismatch"):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_label_events "
                    "(source_namespace, source_event_id, source_item_id, source_item_url, project, method, "
                    "label_name, label_value, occurred_at, supersedes_label_event_id) "
                    "VALUES ('review', 'unresolved-wrong', 'missing-2', 'https://missing/2', 'applypilot', "
                    "'manual', 'fit', 'false', now(), %s)",
                    (first,),
                )


def test_stable_source_namespace_survives_snapshot_changes_and_scopes_event_identity(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        conn.execute("SET LOCAL ROLE brain_schema_migrator")
        source_ids = []
        for fingerprint in (HASHES[0], HASHES[1]):
            source_ids.append(
                conn.execute(
                    "INSERT INTO brain_migration_sources "
                    "(source_namespace, source_fingerprint, byte_length, schema_metadata) "
                    "VALUES ('applypilot-sqlite', %s, 1, '{}') RETURNING migration_source_id",
                    (fingerprint,),
                ).fetchone()["migration_source_id"]
            )
        assert source_ids[0] != source_ids[1]
        conn.execute(
            "INSERT INTO brain_label_events "
            "(source_namespace, source_event_id, job_id, project, method, label_name, label_value, occurred_at) "
            "VALUES ('applypilot-sqlite', 'local-1', 'job-1', 'applypilot', 'import', 'fit', 'true', now()), "
            "('other-sqlite', 'local-1', 'job-1', 'applypilot', 'import', 'fit', 'true', now())"
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_label_events "
                    "(source_namespace, source_event_id, job_id, project, method, label_name, label_value, occurred_at) "
                    "VALUES ('applypilot-sqlite', 'local-1', 'job-1', 'applypilot', 'import', 'fit', 'false', now())"
                )


def test_artifact_request_identity_replica_metadata_and_protected_archive_manifest(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        conn.execute(
            "INSERT INTO brain_artifacts "
            "(request_id, artifact_hash, media_type, byte_length, schema_version, provenance, location) "
            "VALUES ('artifact-request-1', %s, 'application/json', 10, 1, '{\"source\":\"test\"}', 's3://staging/a')",
            (HASHES[0],),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_artifacts "
                    "(request_id, artifact_hash, media_type, byte_length, schema_version, location) "
                    "VALUES ('artifact-request-1', %s, 'application/json', 10, 1, 's3://staging/b')",
                    (HASHES[1],),
                )
        conn.execute(
            "INSERT INTO brain_artifact_locations "
            "(artifact_hash, backend, bucket_or_container, object_key, provider_version_id, provider_checksum, "
            "storage_immutable, encryption_mode, encryption_key_id, durability, verified_at) "
            "VALUES (%s, 's3', 'brain-staging', 'objects/a', 'version-1', %s, "
            "FALSE, 'provider_managed', 'aws/s3', 'committed_unprotected', NULL)",
            (HASHES[0], HASHES[0]),
        )
        with pytest.raises(psycopg.errors.CheckViolation, match="protected verified immutable replica"):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_archive.brain_archive_manifests "
                    "(retry_identity, source_relation, artifact_hash, row_count) "
                    "VALUES ('archive-request-1', 'brain_job_decisions', %s, 1)",
                    (HASHES[0],),
                )
        conn.execute(
            "INSERT INTO brain_artifact_locations "
            "(artifact_hash, backend, bucket_or_container, object_key, provider_version_id, provider_checksum, "
            "storage_immutable, encryption_mode, encryption_key_id, durability, verified_at) "
            "VALUES (%s, 'azure', 'brain-archive', 'objects/a', 'etag-1', %s, "
            "TRUE, 'customer_managed', 'key-v1', 'verified', now())",
            (HASHES[0], HASHES[0]),
        )
        conn.execute(
            "INSERT INTO brain_archive.brain_archive_manifests "
            "(retry_identity, source_relation, artifact_hash, row_count) "
            "VALUES ('archive-request-1', 'brain_job_decisions', %s, 1)",
            (HASHES[0],),
        )
        with pytest.raises(psycopg.errors.UniqueViolation):
            with conn.transaction():
                conn.execute(
                    "INSERT INTO brain_archive.brain_archive_manifests "
                    "(retry_identity, source_relation, artifact_hash, row_count) "
                    "VALUES ('archive-request-1', 'brain_job_decisions', %s, 1)",
                    (HASHES[0],),
                )


def test_controller_policy_transition_requires_latest_gate_and_paused_bound_fleet_config(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        fleet_schema.ensure_schema_v3(conn)
        _grant_fleet_controller_contract(conn)
        conn.commit()
        _install_fixture_data(conn)
        _insert_policy(conn)
        _attach_policy_contract(conn, "ats-v1")
        conn.execute("SET LOCAL ROLE brain_schema_migrator")
        conn.execute("SELECT brain_transition_policy('ats-v1', 'validated')")
        conn.execute("SELECT brain_transition_policy('ats-v1', 'canary')")
        conn.commit()
        with pytest.raises(psycopg.errors.InsufficientPrivilege, match="controller"):
            with conn.transaction():
                conn.execute(
                    "UPDATE brain_decision_policies SET lifecycle='active', activated_at=now() "
                    "WHERE policy_version='ats-v1'"
                )
        conn.execute(
            "INSERT INTO fleet_decision_policies (policy_version, lane, status) "
            "VALUES ('ats-v1', 'ats', 'canary') "
            "ON CONFLICT (policy_version) DO UPDATE SET lane=EXCLUDED.lane, status=EXCLUDED.status"
        )
        conn.execute(
            "UPDATE fleet_decision_policies SET status='retired', retired_at=now() "
            "WHERE lane='ats' AND policy_version<>'ats-v1' AND status='active'"
        )
        conn.execute("UPDATE fleet_config SET paused=FALSE, ats_paused=FALSE, ats_policy_version=NULL WHERE id=1")
        conn.commit()
        with pytest.raises(psycopg.errors.ObjectNotInPrerequisiteState, match="lane must remain paused"):
            with conn.transaction():
                conn.execute("SET LOCAL ROLE brain_schema_migrator")
                conn.execute("SELECT brain_transition_policy('ats-v1', 'active')")
        conn.execute("UPDATE fleet_config SET paused=TRUE WHERE id=1")
        conn.execute(
            "INSERT INTO brain_policy_release_gate_events "
            "(policy_version,lane,lifecycle,gate_name,gate_state,checked_by,report_artifact_hash,mismatch_count) "
            "VALUES ('ats-v1','ats','active','parity','failed','owner',%s,1)",
            (HASHES[0],),
        )
        conn.commit()
        with pytest.raises(psycopg.errors.CheckViolation, match="latest mandatory gate"):
            with conn.transaction():
                conn.execute("SET LOCAL ROLE brain_schema_migrator")
                conn.execute("SELECT brain_transition_policy('ats-v1', 'active')")
        conn.execute(
            "INSERT INTO brain_policy_release_gate_events "
            "(policy_version,lane,lifecycle,gate_name,gate_state,checked_by,report_artifact_hash) "
            "VALUES ('ats-v1','ats','active','parity','passed','owner',%s)",
            (HASHES[0],),
        )
        conn.execute("SET LOCAL ROLE brain_schema_migrator")
        conn.execute("SELECT brain_transition_policy('ats-v1', 'active')")
        conn.execute("SELECT brain_transition_policy('ats-v1', 'retired')")
        assert conn.execute(
            "SELECT lifecycle, retired_at IS NOT NULL AS retired FROM brain_decision_policies "
            "WHERE policy_version='ats-v1'"
        ).fetchone() == {"lifecycle": "retired", "retired": True}


def test_atomic_activation_and_retirement_leave_leased_rows_untouched(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        fleet_schema.ensure_schema_v3(conn)
        _grant_fleet_controller_contract(conn)
        conn.execute(
            "UPDATE fleet_decision_policies SET status='retired',retired_at=now() WHERE lane='ats' AND status='active'"
        )
        conn.execute("UPDATE fleet_config SET paused=TRUE,ats_paused=FALSE,ats_policy_version=NULL WHERE id=1")
        conn.commit()
        _install_fixture_data(conn)
        conn.execute("UPDATE brain_jobs SET canonical_url='https://jobs/1' WHERE job_id='job-1'")
        conn.execute(
            "INSERT INTO brain_jobs (job_id,source_namespace,source_job_id,canonical_url,title) "
            "VALUES ('job-3','legacy','3','https://jobs/3','Leased')"
        )
        conn.commit()
        for policy in ("ats-v1", "ats-v2"):
            _insert_policy(conn, policy)
            schema.ensure_policy_partition(conn, policy)
            _attach_policy_contract(conn, policy)
            _insert_decision(
                conn,
                decision_id=f"decision-{policy}",
                source_id=f"source-{policy}",
                policy=policy,
                input_hash=HASHES[1] if policy == "ats-v1" else HASHES[2],
            )
            if policy == "ats-v1":
                with pytest.raises(psycopg.errors.UniqueViolation):
                    _insert_decision(
                        conn,
                        decision_id="decision-v1-ambiguous",
                        source_id="source-v1-ambiguous",
                        policy=policy,
                        input_hash=HASHES[3],
                    )
            with conn.transaction():
                conn.execute("SET LOCAL ROLE brain_schema_migrator")
                conn.execute("SELECT brain_transition_policy(%s,'validated')", (policy,))
                conn.execute("SELECT brain_transition_policy(%s,'canary')", (policy,))
                if policy == "ats-v1":
                    conn.execute("SELECT brain_transition_policy(%s,'active')", (policy,))
        conn.execute(
            "INSERT INTO apply_queue "
            "(url,application_url,score,status,lane,approved_batch,decision_id,policy_version,decision_action,"
            "qualification_verdict,qualification_score,qualification_floor,preference_score,outcome_score,"
            "final_score,decision_confidence,decision_created_at,decision_expires_at,input_hash,lease_owner,"
            "lease_expires_at,worker_lease_id) VALUES "
            "('https://jobs/3','https://apply/3',0.8,'leased','ats','v1:leased','decision-ats-v1','ats-v1','apply',"
            "'qualified',0.8,0.6,0.7,0.9,0.8,0.8,now(),now()+interval '1 day',%s,'worker-1',"
            "now()+interval '5 minutes',gen_random_uuid()),"
            "('https://jobs/1','https://apply/1',0.8,'queued','ats','v1:queued','queued-v1','ats-v1','apply',"
            "'qualified',0.8,0.6,0.7,0.9,0.8,0.8,now(),now()+interval '1 day',%s,NULL,NULL,NULL)",
            (HASHES[1], HASHES[1]),
        )
        leased_before = conn.execute(
            "SELECT to_jsonb(q) AS row FROM apply_queue q WHERE url='https://jobs/3'"
        ).fetchone()["row"]
        conn.execute("SET LOCAL ROLE brain_schema_migrator")
        conn.execute("SELECT brain_transition_policy('ats-v2','active')")
        assert (
            conn.execute("SELECT to_jsonb(q) AS row FROM apply_queue q WHERE url='https://jobs/3'").fetchone()["row"]
            == leased_before
        )
        assert conn.execute(
            "SELECT policy_version,decision_id FROM apply_queue WHERE url='https://jobs/1'"
        ).fetchone() == {"policy_version": "ats-v2", "decision_id": "decision-ats-v2"}
        pause_before = conn.execute("SELECT paused,ats_paused,ats_apply_mode FROM fleet_config WHERE id=1").fetchone()
        conn.execute("SELECT brain_transition_policy('ats-v2','retired')")
        assert (
            conn.execute("SELECT lifecycle FROM brain_decision_policies WHERE policy_version='ats-v2'").fetchone()[
                "lifecycle"
            ]
            == "retired"
        )
        assert (
            conn.execute("SELECT status FROM fleet_decision_policies WHERE policy_version='ats-v2'").fetchone()[
                "status"
            ]
            == "retired"
        )
        assert (
            conn.execute("SELECT ats_policy_version FROM fleet_config WHERE id=1").fetchone()["ats_policy_version"]
            is None
        )
        assert conn.execute(
            "SELECT approved_batch,policy_version,decision_id FROM apply_queue WHERE url='https://jobs/1'"
        ).fetchone() == {"approved_batch": None, "policy_version": None, "decision_id": None}
        assert (
            conn.execute("SELECT to_jsonb(q) AS row FROM apply_queue q WHERE url='https://jobs/3'").fetchone()["row"]
            == leased_before
        )
        assert (
            conn.execute("SELECT paused,ats_paused,ats_apply_mode FROM fleet_config WHERE id=1").fetchone()
            == pause_before
        )


def test_parity_results_bind_exact_definition_and_protected_report(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        run_id, batch_ids = _insert_migration_run_and_batches(conn, 1)
        _claim_batch(conn, batch_ids[0])
        completed_batch = _complete_batch(conn, batch_ids[0])
        _checkpoint_batch(conn, batch_ids[0], completed_batch)
        completed_run = _complete_run(conn, run_id)
        _protect_artifact(conn, HASHES[0], "parity-report")
        parity_id = conn.execute(
            "INSERT INTO brain_parity_runs "
            "(migration_run_id,source_namespace,definition_version,completed_run_event_id,report_artifact_hash,"
            "final_delta_receipt_hash,writer_freeze_receipt_hash,started_at) "
            "VALUES (%s,'sqlite',1,%s,%s,%s,%s,now()) RETURNING parity_run_id",
            (run_id, completed_run, HASHES[0], HASHES[1], HASHES[2]),
        ).fetchone()["parity_run_id"]
        base_sql = (
            "INSERT INTO brain_parity_results "
            "(migration_run_id,source_namespace,parity_run_id,definition_version,check_key,table_name,check_type,"
            "source_count,target_count,source_hash,target_hash,report_artifact_hash) "
            "VALUES (%s,'sqlite',%s,1,'jobs',%s,%s,1,1,%s,%s,%s)"
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            with conn.transaction():
                conn.execute(
                    base_sql,
                    (run_id, parity_id, "brain_label_events", "canonical", HASHES[1], HASHES[1], HASHES[0]),
                )
        with pytest.raises((psycopg.errors.CheckViolation, psycopg.errors.ForeignKeyViolation)):
            with conn.transaction():
                conn.execute(
                    base_sql,
                    (run_id, parity_id, "brain_jobs", "evil", HASHES[1], HASHES[1], HASHES[0]),
                )
        with pytest.raises(psycopg.errors.CheckViolation, match="protected"):
            with conn.transaction():
                conn.execute(
                    base_sql,
                    (run_id, parity_id, "brain_jobs", "canonical", HASHES[1], HASHES[1], HASHES[3]),
                )


def test_gate_definition_version_cannot_cross_satisfy_transition(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        _insert_policy(conn)
        _protect_artifact(conn, HASHES[0], "gate-v2")
        conn.execute(
            "INSERT INTO brain_policy_gate_definitions (definition_version,lane,lifecycle,gate_name) "
            "SELECT 2,lane,lifecycle,gate_name FROM brain_policy_gate_definitions WHERE definition_version=1"
        )
        definitions = conn.execute(
            "SELECT gate_name FROM brain_policy_gate_definitions "
            "WHERE definition_version=2 AND lane='ats' AND lifecycle='validated'"
        ).fetchall()
        for definition in definitions:
            conn.execute(
                "INSERT INTO brain_policy_release_gate_events "
                "(policy_version,lane,lifecycle,definition_version,gate_name,gate_state,checked_by,report_artifact_hash) "
                "VALUES ('ats-v1','ats','validated',2,%s,'passed','owner',%s)",
                (definition["gate_name"], HASHES[0]),
            )
        conn.execute("SET LOCAL ROLE brain_schema_migrator")
        with pytest.raises(psycopg.errors.CheckViolation, match="latest mandatory gate"):
            conn.execute("SELECT brain_transition_policy('ats-v1','validated')")


def test_partition_catalog_rejects_extra_index_and_constraint(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        _install_fixture_data(conn)
        _insert_policy(conn)
        partition = schema.ensure_policy_partition(conn, "ats-v1")
        conn.execute(
            pg_sql.SQL("CREATE INDEX {} ON {}.{} (created_at)").format(
                pg_sql.Identifier("brain_partition_evil_idx"),
                pg_sql.Identifier("public"),
                pg_sql.Identifier(partition),
            )
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="partition.*index"):
            schema.verify_schema_v1(conn)
        conn.execute("DROP INDEX brain_partition_evil_idx")
        conn.execute(
            pg_sql.SQL("ALTER TABLE {}.{} ADD CONSTRAINT {} CHECK (true)").format(
                pg_sql.Identifier("public"),
                pg_sql.Identifier(partition),
                pg_sql.Identifier("brain_partition_evil_ck"),
            )
        )
        conn.commit()
        with pytest.raises(RuntimeError, match="partition.*constraint"):
            schema.verify_schema_v1(conn)


def test_exact_owner_rejects_membership_proxy_owner(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as conn:
        schema.ensure_schema_v1(conn)
        conn.execute("CREATE ROLE brain_proxy_owner NOLOGIN")
        conn.execute("GRANT brain_proxy_owner TO brain_schema_migrator")
        conn.execute("ALTER TABLE brain_jobs OWNER TO brain_proxy_owner")
        conn.commit()
        with pytest.raises(RuntimeError, match="ownership mismatch.*brain_jobs.*brain_proxy_owner"):
            schema.verify_schema_v1(conn)


def test_verifier_has_public_usage_and_only_enumerated_brain_reads(brain_pg):
    with psycopg.connect(brain_pg, row_factory=dict_row) as owner:
        fleet_schema.ensure_schema_v3(owner)
        _grant_fleet_controller_contract(owner)
        owner.commit()
        schema.ensure_schema_v1(owner)
        assert owner.execute(
            "SELECT has_schema_privilege('brain_schema_verifier','public','USAGE') AS public_usage,"
            "has_schema_privilege('brain_schema_verifier','public','CREATE') AS public_create,"
            "has_table_privilege('brain_schema_verifier','brain_jobs','SELECT') AS brain_read,"
            "has_table_privilege('brain_schema_verifier','fleet_config','SELECT') AS fleet_read"
        ).fetchone() == {
            "public_usage": True,
            "public_create": False,
            "brain_read": True,
            "fleet_read": False,
        }
    with psycopg.connect(_dsn_for(brain_pg, "brain_schema_verifier"), row_factory=dict_row) as verifier:
        assert verifier.execute("SELECT count(*) AS count FROM brain_jobs").fetchone()["count"] == 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            verifier.execute("SELECT count(*) FROM fleet_config")
