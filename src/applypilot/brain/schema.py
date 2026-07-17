"""Install and deeply verify the additive Postgres canonical-brain schema v1."""

from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path

from psycopg import sql
from psycopg.pq import TransactionStatus

_SCHEMA_SQL = Path(__file__).with_name("schema_v1.sql")
_SCHEMA_V2_SQL = Path(__file__).with_name("schema_v2.sql")
_SCHEMA_LOCK_KEY = "applypilot:brain:schema:v1"
_MIGRATION_NAME = "brain schema v1"
_MIGRATION_V2_NAME = "brain schema v2 lifecycle principals"
_MIGRATION_ROLE = "brain_schema_migrator"
_VERIFIER_ROLE = "brain_schema_verifier"
_STATUS_ROLE = "brain_status_reader"
_POLICY_CONTROLLER_ROLE = "brain_policy_controller"
_STATUS_READ_RELATIONS = {
    "brain_decision_policies",
    "brain_policy_artifacts",
    "brain_policy_approvals",
    "brain_parity_runs",
    "brain_parity_run_events",
}
_STATUS_READ_COLUMNS = {
    "fleet_config": {
        "id", "paused", "ats_paused", "ats_pause_source", "ats_apply_mode",
        "linkedin_apply_mode", "canary_enabled", "linkedin_canary_enabled",
        "canary_remaining", "linkedin_canary_remaining", "ats_policy_version",
        "linkedin_policy_version", "pinned_worker_version", "canary_worker_id",
        "canary_version", "approval_threshold", "spend_cap_usd", "linkedin_owner_ip",
    },
    "fleet_decision_policies": {
        "policy_version", "lane", "status", "activated_at", "retired_at",
    },
    "apply_queue": {
        "status", "lease_owner", "lease_expires_at", "decision_id", "policy_version",
        "decision_action", "qualification_verdict", "qualification_score",
        "qualification_floor", "preference_score", "outcome_score", "final_score",
        "decision_confidence", "decision_created_at", "decision_expires_at", "input_hash",
    },
    "linkedin_queue": {
        "status", "lease_owner", "lease_expires_at", "decision_id", "policy_version",
        "decision_action", "qualification_verdict", "qualification_score",
        "qualification_floor", "preference_score", "outcome_score", "final_score",
        "decision_confidence", "decision_created_at", "decision_expires_at", "input_hash",
    },
}
_CONTROLLER_FUNCTIONS = {
    "brain_controller_transition_policy",
    "brain_controller_arm_canary",
    "brain_controller_stop_canary",
}
_LIFECYCLE_LOCK_RELATIONS = {
    "apply_queue",
    "fleet_config",
    "fleet_decision_policies",
    "fleet_desired_state",
    "fleet_worker_principals",
    "linkedin_queue",
    "rate_governor",
    "worker_heartbeat",
    "workers",
}
_LIFECYCLE_OWNER_READ_RELATIONS = _LIFECYCLE_LOCK_RELATIONS | {
    "applied_set",
    "apply_attempts",
    "apply_result_events",
    "fleet_worker_blocklist",
}
_EXPECTED_CATALOG_HASH = "e966e54ccb0ed6f14059e0e0a1c185001f42490e2b48dc27a887662afe23259f"

_FUNCTIONS = {
    "brain_reject_mutation": "",
    "brain_register_decision": "",
    "brain_require_controller": "",
    "brain_artifact_is_authoritative": "candidate_hash text",
    "brain_check_controller_insert": "",
    "brain_check_migration_run_event": "",
    "brain_check_migration_batch_definition": "",
    "brain_check_migration_batch_event": "",
    "brain_check_migration_checkpoint": "",
    "brain_check_policy_lifecycle": "",
    "brain_bind_policy_gates": (
        "candidate text, candidate_lane text, target text, candidate_definition_version integer"
    ),
    "brain_check_supersession": "",
    "brain_check_parity_pass": "",
    "brain_check_parity_result": "",
    "brain_check_archive_manifest": "",
    "brain_transition_policy": "requested_policy_version text, requested_lifecycle text",
    "brain_controller_transition_policy": (
        "requested_policy_version text, requested_lifecycle text, expected_lane text"
    ),
    "brain_controller_arm_canary": (
        "requested_policy_version text, requested_lane text, requested_capacity integer, "
        "expected_ats_pause_source text, expect_null_ats_pause_source boolean, "
        "heartbeat_max_age_seconds integer"
    ),
    "brain_controller_stop_canary": "requested_lane text",
}


def _bounded_timeout(value: object, default: float = 30.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0 else default


_LOCK_TIMEOUT_SECONDS = _bounded_timeout(os.environ.get("APPLYPILOT_BRAIN_SCHEMA_LOCK_TIMEOUT_SECONDS"))

_RELATIONS = {
    "brain_schema_versions": "r",
    "brain_artifacts": "r",
    "brain_artifact_locations": "r",
    "brain_jobs": "r",
    "brain_job_aliases": "r",
    "brain_job_observations": "r",
    "brain_label_events": "r",
    "brain_pairwise_events": "r",
    "brain_email_events": "r",
    "brain_reviewed_outcomes": "r",
    "brain_applications": "r",
    "brain_application_events": "r",
    "brain_decision_policies": "r",
    "brain_policy_artifacts": "r",
    "brain_policy_approvals": "r",
    "brain_policy_gate_definitions": "r",
    "brain_policy_release_gate_events": "r",
    "brain_policy_transition_receipts": "r",
    "brain_policy_activation_receipts": "r",
    "brain_canary_lifecycle_events": "r",
    "brain_decision_identities": "r",
    "brain_job_decisions": "p",
    "brain_migration_sources": "r",
    "brain_migration_runs": "r",
    "brain_migration_run_events": "r",
    "brain_migration_batches": "r",
    "brain_migration_batch_events": "r",
    "brain_migration_checkpoints": "r",
    "brain_migration_quarantine": "r",
    "brain_parity_definitions": "r",
    "brain_parity_runs": "r",
    "brain_parity_results": "r",
    "brain_parity_run_events": "r",
}

# Key columns are verified by exact formatted type and nullability. Constraint and
# trigger checks below cover the remainder of each table's behavioral contract.
_KEY_COLUMNS = {
    ("brain_schema_versions", "migration_checksum"): ("text", True),
    ("brain_artifacts", "artifact_hash"): ("text", True),
    ("brain_artifacts", "request_id"): ("text", True),
    ("brain_artifact_locations", "durability"): ("text", True),
    ("brain_jobs", "job_id"): ("text", True),
    ("brain_job_aliases", "source_database_fingerprint"): ("text", True),
    ("brain_label_events", "job_id"): ("text", False),
    ("brain_pairwise_events", "left_job_id"): ("text", False),
    ("brain_reviewed_outcomes", "email_event_id"): ("bigint", True),
    ("brain_reviewed_outcomes", "weight"): ("numeric", False),
    ("brain_applications", "lane"): ("text", False),
    ("brain_application_events", "application_id"): ("text", True),
    ("brain_policy_artifacts", "artifact_role"): ("text", True),
    ("brain_canary_lifecycle_events", "prior_ats_pause_source"): ("text", False),
    ("brain_job_decisions", "decision_id"): ("text", True),
    ("brain_job_decisions", "policy_version"): ("text", True),
    ("brain_job_decisions", "confidence"): ("numeric", False),
    ("brain_job_decisions", "expires_at"): ("timestamp with time zone", False),
    ("brain_migration_batch_events", "lease_expires_at"): ("timestamp with time zone", False),
    ("brain_parity_results", "mismatch_count"): ("bigint", True),
}

_CONSTRAINTS = {
    "brain_canary_lifecycle_events_lane_check": ("lane", "ats", "linkedin"),
    "brain_canary_lifecycle_events_type_check": ("event_type", "armed", "stopped"),
    "brain_job_aliases_idempotent": ("UNIQUE NULLS NOT DISTINCT", "source_namespace", "source_database_fingerprint"),
    "brain_label_events_endpoint": (
        "CHECK",
        "job_id IS NOT NULL",
        "source_item_id IS NOT NULL",
        "source_item_url IS NOT NULL",
    ),
    "brain_pairwise_events_left_endpoint": ("CHECK", "left_job_id IS NOT NULL", "left_source_item_id IS NOT NULL"),
    "brain_pairwise_events_right_endpoint": ("CHECK", "right_job_id IS NOT NULL", "right_source_item_id IS NOT NULL"),
    "brain_reviewed_outcomes_email_job_fk": (
        "FOREIGN KEY (email_event_id, job_id)",
        "brain_email_events(email_event_id, job_id)",
    ),
    "brain_application_events_application_id_fkey": (
        "FOREIGN KEY (application_id)",
        "brain_applications(application_id)",
    ),
    "brain_policy_artifacts_pkey": ("PRIMARY KEY (policy_version, artifact_role)",),
    "brain_policy_release_gate_truth": (
        "CHECK",
        "gate_state <> 'passed'::text",
        "mismatch_count = 0",
        "unresolved_count = 0",
    ),
    "brain_policy_transition_receipts_gate_fk": (
        "FOREIGN KEY (policy_version, lane, lifecycle, definition_version, gate_name, gate_event_id, gate_state)",
        "brain_policy_release_gate_events(policy_version, lane, lifecycle, definition_version, gate_name, gate_event_id, gate_state)",
    ),
    "brain_job_decisions_identity_fk": (
        "FOREIGN KEY (policy_version, decision_id)",
        "brain_decision_identities(policy_version, decision_id)",
    ),
    "brain_job_decisions_policy_lane_fk": (
        "FOREIGN KEY (policy_version, lane)",
        "brain_decision_policies(policy_version, lane)",
    ),
    "brain_job_decisions_apply_expiry": (
        "CHECK",
        "action = 'apply'::text",
        "expires_at IS NOT NULL",
        "expires_at > created_at",
    ),
    "brain_job_decisions_policy_job_key": ("UNIQUE (policy_version, job_id)",),
    "brain_parity_results_definition_fk": (
        "FOREIGN KEY (definition_version, check_key, table_name, check_type)",
        "brain_parity_definitions(definition_version, check_key, relation_name, check_type)",
    ),
    "brain_migration_batches_lineage_key": (
        "UNIQUE (migration_run_id, source_namespace, source_table, migration_batch_id, batch_ordinal)",
    ),
    "brain_migration_batch_events_batch_fk": (
        "FOREIGN KEY (migration_run_id, source_namespace, source_table, migration_batch_id, batch_ordinal)",
        "brain_migration_batches(migration_run_id, source_namespace, source_table, migration_batch_id, batch_ordinal)",
    ),
    "brain_migration_checkpoints_completed_event_fk": (
        "FOREIGN KEY (migration_run_id, source_namespace, source_table, migration_batch_id, batch_ordinal, migration_batch_event_id, committed_event_type)",
        "brain_migration_batch_events",
    ),
    "brain_migration_quarantine_batch_fk": (
        "FOREIGN KEY (migration_run_id, source_namespace, source_table, migration_batch_id, batch_ordinal)",
        "brain_migration_batches",
    ),
    "brain_parity_results_run_fk": (
        "FOREIGN KEY (migration_run_id, source_namespace, parity_run_id, definition_version, report_artifact_hash)",
        "brain_parity_runs(migration_run_id, source_namespace, parity_run_id, definition_version, report_artifact_hash)",
    ),
    "brain_parity_results_check_key": ("UNIQUE (parity_run_id, definition_version, check_key)",),
}

_INDEXES = {
    "brain_decision_policies_one_active_per_lane": (True, "(lifecycle = 'active'::text)"),
    "brain_job_observations_one_successor": (True, "(supersedes_observation_id IS NOT NULL)"),
    "brain_label_events_one_successor": (True, "(supersedes_label_event_id IS NOT NULL)"),
    "brain_pairwise_events_one_successor": (True, "(supersedes_pairwise_event_id IS NOT NULL)"),
    "brain_job_decisions_job_created_idx": (False, None),
    "brain_job_decisions_policy_action_idx": (False, None),
    "brain_jobs_canonical_url_key": (True, "(canonical_url IS NOT NULL)"),
    "brain_migration_batch_events_claim_idx": (False, None),
    "brain_migration_batches_range_idx": (False, None),
    "brain_migration_checkpoints_run_idx": (False, None),
    "brain_migration_quarantine_run_idx": (False, None),
    "brain_parity_results_run_idx": (False, None),
}

_IMMUTABLE_TABLES = {
    "brain_schema_versions",
    "brain_artifacts",
    "brain_artifact_locations",
    "brain_job_aliases",
    "brain_job_observations",
    "brain_label_events",
    "brain_pairwise_events",
    "brain_email_events",
    "brain_reviewed_outcomes",
    "brain_application_events",
    "brain_policy_artifacts",
    "brain_policy_approvals",
    "brain_policy_gate_definitions",
    "brain_policy_release_gate_events",
    "brain_policy_transition_receipts",
    "brain_policy_activation_receipts",
    "brain_canary_lifecycle_events",
    "brain_decision_identities",
    "brain_job_decisions",
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


def _schema_bytes() -> bytes:
    return _SCHEMA_SQL.read_bytes()


def _schema_checksum() -> str:
    return hashlib.sha256(_schema_bytes()).hexdigest()


def _schema_v2_bytes() -> bytes:
    return _SCHEMA_V2_SQL.read_bytes()


def _schema_v2_checksum() -> str:
    return hashlib.sha256(_schema_v2_bytes()).hexdigest()


def _require_idle(conn) -> None:
    if conn.info.transaction_status != TransactionStatus.IDLE:
        raise RuntimeError("brain schema helpers require an idle connection and never commit or roll back caller work")


def _identity(cur) -> tuple[str, bool]:
    cur.execute("SELECT current_user AS name, rolsuper FROM pg_roles WHERE rolname = current_user")
    row = cur.fetchone()
    return row["name"], bool(row["rolsuper"])


def _activate_migration_identity(cur) -> str:
    cur.execute(
        "WITH RECURSIVE memberships(roleid) AS ("
        "SELECT oid FROM pg_roles WHERE rolname=session_user UNION "
        "SELECT membership.roleid FROM pg_auth_members membership "
        "JOIN memberships prior ON prior.roleid=membership.member) "
        "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=%s) AS present, "
        "session_user AS session_name, EXISTS (SELECT 1 FROM memberships WHERE roleid="
        "(SELECT oid FROM pg_roles WHERE rolname=%s)) AS member",
        (_MIGRATION_ROLE, _MIGRATION_ROLE),
    )
    row = cur.fetchone()
    if not row["present"] or not row["member"]:
        raise RuntimeError(
            f"brain schema migration requires explicit session_user membership in fixed role {_MIGRATION_ROLE}"
        )
    cur.execute(
        "SELECT COALESCE(grantee.rolname,'PUBLIC') AS grantee FROM pg_namespace n "
        "CROSS JOIN LATERAL aclexplode(n.nspacl) acl "
        "LEFT JOIN pg_roles grantee ON grantee.oid=acl.grantee "
        "WHERE n.nspname='public' AND acl.privilege_type='CREATE' "
        "AND COALESCE(grantee.rolname,'PUBLIC') NOT IN (%s,'pg_database_owner')",
        (_MIGRATION_ROLE,),
    )
    for grant in cur.fetchall():
        grantee = sql.SQL("PUBLIC") if grant["grantee"] == "PUBLIC" else sql.Identifier(grant["grantee"])
        cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(grantee))
    cur.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(_MIGRATION_ROLE)))
    current_user, _ = _identity(cur)
    if current_user != _MIGRATION_ROLE:
        raise RuntimeError("failed to activate fixed brain migration role")
    return current_user


def _verifier_identity() -> str:
    return _VERIFIER_ROLE


def _apply_acl_contract(cur, migration_identity: str) -> None:
    verifier_identity = _verifier_identity()
    cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname=%s) AS present", (verifier_identity,))
    if not cur.fetchone()["present"]:
        raise RuntimeError(f"fixed brain verifier role does not exist: {verifier_identity}")

    cur.execute(
        "SELECT COALESCE(grantee.rolname, 'PUBLIC') AS grantee "
        "FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) acl "
        "LEFT JOIN pg_roles grantee ON grantee.oid=acl.grantee "
        "WHERE n.nspname='public' AND acl.privilege_type='CREATE' "
        "AND COALESCE(grantee.rolname, 'PUBLIC') NOT IN (%s, 'pg_database_owner')",
        (migration_identity,),
    )
    for row in cur.fetchall():
        grantee = sql.SQL("PUBLIC") if row["grantee"] == "PUBLIC" else sql.Identifier(row["grantee"])
        cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(grantee))

    verifier = sql.Identifier(verifier_identity)
    cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA public, brain_archive FROM {}").format(verifier))
    cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public, brain_archive TO {}").format(verifier))
    for relation in _RELATIONS:
        cur.execute("SELECT to_regclass(%s) AS relation", (f"public.{relation}",))
        if cur.fetchone()["relation"] is None:
            continue
        cur.execute(
            sql.SQL("GRANT SELECT ON TABLE {}.{} TO {}").format(
                sql.Identifier("public"), sql.Identifier(relation), verifier
            )
        )
    cur.execute(
        sql.SQL("GRANT SELECT ON TABLE {}.{} TO {}").format(
            sql.Identifier("brain_archive"), sql.Identifier("brain_archive_manifests"), verifier
        )
    )
    for relation in ("fleet_config", "fleet_decision_policies", "apply_queue", "linkedin_queue"):
        cur.execute("SELECT to_regclass(%s) AS relation", (f"public.{relation}",))
        if cur.fetchone()["relation"] is not None:
            cur.execute(
                "SELECT has_table_privilege(%s,%s,'SELECT,INSERT,UPDATE') AS allowed",
                (migration_identity, f"public.{relation}"),
            )
            if not cur.fetchone()["allowed"]:
                raise RuntimeError(
                    f"fixed controller role {migration_identity} requires SELECT, INSERT, UPDATE on public.{relation}"
                )


def _version_rows(cur):
    cur.execute("SELECT to_regclass('public.brain_schema_versions') AS relation")
    if cur.fetchone()["relation"] is None:
        return []
    cur.execute(
        "SELECT version, migration_name, migration_checksum, applied_at, applied_by "
        "FROM public.brain_schema_versions ORDER BY version"
    )
    rows = cur.fetchall()
    if not rows:
        raise RuntimeError("brain schema version ledger exists but is empty")
    versions = [row["version"] for row in rows]
    if versions not in ([1], [1, 2]):
        raise RuntimeError(
            "unsupported or non-contiguous brain schema version ledger: "
            + ", ".join(str(version) for version in versions)
        )
    return rows


def _version_row(cur):
    rows = _version_rows(cur)
    return rows[0] if rows else None


def _assert_existing_ownership(cur, migration_identity: str) -> None:
    cur.execute(
        "SELECT n.nspname, c.relname, owner.rolname AS owner_name "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_roles owner ON owner.oid = c.relowner "
        "WHERE (n.nspname = 'public' AND left(c.relname, 6) = 'brain_') "
        "OR n.nspname = 'brain_archive'"
    )
    owned_objects = [(f"{row['nspname']}.{row['relname']}", row["owner_name"]) for row in cur.fetchall()]
    cur.execute(
        "SELECT n.nspname, p.proname, owner.rolname AS owner_name "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "JOIN pg_roles owner ON owner.oid = p.proowner "
        "WHERE n.nspname = 'public' AND left(p.proname, 6) = 'brain_'"
    )
    owned_objects.extend((f"{row['nspname']}.{row['proname']}()", row["owner_name"]) for row in cur.fetchall())
    cur.execute(
        "SELECT n.nspname, owner.rolname AS owner_name FROM pg_namespace n "
        "JOIN pg_roles owner ON owner.oid = n.nspowner WHERE n.nspname = 'brain_archive'"
    )
    owned_objects.extend((row["nspname"], row["owner_name"]) for row in cur.fetchall())
    invalid = []
    for object_name, owner_name in owned_objects:
        if owner_name != migration_identity:
            invalid.append(f"{object_name} owned by {owner_name}")
    if invalid:
        raise RuntimeError(
            "existing brain objects are not owned by the migration identity or one of its roles: "
            + ", ".join(sorted(invalid))
        )


def _acquire_xact_lock(cur, timeout_seconds: float) -> None:
    deadline = time.monotonic() + _bounded_timeout(timeout_seconds)
    while True:
        cur.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s)) AS acquired",
            (_SCHEMA_LOCK_KEY,),
        )
        if cur.fetchone()["acquired"]:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "timed out waiting for the brain schema v1 migration lock; another owner may be stalled while migrating"
            )
        time.sleep(0.05)


def _catalog_contract_hash(cur) -> str:
    relations = list(_RELATIONS)
    cur.execute(
        "SELECT n.nspname AS schema_name,c.relname,a.attnum,a.attname,"
        "format_type(a.atttypid,a.atttypmod) AS data_type,a.attnotnull,a.attidentity,a.attgenerated,"
        "pg_get_expr(d.adbin,d.adrelid,true) AS default_expr "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped "
        "LEFT JOIN pg_attrdef d ON d.adrelid=c.oid AND d.adnum=a.attnum "
        "WHERE (n.nspname='public' AND c.relname=ANY(%s)) "
        "OR (n.nspname='brain_archive' AND c.relname='brain_archive_manifests') "
        "ORDER BY n.nspname,c.relname,a.attnum",
        (relations,),
    )
    columns = cur.fetchall()
    cur.execute(
        "SELECT n.nspname AS schema_name,c.relname,con.conname,con.contype,con.convalidated,"
        "pg_get_constraintdef(con.oid,true) AS definition "
        "FROM pg_constraint con JOIN pg_class c ON c.oid=con.conrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE (n.nspname='public' AND c.relname=ANY(%s)) "
        "OR (n.nspname='brain_archive' AND c.relname='brain_archive_manifests') "
        "ORDER BY n.nspname,c.relname,con.conname",
        (relations,),
    )
    constraints = cur.fetchall()
    cur.execute(
        "SELECT n.nspname AS schema_name,t.relname AS relation_name,i.relname AS index_name,"
        "x.indisunique,x.indisprimary,x.indisvalid,x.indisready,"
        "pg_get_indexdef(i.oid) AS definition,pg_get_expr(x.indpred,x.indrelid,true) AS predicate,"
        "ARRAY(SELECT opc.opcname FROM unnest(x.indclass::oid[]) WITH ORDINALITY value(opcoid,ordinal) "
        "JOIN pg_opclass opc ON opc.oid=value.opcoid ORDER BY value.ordinal) AS opclasses "
        "FROM pg_index x JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_class t ON t.oid=x.indrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE (n.nspname='public' AND t.relname=ANY(%s)) "
        "OR (n.nspname='brain_archive' AND t.relname='brain_archive_manifests') "
        "ORDER BY n.nspname,t.relname,i.relname",
        (relations,),
    )
    indexes = cur.fetchall()
    cur.execute(
        "SELECT n.nspname AS schema_name,c.relname,t.tgname,t.tgenabled,"
        "pg_get_triggerdef(t.oid,true) AS definition,p.proname AS function_name,"
        "pg_get_function_identity_arguments(p.oid) AS function_arguments "
        "FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_proc p ON p.oid=t.tgfoid WHERE NOT t.tgisinternal AND "
        "((n.nspname='public' AND c.relname=ANY(%s)) "
        "OR (n.nspname='brain_archive' AND c.relname='brain_archive_manifests')) "
        "ORDER BY n.nspname,c.relname,t.tgname",
        (relations,),
    )
    triggers = cur.fetchall()
    cur.execute(
        "SELECT p.proname,pg_get_function_identity_arguments(p.oid) AS arguments,"
        "pg_get_function_result(p.oid) AS result,l.lanname AS language,p.prosecdef,p.provolatile,"
        "p.proparallel,p.proisstrict,COALESCE(p.proconfig,ARRAY[]::text[]) AS config,"
        "regexp_replace(p.prosrc,'\\s+',' ','g') AS normalized_body "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace JOIN pg_language l ON l.oid=p.prolang "
        "WHERE n.nspname='public' AND left(p.proname,6)='brain_' "
        "ORDER BY p.proname,pg_get_function_identity_arguments(p.oid)"
    )
    functions = cur.fetchall()
    payload = {
        "columns": columns,
        "constraints": constraints,
        "indexes": indexes,
        "triggers": triggers,
        "functions": functions,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _verify_contract(cur) -> None:
    checksum = _schema_checksum()
    versions = _version_rows(cur)
    version = versions[0] if versions else None
    problems: list[str] = []
    if version is None:
        problems.append("missing schema version 1")
        migration_identity = None
    else:
        migration_identity = _MIGRATION_ROLE
        if version["migration_name"] != _MIGRATION_NAME:
            problems.append("migration name mismatch")
        if version["migration_checksum"] != checksum:
            problems.append("migration checksum mismatch")
        if version["applied_by"] != _MIGRATION_ROLE:
            problems.append(f"migration ledger owner mismatch: expected {_MIGRATION_ROLE}, got {version['applied_by']}")
    version_v2 = versions[1] if len(versions) > 1 else None
    if version_v2 is None:
        problems.append("missing schema version 2 lifecycle principal contract")
    else:
        if version_v2["migration_name"] != _MIGRATION_V2_NAME:
            problems.append("migration v2 name mismatch")
        if version_v2["migration_checksum"] != _schema_v2_checksum():
            problems.append("migration v2 checksum mismatch")
        if version_v2["applied_by"] != _MIGRATION_ROLE:
            problems.append(
                f"migration v2 ledger owner mismatch: expected {_MIGRATION_ROLE}, "
                f"got {version_v2['applied_by']}"
            )

    cur.execute(
        "SELECT c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relname = ANY(%s)",
        (list(_RELATIONS),),
    )
    relations = {row["relname"]: row["relkind"] for row in cur.fetchall()}
    for name, kind in _RELATIONS.items():
        if relations.get(name) != kind:
            problems.append(f"relation {name} expected kind {kind}, got {relations.get(name)!r}")

    cur.execute(
        "SELECT c.relname, a.attname, format_type(a.atttypid, a.atttypmod) AS data_type, "
        "a.attnotnull FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped "
        "WHERE n.nspname = 'public' AND c.relname = ANY(%s)",
        (list(_RELATIONS),),
    )
    columns = {(row["relname"], row["attname"]): (row["data_type"], bool(row["attnotnull"])) for row in cur.fetchall()}
    for key, signature in _KEY_COLUMNS.items():
        if columns.get(key) != signature:
            problems.append(f"column {key[0]}.{key[1]} expected {signature}, got {columns.get(key)!r}")

    cur.execute(
        "SELECT conname, convalidated, pg_get_constraintdef(con.oid, true) AS definition "
        "FROM pg_constraint con JOIN pg_namespace n ON n.oid = con.connamespace "
        "WHERE n.nspname IN ('public', 'brain_archive') AND conname = ANY(%s)",
        (list(_CONSTRAINTS),),
    )
    constraints = {row["conname"]: row for row in cur.fetchall()}
    for name, required_fragments in _CONSTRAINTS.items():
        constraint = constraints.get(name)
        if constraint is None:
            problems.append(f"missing constraint: {name}")
        elif not constraint["convalidated"]:
            problems.append(f"constraint {name} is not validated")
        elif any(fragment not in constraint["definition"] for fragment in required_fragments):
            problems.append(f"constraint {name} definition mismatch: {constraint['definition']}")

    cur.execute(
        "SELECT c.relname AS index_name, i.indisunique, i.indisvalid, i.indisready, "
        "pg_get_expr(i.indpred, i.indrelid) AS predicate "
        "FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relname=ANY(%s)",
        (list(_INDEXES),),
    )
    indexes = {row["index_name"]: row for row in cur.fetchall()}
    for name, (unique, predicate) in _INDEXES.items():
        index = indexes.get(name)
        if index is None:
            problems.append(f"missing index: {name}")
        elif not index["indisvalid"] or not index["indisready"]:
            problems.append(f"index {name} is not valid and ready")
        elif bool(index["indisunique"]) != unique or index["predicate"] != predicate:
            problems.append(
                f"index {name} signature mismatch: unique={index['indisunique']} predicate={index['predicate']!r}"
            )

    cur.execute(
        "SELECT c.relname, t.tgname, t.tgenabled, t.tgtype, p.proname, pg_get_triggerdef(t.oid, true) AS definition "
        "FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "JOIN pg_proc p ON p.oid=t.tgfoid "
        "WHERE NOT t.tgisinternal AND n.nspname IN ('public', 'brain_archive')"
    )
    triggers = {(row["relname"], row["tgname"]): row for row in cur.fetchall()}
    for table in _IMMUTABLE_TABLES:
        for suffix, trigger_type in (("append_only", 27), ("append_only_truncate", 34)):
            trigger = triggers.get((table, f"{table}_{suffix}"))
            if trigger is None:
                problems.append(f"missing {suffix} trigger on {table}")
            elif trigger["tgenabled"] != "O" or trigger["proname"] != "brain_reject_mutation":
                problems.append(f"trigger {table}_{suffix} binding or enabled state mismatch")
            elif trigger["tgtype"] != trigger_type:
                problems.append(f"trigger {table}_{suffix} event definition mismatch")
    for suffix, trigger_type in (("append_only", 27), ("append_only_truncate", 34)):
        trigger = triggers.get(("brain_archive_manifests", f"brain_archive_manifests_{suffix}"))
        if trigger is None or trigger["tgenabled"] != "O" or trigger["proname"] != "brain_reject_mutation":
            problems.append(f"archive manifest {suffix} trigger binding or enabled state mismatch")
        elif trigger["tgtype"] != trigger_type:
            problems.append(f"archive manifest {suffix} trigger event definition mismatch")
    for table in (
        "brain_job_observations",
        "brain_label_events",
        "brain_pairwise_events",
        "brain_email_events",
        "brain_reviewed_outcomes",
        "brain_application_events",
        "brain_migration_run_events",
        "brain_migration_batch_events",
    ):
        trigger = triggers.get((table, f"{table}_supersession"))
        if (
            trigger is None
            or trigger["tgenabled"] != "O"
            or trigger["proname"] != "brain_check_supersession"
            or trigger["tgtype"] != 5
        ):
            problems.append(f"supersession trigger on {table} has an invalid binding or enabled state")
    for table, name, function, trigger_type in (
        ("brain_job_decisions", "brain_job_decisions_register", "brain_register_decision", 7),
        ("brain_decision_policies", "brain_decision_policies_lifecycle", "brain_check_policy_lifecycle", 23),
    ):
        trigger = triggers.get((table, name))
        if (
            trigger is None
            or trigger["tgenabled"] != "O"
            or trigger["proname"] != function
            or trigger["tgtype"] != trigger_type
        ):
            problems.append(f"trigger {name} binding or enabled state mismatch")
    for table, name, function in (
        ("brain_parity_run_events", "brain_parity_run_events_pass", "brain_check_parity_pass"),
        ("brain_parity_results", "brain_parity_results_guard", "brain_check_parity_result"),
        ("brain_archive_manifests", "brain_archive_manifests_guard", "brain_check_archive_manifest"),
    ):
        trigger = triggers.get((table, name))
        if trigger is None or trigger["tgenabled"] != "O" or trigger["proname"] != function:
            problems.append(f"trigger {name} binding or enabled state mismatch")

    cur.execute(
        "SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS arguments, p.prosecdef "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='public' AND p.proname=ANY(%s)",
        (list(_FUNCTIONS),),
    )
    functions = {row["proname"]: row for row in cur.fetchall()}
    for name, arguments in _FUNCTIONS.items():
        function = functions.get(name)
        expected_security_definer = name == "brain_transition_policy" or name in _CONTROLLER_FUNCTIONS
        if (
            function is None
            or function["arguments"] != arguments
            or bool(function["prosecdef"]) != expected_security_definer
        ):
            problems.append(f"function {name} signature or security mode mismatch")

    cur.execute(
        "SELECT pt.partstrat, pg_get_partkeydef(pt.partrelid) AS partkey "
        "FROM pg_partitioned_table pt WHERE pt.partrelid = "
        "to_regclass('public.brain_job_decisions')"
    )
    partition = cur.fetchone()
    if partition is None or partition["partstrat"] != "l" or partition["partkey"] != "LIST (policy_version)":
        problems.append("brain_job_decisions is not LIST partitioned by policy_version")
    cur.execute(
        "SELECT c.relname, pg_get_expr(c.relpartbound, c.oid) AS bound "
        "FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid "
        "WHERE i.inhparent = to_regclass('public.brain_job_decisions')"
    )
    children = {row["relname"]: row["bound"] for row in cur.fetchall()}
    if "DEFAULT" in children.values():
        problems.append("default policy decision partition is forbidden")
    for child in children:
        for trigger_name, function_name, trigger_type in (
            ("brain_job_decisions_register", "brain_register_decision", 7),
            ("brain_job_decisions_append_only", "brain_reject_mutation", 27),
            ("brain_job_decisions_append_only_truncate", "brain_reject_mutation", 34),
        ):
            trigger = triggers.get((child, trigger_name))
            if (
                trigger is None
                or trigger["tgenabled"] != "O"
                or trigger["proname"] != function_name
                or trigger["tgtype"] != trigger_type
            ):
                problems.append(f"partition {child} trigger {trigger_name} contract mismatch")
        cur.execute(
            "SELECT conname,contype,convalidated,pg_get_constraintdef(oid,true) AS definition "
            "FROM pg_constraint WHERE conrelid='public.brain_job_decisions'::regclass "
            "ORDER BY conname"
        )
        parent_constraint_rows = cur.fetchall()
        cur.execute(
            "SELECT conname,contype,convalidated,pg_get_constraintdef(oid,true) AS definition "
            "FROM pg_constraint WHERE conrelid=%s::regclass ORDER BY conname",
            (f"public.{child}",),
        )
        child_constraint_rows = cur.fetchall()
        parent_constraints = sorted(
            (row["contype"], row["convalidated"], row["definition"]) for row in parent_constraint_rows
        )
        child_constraints = sorted(
            (row["contype"], row["convalidated"], row["definition"]) for row in child_constraint_rows
        )
        if child_constraints != parent_constraints:
            problems.append(
                f"partition {child} constraint contract mismatch: "
                f"expected={parent_constraints!r} actual={child_constraints!r}"
            )

        cur.execute(
            "SELECT index_class.relname AS parent_index,idx.indisunique,idx.indisprimary,idx.indisvalid,"
            "idx.indisready,idx.indnkeyatts,idx.indnatts,idx.indkey::text AS keys,"
            "idx.indclass::text AS opclasses,idx.indoption::text AS options,"
            "pg_get_expr(idx.indexprs,idx.indrelid,true) AS expressions,"
            "pg_get_expr(idx.indpred,idx.indrelid,true) AS predicate "
            "FROM pg_index idx JOIN pg_class index_class ON index_class.oid=idx.indexrelid "
            "WHERE idx.indrelid='public.brain_job_decisions'::regclass ORDER BY index_class.relname"
        )
        parent_indexes = {row["parent_index"]: tuple(row.values())[1:] for row in cur.fetchall()}
        cur.execute(
            "SELECT parent_class.relname AS parent_index,child_class.relname AS child_index,"
            "idx.indisunique,idx.indisprimary,idx.indisvalid,idx.indisready,idx.indnkeyatts,idx.indnatts,"
            "idx.indkey::text AS keys,idx.indclass::text AS opclasses,idx.indoption::text AS options,"
            "pg_get_expr(idx.indexprs,idx.indrelid,true) AS expressions,"
            "pg_get_expr(idx.indpred,idx.indrelid,true) AS predicate "
            "FROM pg_index idx JOIN pg_class child_class ON child_class.oid=idx.indexrelid "
            "LEFT JOIN pg_inherits inheritance ON inheritance.inhrelid=idx.indexrelid "
            "LEFT JOIN pg_class parent_class ON parent_class.oid=inheritance.inhparent "
            "WHERE idx.indrelid=%s::regclass ORDER BY child_class.relname",
            (f"public.{child}",),
        )
        child_index_rows = cur.fetchall()
        child_indexes = {
            row["parent_index"]: tuple(row.values())[2:] for row in child_index_rows if row["parent_index"] is not None
        }
        if (
            any(row["parent_index"] is None for row in child_index_rows)
            or set(child_indexes) != set(parent_indexes)
            or any(child_indexes[name] != parent_indexes[name] for name in parent_indexes)
        ):
            problems.append(f"partition {child} index contract mismatch")
    allowed_public_relations = set(_RELATIONS) | set(children)
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND left(c.relname,6)='brain_' AND c.relkind IN ('r','p')"
    )
    unknown_relations = sorted(
        row["relname"] for row in cur.fetchall() if row["relname"] not in allowed_public_relations
    )
    if unknown_relations:
        problems.append("unenumerated brain relations: " + ", ".join(unknown_relations))
    cur.execute(
        "SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'brain_archive' AND c.relname = 'brain_archive_manifests'"
    )
    archive_relation = cur.fetchone()
    if archive_relation is None or archive_relation["relkind"] != "r":
        problems.append("brain_archive_manifests is missing or not an ordinary table")

    if migration_identity is not None:
        cur.execute(
            "SELECT n.nspname, c.relname, owner.rolname AS owner_name "
            "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "JOIN pg_roles owner ON owner.oid = c.relowner "
            "WHERE (n.nspname = 'public' AND left(c.relname, 6) = 'brain_') "
            "OR n.nspname = 'brain_archive'"
        )
        for row in cur.fetchall():
            if row["owner_name"] != migration_identity:
                problems.append(f"ownership mismatch: {row['nspname']}.{row['relname']} owned by {row['owner_name']}")
        cur.execute(
            "SELECT n.nspname, p.proname, owner.rolname AS owner_name "
            "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "JOIN pg_roles owner ON owner.oid = p.proowner "
            "WHERE n.nspname = 'public' AND left(p.proname, 6) = 'brain_'"
        )
        for row in cur.fetchall():
            if row["owner_name"] != migration_identity:
                problems.append(f"ownership mismatch: {row['nspname']}.{row['proname']}() owned by {row['owner_name']}")
        cur.execute(
            "SELECT owner.rolname AS owner_name FROM pg_namespace n "
            "JOIN pg_roles owner ON owner.oid = n.nspowner WHERE n.nspname = 'brain_archive'"
        )
        archive_owner = cur.fetchone()
        if archive_owner is None:
            problems.append("missing brain_archive schema")
        else:
            if archive_owner["owner_name"] != migration_identity:
                problems.append(f"ownership mismatch: brain_archive owned by {archive_owner['owner_name']}")

    catalog_hash = _catalog_contract_hash(cur)
    if _EXPECTED_CATALOG_HASH and catalog_hash != _EXPECTED_CATALOG_HASH:
        problems.append(f"catalog contract hash mismatch: expected {_EXPECTED_CATALOG_HASH}, got {catalog_hash}")

    cur.execute(
        "SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,"
        "rolbypassrls,rolinherit FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname",
        ([_POLICY_CONTROLLER_ROLE, _STATUS_ROLE],),
    )
    capability_roles = cur.fetchall()
    if len(capability_roles) != 2:
        problems.append("missing fixed lifecycle capability roles")
    for role in capability_roles:
        unsafe = any(
            role[key]
            for key in (
                "rolcanlogin",
                "rolsuper",
                "rolcreatedb",
                "rolcreaterole",
                "rolreplication",
                "rolbypassrls",
                "rolinherit",
            )
        )
        if unsafe:
            problems.append(f"unsafe lifecycle capability role attributes: {role['rolname']}")
    cur.execute(
        "SELECT member_role.rolname AS member_role,parent_role.rolname AS parent_role "
        "FROM pg_auth_members membership "
        "JOIN pg_roles member_role ON member_role.oid=membership.member "
        "JOIN pg_roles parent_role ON parent_role.oid=membership.roleid "
        "WHERE member_role.rolname=ANY(%s) OR parent_role.rolname=ANY(%s)",
        (
            [_POLICY_CONTROLLER_ROLE, _STATUS_ROLE],
            [_MIGRATION_ROLE, _POLICY_CONTROLLER_ROLE, _STATUS_ROLE],
        ),
    )
    unsafe_memberships = [
        f"{row['member_role']}->{row['parent_role']}"
        for row in cur.fetchall()
        if row["member_role"] in {_POLICY_CONTROLLER_ROLE, _STATUS_ROLE}
    ]
    if unsafe_memberships:
        problems.append("unsafe lifecycle role membership: " + ", ".join(unsafe_memberships))
    cur.execute(
        "WITH RECURSIVE memberships(login_oid,roleid) AS ("
        "SELECT oid,oid FROM pg_roles WHERE rolcanlogin UNION "
        "SELECT prior.login_oid,membership.roleid FROM memberships prior "
        "JOIN pg_auth_members membership ON membership.member=prior.roleid) "
        "SELECT DISTINCT login.rolname AS login_name,capability.rolname AS capability "
        "FROM memberships JOIN pg_roles login ON login.oid=memberships.login_oid "
        "JOIN pg_roles capability ON capability.oid=memberships.roleid "
        "WHERE capability.rolname=ANY(%s)",
        ([_POLICY_CONTROLLER_ROLE, _STATUS_ROLE],),
    )
    for lifecycle_login in cur.fetchall():
        login_name = lifecycle_login["login_name"]
        capability_name = lifecycle_login["capability"]
        cur.execute(
            "WITH RECURSIVE memberships(roleid) AS ("
            "SELECT oid FROM pg_roles WHERE rolname=%s UNION "
            "SELECT member.roleid FROM pg_auth_members member "
            "JOIN memberships prior ON prior.roleid=member.member) "
            "SELECT EXISTS(SELECT 1 FROM memberships JOIN pg_roles role ON role.oid=memberships.roleid "
            "WHERE role.rolname<>%s AND role.rolname<>%s) AS extra_membership,"
            "EXISTS(SELECT 1 FROM pg_roles role WHERE role.rolname=%s AND "
            "(role.rolsuper OR role.rolcreatedb OR role.rolcreaterole OR role.rolreplication "
            "OR role.rolbypassrls)) AS unsafe_identity,"
            "has_schema_privilege(%s,'public','CREATE') AS public_create,"
            "EXISTS(SELECT 1 FROM pg_class relation JOIN pg_namespace namespace "
            "ON namespace.oid=relation.relnamespace WHERE namespace.nspname='public' "
            "AND relation.relkind IN ('r','p','v','m','f') AND (has_table_privilege(%s,relation.oid,"
            "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR "
            "has_any_column_privilege(%s,relation.oid,'INSERT,UPDATE,REFERENCES'))) AS mutation_authority,"
            "EXISTS(SELECT 1 FROM pg_proc function JOIN pg_namespace namespace "
            "ON namespace.oid=function.pronamespace CROSS JOIN LATERAL aclexplode(function.proacl) acl "
            "WHERE namespace.nspname='public' AND acl.grantee="
            "(SELECT oid FROM pg_roles WHERE rolname=%s)) AS direct_function_grant",
            (
                login_name,
                login_name,
                capability_name,
                login_name,
                login_name,
                login_name,
                login_name,
                login_name,
            ),
        )
        login_contract = cur.fetchone()
        if login_contract is None or any(login_contract.values()):
            problems.append(
                f"lifecycle login {login_name} exceeds {capability_name} capability"
            )
    cur.execute(
        "SELECT p.proname,has_function_privilege(%s,p.oid,'EXECUTE') AS controller_execute,"
        "has_function_privilege(%s,p.oid,'EXECUTE') AS status_execute "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='public' AND p.proname=ANY(%s) ORDER BY p.proname",
        (
            _POLICY_CONTROLLER_ROLE,
            _STATUS_ROLE,
            sorted(_CONTROLLER_FUNCTIONS | {"brain_transition_policy"}),
        ),
    )
    lifecycle_function_acl = cur.fetchall()
    if len(lifecycle_function_acl) != 4 or any(
        row["status_execute"]
        or row["controller_execute"] != (row["proname"] in _CONTROLLER_FUNCTIONS)
        for row in lifecycle_function_acl
    ):
        problems.append("lifecycle function ACL contract mismatch")
    missing_owner_privileges: list[str] = []
    for privilege, relations in (
        ("SELECT", _LIFECYCLE_OWNER_READ_RELATIONS),
        ("UPDATE", _LIFECYCLE_LOCK_RELATIONS),
        ("INSERT", {"fleet_decision_policies"}),
    ):
        cur.execute(
            "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname='public' AND c.relname=ANY(%s) "
            "AND NOT has_table_privilege(%s,c.oid,%s) ORDER BY c.relname",
            (sorted(relations), _MIGRATION_ROLE, privilege),
        )
        missing_owner_privileges.extend(
            f"{row['relname']}:{privilege}" for row in cur.fetchall()
        )
    if missing_owner_privileges:
        problems.append(
            "lifecycle function owner lacks required privileges: "
            + ", ".join(missing_owner_privileges)
        )
    for role_name in (_STATUS_ROLE, _POLICY_CONTROLLER_ROLE):
        cur.execute(
            "SELECT has_schema_privilege(%s,'public','USAGE') AS usage,"
            "has_schema_privilege(%s,'public','CREATE') AS create",
            (role_name, role_name),
        )
        if cur.fetchone() != {"usage": True, "create": False}:
            problems.append(f"lifecycle role {role_name} schema ACL mismatch")
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind IN ('r','p') AND "
        "has_table_privilege(%s,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",
        (_STATUS_ROLE,),
    )
    status_mutations = [row["relname"] for row in cur.fetchall()]
    if status_mutations:
        problems.append("status role has mutation privileges: " + ", ".join(status_mutations))
    cur.execute(
        "SELECT c.relname,NULL::text AS column_name,acl.privilege_type FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) acl "
        "WHERE n.nspname='public' AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s) "
        "UNION ALL "
        "SELECT c.relname,a.attname,acl.privilege_type FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(a.attacl) acl WHERE n.nspname='public' "
        "AND a.attnum>0 AND NOT a.attisdropped "
        "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s)",
        (_STATUS_ROLE, _STATUS_ROLE),
    )
    actual_status_acls = {
        (row["relname"], row["column_name"], row["privilege_type"])
        for row in cur.fetchall()
    }
    expected_status_acls = {
        (relation, None, "SELECT") for relation in _STATUS_READ_RELATIONS
    } | {
        (relation, column, "SELECT")
        for relation, columns in _STATUS_READ_COLUMNS.items()
        for column in columns
    }
    if actual_status_acls != expected_status_acls:
        problems.append(
            "status role direct ACL contract mismatch: "
            f"missing={sorted(expected_status_acls - actual_status_acls)!r} "
            f"extra={sorted(actual_status_acls - expected_status_acls)!r}"
        )
    cur.execute(
        "SELECT c.relname,acl.privilege_type FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(c.relacl) acl "
        "WHERE n.nspname='public' AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s) "
        "UNION ALL "
        "SELECT c.relname||'.'||a.attname,acl.privilege_type FROM pg_attribute a "
        "JOIN pg_class c ON c.oid=a.attrelid JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(a.attacl) acl "
        "WHERE n.nspname='public' AND a.attnum>0 AND NOT a.attisdropped "
        "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s)",
        (_POLICY_CONTROLLER_ROLE, _POLICY_CONTROLLER_ROLE),
    )
    controller_table_acls = [
        f"{row['relname']}:{row['privilege_type']}" for row in cur.fetchall()
    ]
    if controller_table_acls:
        problems.append(
            "policy controller has forbidden direct table privileges: "
            + ", ".join(sorted(controller_table_acls))
        )

    verifier_identity = _verifier_identity()
    cur.execute(
        "SELECT has_schema_privilege(%s,'public','USAGE') AS public_usage,"
        "has_schema_privilege(%s,'public','CREATE') AS public_create,"
        "has_schema_privilege(%s,'brain_archive','USAGE') AS archive_usage,"
        "has_schema_privilege(%s,'brain_archive','CREATE') AS archive_create",
        (verifier_identity, verifier_identity, verifier_identity, verifier_identity),
    )
    verifier_schema_acl = cur.fetchone()
    if verifier_schema_acl != {
        "public_usage": True,
        "public_create": False,
        "archive_usage": True,
        "archive_create": False,
    }:
        problems.append(f"verifier role {verifier_identity} schema ACL contract mismatch")
    cur.execute(
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND left(c.relname,6)<>'brain_' AND c.relkind IN ('r','p','v','m','f') "
        "AND has_table_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",
        (verifier_identity,),
    )
    unrelated_verifier_relations = [row["relname"] for row in cur.fetchall()]
    if unrelated_verifier_relations:
        problems.append(
            "verifier role has unrelated public relation access: " + ", ".join(sorted(unrelated_verifier_relations))
        )
    cur.execute(
        "SELECT n.nspname, c.relname, COALESCE(grantee.rolname, 'PUBLIC') AS grantee, acl.privilege_type "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(c.relacl) acl "
        "LEFT JOIN pg_roles grantee ON grantee.oid = acl.grantee "
        "WHERE ((n.nspname = 'public' AND left(c.relname, 6) = 'brain_') "
        "OR n.nspname = 'brain_archive') AND acl.grantee <> c.relowner"
    )
    invalid_grants = []
    for row in cur.fetchall():
        allowed = row["grantee"] == verifier_identity and row["privilege_type"] == "SELECT"
        allowed = allowed or (
            row["nspname"] == "public"
            and row["relname"] in _STATUS_READ_RELATIONS
            and row["grantee"] == _STATUS_ROLE
            and row["privilege_type"] == "SELECT"
        )
        if not allowed:
            invalid_grants.append(f"{row['nspname']}.{row['relname']}->{row['grantee']}:{row['privilege_type']}")
    if invalid_grants:
        problems.append("invalid non-owner object ACLs: " + ", ".join(sorted(set(invalid_grants))))
    if verifier_identity is not None:
        cur.execute(
            "SELECT count(*) AS count FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE ((n.nspname='public' AND left(c.relname, 6)='brain_') OR n.nspname='brain_archive') "
            "AND c.relkind IN ('r','p') AND has_table_privilege(%s, c.oid, 'SELECT')",
            (verifier_identity,),
        )
        readable = cur.fetchone()["count"]
        cur.execute(
            "SELECT count(*) AS count FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE ((n.nspname='public' AND left(c.relname, 6)='brain_') OR n.nspname='brain_archive') "
            "AND c.relkind IN ('r','p')"
        )
        if readable != cur.fetchone()["count"]:
            problems.append(f"verifier role {verifier_identity} lacks complete read-only table access")
    cur.execute(
        "SELECT p.proname, COALESCE(grantee.rolname, 'PUBLIC') AS grantee "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        "CROSS JOIN LATERAL aclexplode(p.proacl) acl "
        "LEFT JOIN pg_roles grantee ON grantee.oid = acl.grantee "
        "WHERE n.nspname = 'public' AND left(p.proname, 6) = 'brain_' "
        "AND acl.grantee <> p.proowner"
    )
    function_grants = [
        f"{row['proname']}()->{row['grantee']}"
        for row in cur.fetchall()
        if not (
            row["proname"] in _CONTROLLER_FUNCTIONS
            and row["grantee"] == _POLICY_CONTROLLER_ROLE
        )
    ]
    if function_grants:
        problems.append("non-owner function ACLs: " + ", ".join(sorted(set(function_grants))))
    cur.execute(
        "SELECT COALESCE(grantee.rolname, 'PUBLIC') AS grantee "
        "FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) acl "
        "LEFT JOIN pg_roles grantee ON grantee.oid = acl.grantee "
        "WHERE n.nspname = 'brain_archive' AND acl.grantee <> n.nspowner"
    )
    schema_grants = [row["grantee"] for row in cur.fetchall()]
    expected_schema_grants = [] if verifier_identity is None else [verifier_identity]
    if sorted(schema_grants) != expected_schema_grants:
        problems.append("brain_archive schema ACL contract mismatch: " + ", ".join(sorted(schema_grants)))
    cur.execute(
        "SELECT COALESCE(grantee.rolname, 'PUBLIC') AS grantee, acl.privilege_type "
        "FROM pg_namespace n CROSS JOIN LATERAL aclexplode(n.nspacl) acl "
        "LEFT JOIN pg_roles grantee ON grantee.oid=acl.grantee "
        "WHERE n.nspname='public' AND acl.privilege_type='CREATE' "
        "AND COALESCE(grantee.rolname, 'PUBLIC') NOT IN (%s, 'pg_database_owner')",
        (migration_identity or "",),
    )
    create_grants = [row["grantee"] for row in cur.fetchall()]
    if create_grants:
        problems.append("non-controller public schema CREATE authority: " + ", ".join(sorted(create_grants)))
    if migration_identity is not None:
        cur.execute(
            "SELECT da.defaclobjtype, COALESCE(grantee.rolname, 'PUBLIC') AS grantee "
            "FROM pg_default_acl da JOIN pg_namespace n ON n.oid = da.defaclnamespace "
            "CROSS JOIN LATERAL aclexplode(da.defaclacl) acl "
            "LEFT JOIN pg_roles grantee ON grantee.oid = acl.grantee "
            "WHERE da.defaclrole = (SELECT oid FROM pg_roles WHERE rolname = %s) "
            "AND n.nspname = 'public' AND acl.grantee <> da.defaclrole",
            (migration_identity,),
        )
        default_grants = [f"{row['defaclobjtype']}->{row['grantee']}" for row in cur.fetchall()]
        if default_grants:
            problems.append("non-owner default ACLs: " + ", ".join(sorted(set(default_grants))))

    cur.execute("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fleet_worker') AS present")
    if cur.fetchone()["present"]:
        cur.execute(
            "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE ((n.nspname = 'public' AND left(c.relname, 6) = 'brain_') OR n.nspname = 'brain_archive') "
            "AND c.relkind IN ('r', 'p', 'S') AND CASE WHEN c.relkind = 'S' "
            "THEN has_sequence_privilege('fleet_worker', c.oid, 'USAGE,SELECT,UPDATE') "
            "ELSE has_table_privilege('fleet_worker', c.oid, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') END"
        )
        exposed = [f"{row['nspname']}.{row['relname']}" for row in cur.fetchall()]
        if exposed:
            problems.append("fleet_worker authority exposure: " + ", ".join(sorted(exposed)))
        cur.execute(
            "SELECT p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'public' AND left(p.proname, 6) = 'brain_' "
            "AND has_function_privilege('fleet_worker', p.oid, 'EXECUTE')"
        )
        exposed_functions = [row["proname"] for row in cur.fetchall()]
        cur.execute("SELECT has_schema_privilege('fleet_worker', 'brain_archive', 'USAGE,CREATE') AS exposed")
        if exposed_functions or cur.fetchone()["exposed"]:
            problems.append("fleet_worker function/schema exposure: " + ", ".join(sorted(exposed_functions)))

    if problems:
        raise RuntimeError("brain schema v1 verification failed: " + "; ".join(problems))


def verify_brain_schema_v1(conn) -> None:
    """Deeply verify schema v1 without changing caller transaction state."""
    _require_idle(conn)
    with conn.transaction():
        with conn.cursor() as cur:
            _verify_contract(cur)


def ensure_brain_schema_v1(
    conn,
    *,
    lock_timeout_seconds: float = _LOCK_TIMEOUT_SECONDS,
) -> None:
    """Install schema v1 once with a dedicated migration identity, or verify it."""
    _require_idle(conn)
    with conn.transaction():
        with conn.cursor() as cur:
            versions = _version_rows(cur)
            if len(versions) == 2:
                _verify_contract(cur)
                return

            migration_identity = _activate_migration_identity(cur)
            _assert_existing_ownership(cur, migration_identity)
            _acquire_xact_lock(cur, lock_timeout_seconds)

            # Another installer may have completed while this connection waited.
            versions = _version_rows(cur)
            if len(versions) == 2:
                _verify_contract(cur)
                return

            if not versions:
                cur.execute(_schema_bytes().decode("utf-8"))
                _apply_acl_contract(cur, migration_identity)
                cur.execute(
                    "INSERT INTO public.brain_schema_versions "
                    "(version, migration_name, migration_checksum, applied_by) "
                    "VALUES (1, %s, %s, %s)",
                    (_MIGRATION_NAME, _schema_checksum(), migration_identity),
                )
            cur.execute(_schema_v2_bytes().decode("utf-8"))
            _apply_acl_contract(cur, migration_identity)
            cur.execute(
                "INSERT INTO public.brain_schema_versions "
                "(version, migration_name, migration_checksum, applied_by) "
                "VALUES (2, %s, %s, %s)",
                (_MIGRATION_V2_NAME, _schema_v2_checksum(), migration_identity),
            )
            _verify_contract(cur)


def ensure_policy_partition(conn, policy_version: str) -> str:
    """Create and verify the owner-only LIST partition for one policy version."""
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise ValueError("policy_version must be a non-empty string")
    _require_idle(conn)
    suffix = hashlib.sha256(policy_version.encode("utf-8")).hexdigest()[:16]
    partition_name = f"brain_job_decisions_policy_{suffix}"
    with conn.transaction():
        with conn.cursor() as cur:
            version = _version_row(cur)
            if version is None:
                raise RuntimeError("brain schema v1 must be installed before creating a policy partition")
            current_user = _activate_migration_identity(cur)
            cur.execute(
                "SELECT pg_advisory_xact_lock(hashtext(%s), hashtext(%s))",
                ("applypilot:brain:policy-partition", policy_version),
            )
            cur.execute(
                "SELECT owner.rolname AS owner_name FROM pg_class c "
                "JOIN pg_roles owner ON owner.oid = c.relowner "
                "WHERE c.oid = 'public.brain_job_decisions'::regclass"
            )
            owner = cur.fetchone()["owner_name"]
            cur.execute(
                "SELECT %s = %s OR pg_has_role(%s, %s, 'MEMBER') AS allowed",
                (current_user, owner, current_user, owner),
            )
            if not cur.fetchone()["allowed"]:
                raise RuntimeError("policy partitions may only be created by the brain schema owner")
            cur.execute(
                "SELECT 1 FROM public.brain_decision_policies WHERE policy_version = %s",
                (policy_version,),
            )
            if cur.fetchone() is None:
                raise RuntimeError(f"unknown policy_version: {policy_version}")
            cur.execute(
                "SELECT c.oid::regclass::text AS relation FROM pg_inherits i "
                "JOIN pg_class c ON c.oid=i.inhrelid "
                "WHERE i.inhparent='public.brain_job_decisions'::regclass "
                "AND pg_get_expr(c.relpartbound, c.oid)='DEFAULT'"
            )
            default_partition = cur.fetchone()
            if default_partition is not None:
                cur.execute("SET LOCAL ROLE NONE")
                cur.execute(
                    sql.SQL("SELECT count(*) AS count FROM {} WHERE policy_version=%s").format(
                        sql.Identifier(*default_partition["relation"].split(".", 1))
                    ),
                    (policy_version,),
                )
                if cur.fetchone()["count"]:
                    raise RuntimeError(
                        f"default partition contains rows for policy_version {policy_version}; refusing to route silently"
                    )
                raise RuntimeError(
                    "default decision partition is forbidden; detach it before creating policy partitions"
                )
            cur.execute("SELECT to_regclass(%s) AS relation", (f"public.{partition_name}",))
            if cur.fetchone()["relation"] is None:
                cur.execute(
                    sql.SQL("CREATE TABLE {}.{} PARTITION OF {}.{} FOR VALUES IN ({})").format(
                        sql.Identifier("public"),
                        sql.Identifier(partition_name),
                        sql.Identifier("public"),
                        sql.Identifier("brain_job_decisions"),
                        sql.Literal(policy_version),
                    )
                )
            cur.execute(
                "SELECT t.tgname FROM pg_trigger t WHERE t.tgrelid=%s::regclass AND NOT t.tgisinternal",
                (f"public.{partition_name}",),
            )
            partition_triggers = {row["tgname"] for row in cur.fetchall()}
            for trigger_name, timing, granularity in (
                ("brain_job_decisions_register", "BEFORE INSERT", "FOR EACH ROW"),
                ("brain_job_decisions_append_only", "BEFORE UPDATE OR DELETE", "FOR EACH ROW"),
                ("brain_job_decisions_append_only_truncate", "BEFORE TRUNCATE", "FOR EACH STATEMENT"),
            ):
                if trigger_name not in partition_triggers:
                    function_name = (
                        "brain_register_decision" if trigger_name.endswith("register") else "brain_reject_mutation"
                    )
                    cur.execute(
                        sql.SQL("CREATE TRIGGER {} {} ON {}.{} {} EXECUTE FUNCTION public.{}()").format(
                            sql.Identifier(trigger_name),
                            sql.SQL(timing),
                            sql.Identifier("public"),
                            sql.Identifier(partition_name),
                            sql.SQL(granularity),
                            sql.Identifier(function_name),
                        )
                    )
            cur.execute(
                "SELECT DISTINCT COALESCE(role.rolname,'PUBLIC') AS grantee "
                "FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) acl "
                "LEFT JOIN pg_roles role ON role.oid=acl.grantee "
                "WHERE c.oid=%s::regclass AND acl.grantee<>c.relowner",
                (f"public.{partition_name}",),
            )
            for grant in cur.fetchall():
                grantee = sql.SQL("PUBLIC") if grant["grantee"] == "PUBLIC" else sql.Identifier(grant["grantee"])
                cur.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON TABLE {}.{} FROM {}").format(
                        sql.Identifier("public"), sql.Identifier(partition_name), grantee
                    )
                )
            cur.execute(
                sql.SQL("GRANT SELECT ON TABLE {}.{} TO {}").format(
                    sql.Identifier("public"), sql.Identifier(partition_name), sql.Identifier(_VERIFIER_ROLE)
                )
            )
            cur.execute(
                "SELECT pg_get_expr(c.relpartbound, c.oid) AS bound, owner.rolname AS owner_name "
                "FROM pg_class c JOIN pg_roles owner ON owner.oid = c.relowner "
                "WHERE c.oid = %s::regclass",
                (f"public.{partition_name}",),
            )
            row = cur.fetchone()
            expected_bound = f"FOR VALUES IN ('{policy_version.replace(chr(39), chr(39) * 2)}')"
            if row is None or row["bound"] != expected_bound or row["owner_name"] != owner:
                raise RuntimeError("existing policy partition has an incompatible bound or owner")
            _verify_contract(cur)
    return partition_name


ensure_schema_v1 = ensure_brain_schema_v1
verify_schema_v1 = verify_brain_schema_v1
