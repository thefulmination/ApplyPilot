"""Least-privilege PostgreSQL role for remote fleet workers.

This reconciles privileges only inside the connection's current database. Whether
the role can connect to any other database is a deployment/pg_hba concern and must
be controlled separately.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from psycopg import sql

DEFAULT_ROLE = "fleet_worker"
_ROLE_LOCK_KEY = "applypilot:fleet-worker-role:v2"


@dataclass(frozen=True)
class AclRegrant:
    """One parsed, non-PUBLIC ACL dependency to preserve during hardening."""

    object_kind: str
    qualified_name: str
    privileges: tuple[str, ...]
    grantee: str


@dataclass(frozen=True)
class RegrantManifest:
    """Operator-approved principals and structured grants for database hardening."""

    database_owner_role: str
    controller_roles: tuple[str, ...]
    verifier_roles: tuple[str, ...]
    retired_admin_roles: tuple[str, ...]
    infrastructure_superuser_roles: tuple[str, ...] = ()
    expected_service_roles: tuple[str, ...] = ()
    regrants: tuple[AclRegrant, ...] = ()


@dataclass(frozen=True)
class RoleReconciliationReceipt:
    role_name: str
    worker_id: str
    contract: str
    connect_allowlist: tuple[str, ...]
    effective_connect_grantees: tuple[Mapping[str, Any], ...]
    inventory: Mapping[str, Any]
    rollback_sql: str
    credential_forward_reconcile_required: bool
    reconciled_at: str


@dataclass(frozen=True)
class RuntimePrincipal:
    session_user: str
    current_user: str
    worker_id: str
    contract: str


@dataclass(frozen=True)
class BootstrapTopology:
    """One-time provider-admin handoff to permanent non-superuser roles."""

    database_owner_role: str
    controller_role: str
    verifier_role: str
    migrator_role: str
    retired_admin_roles: tuple[str, ...]
    expected_service_roles: tuple[str, ...] = ()
    infrastructure_superuser_roles: tuple[str, ...] = ()


@dataclass(frozen=True)
class BootstrapReceipt:
    database_name: str
    session_user: str
    topology: Mapping[str, Any]
    inventory: Mapping[str, Any]
    effective_connect_grantees: tuple[Mapping[str, Any], ...]
    rollback_sql: str
    escalation_required: bool
    bootstrapped_at: str


_COMMON_FUNCTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fleet_worker_identity", ()),
    ("fleet_worker_admission_snapshot", ()),
    ("fleet_worker_schema_contract", ()),
    ("fleet_worker_heartbeat", ("JSONB",)),
    ("fleet_worker_runtime_state", ("TEXT",)),
    ("fleet_worker_version_status", ("TEXT", "TEXT")),
    ("fleet_worker_ack_command", ("BIGINT", "TEXT")),
    ("fleet_worker_agent_blocks", ()),
)
_TRANSITION_FUNCTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fleet_worker_requeue", ("TEXT", "TEXT", "TEXT", "TEXT")),
    ("fleet_worker_park_infrastructure", ("TEXT", "TEXT", "TEXT")),
    ("fleet_worker_terminalize", ("TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "TEXT", "JSONB")),
    ("fleet_worker_park", ("TEXT", "TEXT", "TEXT", "TEXT", "INTEGER", "JSONB")),
    ("fleet_worker_mark_browser_interaction", ()),
    ("fleet_worker_attempt_create", ("TEXT", "TEXT", "JSONB")),
    ("fleet_worker_attempt_transition", ("UUID", "TEXT", "TEXT", "JSONB")),
)
_APPLY_FUNCTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("fleet_worker_lease_ats", ("TEXT", "TEXT", "INTEGER", "TEXT", "INTEGER")),
    *_TRANSITION_FUNCTIONS,
    ("fleet_worker_claim_liveness", ()),
    ("fleet_worker_write_liveness", ("TEXT", "TEXT", "TEXT")),
    ("fleet_worker_record_agent_wall", ("TEXT", "TIMESTAMPTZ")),
    ("fleet_worker_evaluate_agent_budget", ("JSONB", "INTEGER", "INTEGER")),
    ("fleet_worker_otp_request", ("TEXT", "TEXT", "TEXT", "INTEGER")),
    ("fleet_worker_otp_wait", ("BIGINT",)),
    ("fleet_worker_otp_consume", ("BIGINT",)),
)
_CONTRACT_FUNCTIONS = {
    "apply": _APPLY_FUNCTIONS,
    "linkedin": (
        ("fleet_worker_lease_linkedin", ("TEXT", "TEXT", "TEXT", "INTEGER", "TEXT")),
        *_TRANSITION_FUNCTIONS,
        ("fleet_worker_record_agent_wall", ("TEXT", "TIMESTAMPTZ")),
        ("fleet_worker_otp_request", ("TEXT", "TEXT", "TEXT", "INTEGER")),
        ("fleet_worker_otp_wait", ("BIGINT",)),
        ("fleet_worker_otp_consume", ("BIGINT",)),
    ),
    "compute": (
        ("fleet_worker_lease_compute", ()),
        ("fleet_worker_complete_compute", ("TEXT", "TEXT", "TEXT", "JSONB", "JSONB")),
    ),
    "discovery": (
        ("fleet_worker_lease_search", ()),
        ("fleet_worker_complete_search", ("TEXT", "JSONB", "TEXT")),
    ),
}
_CONTRACT_TYPES = {
    "apply": ("apply_queue", "_apply_queue"),
    "linkedin": ("linkedin_queue", "_linkedin_queue"),
    "compute": (),
    "discovery": (),
}
_REQUIRED_FUNCTION_COLUMNS: Mapping[str, tuple[str, ...]] = {
    "fleet_config": (
        "id",
        "paused",
        "ats_paused",
        "ats_apply_mode",
        "canary_enabled",
        "canary_remaining",
        "linkedin_apply_mode",
        "linkedin_canary_enabled",
        "linkedin_canary_remaining",
        "ats_policy_version",
        "linkedin_policy_version",
    ),
    "apply_queue": (
        "url",
        "lane",
        "status",
        "lease_owner",
        "attempts",
        "approved_batch",
        "decision_id",
        "policy_version",
        "decision_action",
        "qualification_verdict",
        "qualification_score",
        "qualification_floor",
        "decision_expires_at",
        "score",
        "final_score",
        "worker_lease_id",
    ),
    "linkedin_queue": (
        "url",
        "lane",
        "status",
        "lease_owner",
        "attempts",
        "approved_batch",
        "decision_id",
        "policy_version",
        "decision_action",
        "qualification_verdict",
        "qualification_score",
        "qualification_floor",
        "decision_expires_at",
        "score",
        "final_score",
        "worker_lease_id",
    ),
    "fleet_worker_lease_ledger": (
        "lease_id",
        "lane",
        "url",
        "worker_id",
        "queue_attempt",
        "policy_version",
        "home_ip",
        "target_host",
        "canary_charged",
        "canary_capacity_before",
        "canary_exhausted",
        "refunded_at",
        "state",
    ),
}


def _row_value(row, key: str, index: int = 0):
    return row[key] if isinstance(row, Mapping) else row[index]


def _identifiers(names: Iterable[str]) -> sql.Composed:
    return sql.SQL(", ").join(sql.Identifier(name) for name in names)


def _function_identifier(name: str, argument_types: tuple[str, ...]) -> sql.Composed:
    return sql.SQL("{}.{}({})").format(
        sql.Identifier("public"),
        sql.Identifier(name),
        sql.SQL(", ").join(sql.SQL(argument_type) for argument_type in argument_types),
    )


def _user_schemas(cur) -> tuple[str, ...]:
    cur.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname <> 'information_schema' AND nspname !~ '^pg_' ORDER BY nspname"
    )
    return tuple(_row_value(row, "nspname") for row in cur.fetchall())


def _transfer_application_ownership(
    cur, *, new_owner_role: str, apply: bool = True
) -> tuple[dict[str, Any], ...]:
    """Transfer only application objects, never cluster/system-owned objects."""
    new_owner = sql.Identifier(new_owner_role)
    transferred: list[dict[str, Any]] = []
    schemas = _user_schemas(cur)

    cur.execute(
        "SELECT n.nspname,c.relname,c.relkind,owner.rolname AS owner_name "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_roles owner ON owner.oid=c.relowner "
        "WHERE n.nspname=ANY(%s) AND c.relkind=ANY(%s) "
        "ORDER BY CASE c.relkind WHEN 'r' THEN 0 WHEN 'p' THEN 0 WHEN 'v' THEN 1 "
        "WHEN 'm' THEN 2 WHEN 'f' THEN 3 WHEN 'S' THEN 4 ELSE 5 END,n.nspname,c.relname",
        (list(schemas), ["r", "p", "v", "m", "f", "S"]),
    )
    relation_kinds = {
        "r": "TABLE",
        "p": "TABLE",
        "v": "VIEW",
        "m": "MATERIALIZED VIEW",
        "f": "FOREIGN TABLE",
        "S": "SEQUENCE",
    }
    for row in cur.fetchall():
        owner_name = _row_value(row, "owner_name", 3)
        if owner_name == new_owner_role:
            continue
        object_kind = relation_kinds[_row_value(row, "relkind", 2)]
        namespace = _row_value(row, "nspname")
        object_name = _row_value(row, "relname", 1)
        if apply:
            cur.execute(
                sql.SQL("ALTER {} {} OWNER TO {}").format(
                    sql.SQL(object_kind), sql.Identifier(namespace, object_name), new_owner
                )
            )
        transferred.append(
            {
                "object_kind": object_kind.lower(),
                "qualified_name": f"{namespace}.{object_name}",
                "owner_before": owner_name,
                "schema_name": namespace,
                "object_name": object_name,
            }
        )

    cur.execute(
        "SELECT n.nspname,p.proname,p.prokind,pg_get_function_identity_arguments(p.oid) AS args,"
        "owner.rolname AS owner_name FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "JOIN pg_roles owner ON owner.oid=p.proowner WHERE n.nspname=ANY(%s) "
        "ORDER BY n.nspname,p.proname,args",
        (list(schemas),),
    )
    for row in cur.fetchall():
        owner_name = _row_value(row, "owner_name", 4)
        if owner_name == new_owner_role:
            continue
        prokind = _row_value(row, "prokind", 2)
        if prokind == "a":
            raise RuntimeError("bootstrap cannot transfer user-defined aggregate ownership safely")
        routine_kind = "PROCEDURE" if prokind == "p" else "FUNCTION"
        namespace = _row_value(row, "nspname")
        routine_name = _row_value(row, "proname", 1)
        arguments = _row_value(row, "args", 3)
        identity = sql.SQL("{}.{}({})").format(
            sql.Identifier(namespace), sql.Identifier(routine_name), sql.SQL(arguments)
        )
        if apply:
            cur.execute(sql.SQL("ALTER {} {} OWNER TO {}").format(sql.SQL(routine_kind), identity, new_owner))
        transferred.append(
            {
                "object_kind": routine_kind.lower(),
                "qualified_name": f"{namespace}.{routine_name}({arguments})",
                "owner_before": owner_name,
                "schema_name": namespace,
                "object_name": routine_name,
                "arguments": arguments,
            }
        )

    cur.execute(
        "SELECT n.nspname,t.typname,t.typtype,owner.rolname AS owner_name "
        "FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
        "JOIN pg_roles owner ON owner.oid=t.typowner "
        "WHERE n.nspname=ANY(%s) AND t.typrelid=0 AND t.typelem=0 "
        "AND t.typtype IN ('c','d','e','r','m') ORDER BY n.nspname,t.typname",
        (list(schemas),),
    )
    for row in cur.fetchall():
        owner_name = _row_value(row, "owner_name", 3)
        if owner_name == new_owner_role:
            continue
        object_kind = "DOMAIN" if _row_value(row, "typtype", 2) == "d" else "TYPE"
        namespace = _row_value(row, "nspname")
        type_name = _row_value(row, "typname", 1)
        if apply:
            cur.execute(
                sql.SQL("ALTER {} {} OWNER TO {}").format(
                    sql.SQL(object_kind), sql.Identifier(namespace, type_name), new_owner
                )
            )
        transferred.append(
            {
                "object_kind": object_kind.lower(),
                "qualified_name": f"{namespace}.{type_name}",
                "owner_before": owner_name,
                "schema_name": namespace,
                "object_name": type_name,
            }
        )

    cur.execute(
        "SELECT n.nspname,o.oprname FROM pg_operator o JOIN pg_namespace n ON n.oid=o.oprnamespace "
        "WHERE n.nspname=ANY(%s) ORDER BY n.nspname,o.oprname",
        (list(schemas),),
    )
    unsupported_operators = [
        f"{_row_value(row, 'nspname')}.{_row_value(row, 'oprname', 1)}" for row in cur.fetchall()
    ]
    if unsupported_operators:
        raise RuntimeError("bootstrap cannot transfer user-defined operators: " + ", ".join(unsupported_operators))

    cur.execute(
        "SELECT n.nspname,owner.rolname AS owner_name FROM pg_namespace n "
        "JOIN pg_roles owner ON owner.oid=n.nspowner WHERE n.nspname=ANY(%s) ORDER BY n.nspname",
        (list(schemas),),
    )
    for row in cur.fetchall():
        owner_name = _row_value(row, "owner_name", 1)
        if owner_name == new_owner_role:
            continue
        schema_name = _row_value(row, "nspname")
        if apply:
            cur.execute(sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(sql.Identifier(schema_name), new_owner))
        transferred.append(
            {
                "object_kind": "schema",
                "qualified_name": schema_name,
                "owner_before": owner_name,
                "schema_name": schema_name,
            }
        )
    return tuple(transferred)


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$.-]{0,62}$")
_REGRANT_PRIVILEGES = {
    "schema": {"USAGE", "CREATE"},
    "table": {"SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"},
    "sequence": {"USAGE", "SELECT", "UPDATE"},
    "function": {"EXECUTE"},
    "type": {"USAGE"},
    "default_functions": {"EXECUTE"},
    "default_types": {"USAGE"},
}


def _reject_unsafe_text(value: str, *, field: str) -> None:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field} contains empty or control-character data")
    if any(marker in value for marker in (";", "--", "/*", "*/", "#", "\\")):
        raise ValueError(f"{field} contains SQL/comment syntax")


def _qualified_identifiers(value: str, *, count: tuple[int, ...]) -> tuple[str, ...]:
    _reject_unsafe_text(value, field="qualified_name")
    parts = tuple(value.split("."))
    if len(parts) not in count or any(not _IDENTIFIER.fullmatch(part) for part in parts):
        raise ValueError(f"invalid structured qualified_name: {value!r}")
    return parts


def _validate_regrants(cur, manifest: RegrantManifest) -> tuple[dict[str, Any], ...]:
    allowed_grantees = set(
        (*manifest.controller_roles, *manifest.verifier_roles, *manifest.expected_service_roles)
    ) - set(manifest.retired_admin_roles)
    normalized: list[dict[str, Any]] = []
    for grant in manifest.regrants:
        kind = grant.object_kind.lower()
        if kind not in _REGRANT_PRIVILEGES:
            raise ValueError(f"unsupported regrant object_kind: {grant.object_kind!r}")
        _reject_unsafe_text(grant.grantee, field="grantee")
        if grant.grantee.upper() == "PUBLIC":
            raise ValueError("structured regrants to PUBLIC are forbidden")
        if grant.grantee not in allowed_grantees or not _IDENTIFIER.fullmatch(grant.grantee):
            raise ValueError(f"regrant grantee is not an approved service role: {grant.grantee!r}")
        privileges = tuple(dict.fromkeys(privilege.upper() for privilege in grant.privileges))
        if not privileges or not set(privileges) <= _REGRANT_PRIVILEGES[kind]:
            raise ValueError(f"invalid privileges for {kind}: {grant.privileges!r}")
        if kind == "schema":
            _qualified_identifiers(grant.qualified_name, count=(1,))
        elif kind in {"table", "sequence", "type"}:
            _qualified_identifiers(grant.qualified_name, count=(2,))
        elif kind == "function":
            _reject_unsafe_text(grant.qualified_name, field="qualified_name")
            if not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_$.-]*\.[A-Za-z_][A-Za-z0-9_$.-]*\([A-Za-z0-9_ ,.\[\]]*\)", grant.qualified_name
            ):
                raise ValueError(f"invalid function qualified_name: {grant.qualified_name!r}")
            cur.execute("SELECT to_regprocedure(%s) AS oid", (grant.qualified_name,))
            if _row_value(cur.fetchone(), "oid") is None:
                raise RuntimeError(f"regrant function does not exist: {grant.qualified_name}")
        else:
            _qualified_identifiers(grant.qualified_name, count=(1, 2))
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (grant.grantee,))
        if cur.fetchone() is None:
            raise RuntimeError(f"regrant grantee role does not exist: {grant.grantee}")
        normalized.append(
            {
                "object_kind": kind,
                "qualified_name": grant.qualified_name,
                "privileges": privileges,
                "grantee": grant.grantee,
            }
        )
    return tuple(normalized)


def _apply_regrants(cur, regrants: tuple[dict[str, Any], ...]) -> None:
    for grant in regrants:
        kind = grant["object_kind"]
        privileges = sql.SQL(", ").join(sql.SQL(value) for value in grant["privileges"])
        grantee = sql.Identifier(grant["grantee"])
        if kind == "schema":
            target = sql.Identifier(*grant["qualified_name"].split("."))
            cur.execute(sql.SQL("GRANT {} ON SCHEMA {} TO {}").format(privileges, target, grantee))
        elif kind in {"table", "sequence", "type"}:
            target = sql.Identifier(*grant["qualified_name"].split("."))
            cur.execute(sql.SQL("GRANT {} ON {} {} TO {}").format(privileges, sql.SQL(kind.upper()), target, grantee))
        elif kind == "function":
            cur.execute(
                "SELECT to_regprocedure(%s)::regprocedure::text AS identity",
                (grant["qualified_name"],),
            )
            identity = _row_value(cur.fetchone(), "identity")
            cur.execute(sql.SQL("GRANT {} ON FUNCTION {} TO {}").format(privileges, sql.SQL(identity), grantee))
        else:
            owner, *schema_name = grant["qualified_name"].split(".")
            object_type = "FUNCTIONS" if kind == "default_functions" else "TYPES"
            statement = sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} ").format(sql.Identifier(owner))
            if schema_name:
                statement += sql.SQL("IN SCHEMA {} ").format(sql.Identifier(schema_name[0]))
            statement += sql.SQL("GRANT {} ON {} TO {}").format(privileges, sql.SQL(object_type), grantee)
            cur.execute(statement)


def _database_inventory(cur, *, role_name: str, manifest: RegrantManifest) -> dict[str, Any]:
    controller_roles = tuple(dict.fromkeys(manifest.controller_roles))
    verifier_roles = tuple(dict.fromkeys(manifest.verifier_roles))
    retired_admin_roles = tuple(dict.fromkeys(manifest.retired_admin_roles))
    break_glass_roles = tuple(dict.fromkeys(manifest.infrastructure_superuser_roles))
    if not controller_roles or not verifier_roles:
        raise RuntimeError("explicit regrant manifest requires controller and verifier roles")
    if not break_glass_roles:
        raise RuntimeError("explicit regrant manifest requires at least one break-glass superuser role")
    if not _IDENTIFIER.fullmatch(manifest.database_owner_role):
        raise ValueError("database_owner_role must be one plain role identifier")
    if set(retired_admin_roles) & set(
        (manifest.database_owner_role, *controller_roles, *verifier_roles, *break_glass_roles)
    ):
        raise RuntimeError("retired admin roles must be disjoint from owner/controller/verifier/break-glass roles")
    if set(break_glass_roles) & set((manifest.database_owner_role, *controller_roles, *verifier_roles)):
        raise RuntimeError("break-glass roles must be disjoint from owner/controller/verifier roles")
    cur.execute(
        "SELECT session_user AS session_name,current_database() AS database_name,owner.rolname AS owner_name "
        "FROM pg_database d JOIN pg_roles owner ON owner.oid=d.datdba "
        "WHERE d.datname=current_database()"
    )
    database_row = cur.fetchone()
    migration_session = _row_value(database_row, "session_name")
    prior_owner = _row_value(database_row, "owner_name", 2)
    if migration_session not in controller_roles:
        raise RuntimeError("role reconciliation session_user must be an explicit controller role")
    if prior_owner != manifest.database_owner_role and prior_owner not in retired_admin_roles:
        raise RuntimeError(f"current database owner {prior_owner!r} must be the dedicated owner or explicitly retired")

    cur.execute(
        "SELECT role_name FROM public.fleet_worker_principals WHERE role_name<>%s ORDER BY role_name",
        (role_name,),
    )
    mapped_roles = tuple(_row_value(row, "role_name") for row in cur.fetchall())
    connect_allowlist = tuple(
        dict.fromkeys(
            (
                manifest.database_owner_role,
                *controller_roles,
                *verifier_roles,
                *break_glass_roles,
                *mapped_roles,
                role_name,
            )
        )
    )
    if any(name in {"PUBLIC", "fleet_worker"} for name in connect_allowlist):
        raise RuntimeError("CONNECT allowlist forbids PUBLIC and the shared fleet_worker role")

    cur.execute(
        "SELECT rolname FROM pg_catalog.pg_roles WHERE rolname=ANY(%s) ORDER BY rolname",
        (list(connect_allowlist),),
    )
    existing = {_row_value(row, "rolname") for row in cur.fetchall()}
    # The target worker role is allowed to be created by this transaction.
    missing = sorted(set(connect_allowlist) - existing - {role_name})
    if missing:
        raise RuntimeError("regrant manifest references missing roles: " + ", ".join(missing))
    cur.execute(
        "SELECT rolname,rolcanlogin,rolsuper FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname",
        (list(connect_allowlist),),
    )
    allowlist_attributes = {
        _row_value(row, "rolname"): (
            _row_value(row, "rolcanlogin", 1),
            _row_value(row, "rolsuper", 2),
        )
        for row in cur.fetchall()
    }
    owner_attributes = allowlist_attributes.get(manifest.database_owner_role)
    if owner_attributes != (False, False):
        raise RuntimeError("dedicated database owner role must be NOLOGIN and NOSUPERUSER")
    if not any(allowlist_attributes.get(name, (False, False))[0] for name in controller_roles):
        raise RuntimeError("at least one explicit controller role must be LOGIN")
    invalid_controllers = sorted(
        name for name in controller_roles if allowlist_attributes.get(name, (False, False))[1]
    )
    if invalid_controllers:
        raise RuntimeError("controller roles must be NOSUPERUSER: " + ", ".join(invalid_controllers))
    invalid_break_glass = sorted(
        name for name in break_glass_roles if allowlist_attributes.get(name) != (True, True)
    )
    if invalid_break_glass:
        raise RuntimeError("break-glass roles must remain LOGIN SUPERUSER: " + ", ".join(invalid_break_glass))
    cur.execute(
        "SELECT rolname FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname",
        (list(retired_admin_roles),),
    )
    existing_retired = {_row_value(row, "rolname") for row in cur.fetchall()}
    missing_retired = sorted(set(retired_admin_roles) - existing_retired)
    if missing_retired:
        raise RuntimeError("retired admin roles do not exist: " + ", ".join(missing_retired))
    structured_regrants = _validate_regrants(cur, manifest)

    cur.execute(
        "SELECT DISTINCT usename FROM pg_catalog.pg_stat_activity "
        "WHERE datname=current_database() AND pid<>pg_backend_pid() AND usename IS NOT NULL "
        "ORDER BY usename"
    )
    active_services = tuple(_row_value(row, "usename") for row in cur.fetchall())
    expected_services = set(manifest.expected_service_roles)
    unknown_services = sorted(
        set(active_services) - expected_services - set(connect_allowlist) - set(retired_admin_roles)
    )
    if unknown_services:
        raise RuntimeError("unknown database service principals are active: " + ", ".join(unknown_services))
    missing_services = sorted(expected_services - set(active_services))
    if missing_services:
        raise RuntimeError("expected database service principals are not active: " + ", ".join(missing_services))

    cur.execute(
        "SELECT DISTINCT grantee.rolname FROM pg_catalog.pg_database d "
        "CROSS JOIN LATERAL pg_catalog.aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) acl "
        "JOIN pg_catalog.pg_roles grantee ON grantee.oid=acl.grantee "
        "WHERE d.datname=current_database() AND acl.privilege_type='CONNECT' "
        "AND acl.grantee<>d.datdba "
        "ORDER BY grantee.rolname"
    )
    explicit_connect_roles = tuple(_row_value(row, "rolname") for row in cur.fetchall())
    unknown_connect = sorted(set(explicit_connect_roles) - set(connect_allowlist) - set(retired_admin_roles))
    if unknown_connect:
        raise RuntimeError(
            "unknown explicit CONNECT dependencies require manifest resolution: " + ", ".join(unknown_connect)
        )

    cur.execute(
        "SELECT r.rolname AS owner, COALESCE(n.nspname,'*') AS schema_name, "
        "d.defaclobjtype,CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE grantee.rolname END AS grantee,"
        "acl.privilege_type,acl.is_grantable "
        "FROM pg_catalog.pg_default_acl d JOIN pg_catalog.pg_roles r ON r.oid=d.defaclrole "
        "LEFT JOIN pg_catalog.pg_namespace n ON n.oid=d.defaclnamespace "
        "CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) acl "
        "LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid=acl.grantee "
        "ORDER BY r.rolname,schema_name,d.defaclobjtype,grantee,acl.privilege_type"
    )
    default_acls = tuple(
        {
            "owner": _row_value(row, "owner"),
            "schema": _row_value(row, "schema_name", 1),
            "object_type": _row_value(row, "defaclobjtype", 2),
            "grantee": _row_value(row, "grantee", 3),
            "privilege": _row_value(row, "privilege_type", 4),
            "grantable": _row_value(row, "is_grantable", 5),
        }
        for row in cur.fetchall()
    )
    return {
        "connect_allowlist": connect_allowlist,
        "database_name": _row_value(database_row, "database_name", 1),
        "database_owner_before": prior_owner,
        "database_owner_after": manifest.database_owner_role,
        "retired_admin_roles": retired_admin_roles,
        "infrastructure_superuser_roles": break_glass_roles,
        "active_service_roles": active_services,
        "explicit_connect_roles": explicit_connect_roles,
        "default_acls": default_acls,
        "regrants": structured_regrants,
    }


def _target_role_snapshot(cur, *, role_name: str, role_exists: bool) -> dict[str, Any]:
    """Capture only secret-free state needed to reverse mapped-role reconciliation."""
    attributes = None
    memberships: tuple[dict[str, Any], ...] = ()
    granted_memberships: tuple[dict[str, Any], ...] = ()
    principal = None
    if role_exists:
        cur.execute(
            "SELECT rolcanlogin,rolinherit,rolsuper,rolcreatedb,rolcreaterole,rolreplication,"
            "rolbypassrls,rolconnlimit,rolvaliduntil::text AS rolvaliduntil "
            "FROM pg_catalog.pg_roles WHERE rolname=%s",
            (role_name,),
        )
        row = cur.fetchone()
        attributes = {
            name: _row_value(row, name, index)
            for index, name in enumerate(
                (
                    "rolcanlogin",
                    "rolinherit",
                    "rolsuper",
                    "rolcreatedb",
                    "rolcreaterole",
                    "rolreplication",
                    "rolbypassrls",
                    "rolconnlimit",
                    "rolvaliduntil",
                )
            )
        }
        cur.execute(
            "SELECT parent.rolname AS parent_role,grantor.rolname AS grantor_role,"
            "m.admin_option,m.inherit_option,m.set_option "
            "FROM pg_catalog.pg_auth_members m "
            "JOIN pg_catalog.pg_roles parent ON parent.oid=m.roleid "
            "JOIN pg_catalog.pg_roles member ON member.oid=m.member "
            "JOIN pg_catalog.pg_roles grantor ON grantor.oid=m.grantor "
            "WHERE member.rolname=%s ORDER BY parent.rolname,grantor.rolname",
            (role_name,),
        )
        memberships = tuple(
            {
                "parent_role": _row_value(row, "parent_role"),
                "grantor_role": _row_value(row, "grantor_role", 1),
                "admin_option": _row_value(row, "admin_option", 2),
                "inherit_option": _row_value(row, "inherit_option", 3),
                "set_option": _row_value(row, "set_option", 4),
            }
            for row in cur.fetchall()
        )
        cur.execute(
            "SELECT member.rolname AS member_role,grantor.rolname AS grantor_role,"
            "m.admin_option,m.inherit_option,m.set_option "
            "FROM pg_catalog.pg_auth_members m "
            "JOIN pg_catalog.pg_roles parent ON parent.oid=m.roleid "
            "JOIN pg_catalog.pg_roles member ON member.oid=m.member "
            "JOIN pg_catalog.pg_roles grantor ON grantor.oid=m.grantor "
            "WHERE parent.rolname=%s ORDER BY member.rolname,grantor.rolname",
            (role_name,),
        )
        granted_memberships = tuple(
            {
                "member_role": _row_value(row, "member_role"),
                "grantor_role": _row_value(row, "grantor_role", 1),
                "admin_option": _row_value(row, "admin_option", 2),
                "inherit_option": _row_value(row, "inherit_option", 3),
                "set_option": _row_value(row, "set_option", 4),
            }
            for row in cur.fetchall()
        )

    cur.execute(
        "SELECT worker_id,contract,created_at::text AS created_at "
        "FROM public.fleet_worker_principals WHERE role_name=%s",
        (role_name,),
    )
    principal_row = cur.fetchone()
    if principal_row is not None:
        principal = {
            "worker_id": _row_value(principal_row, "worker_id"),
            "contract": _row_value(principal_row, "contract", 1),
            "created_at": _row_value(principal_row, "created_at", 2),
        }

    direct_acls = _direct_acl_snapshot(cur, grantee=role_name) if role_exists else ()
    default_acls, default_scopes = _default_acl_snapshot(cur, grantee=role_name)
    return {
        "existed": role_exists,
        "attributes": attributes,
        "memberships": memberships,
        "granted_memberships": granted_memberships,
        "principal_mapping": principal,
        "direct_acls": direct_acls,
        "default_acls": default_acls,
        "default_acl_scopes": default_scopes,
    }


def _direct_acl_snapshot(cur, *, grantee: str) -> tuple[dict[str, Any], ...]:
    queries = (
        (
            "database",
            "SELECT d.datname AS schema_name,NULL::text AS object_name,NULL::text AS arguments,"
            "acl.privilege_type,acl.is_grantable FROM pg_catalog.pg_database d "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) acl "
            "WHERE d.datname=current_database() AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s)",
        ),
        (
            "schema",
            "SELECT n.nspname AS schema_name,NULL::text AS object_name,NULL::text AS arguments,"
            "acl.privilege_type,acl.is_grantable FROM pg_catalog.pg_namespace n "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(n.nspacl) acl "
            "WHERE n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
            "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s)",
        ),
        (
            "relation",
            "SELECT n.nspname AS schema_name,c.relname AS object_name,c.relkind::text AS arguments,"
            "acl.privilege_type,acl.is_grantable FROM pg_catalog.pg_class c "
            "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) acl "
            "WHERE c.relkind=ANY(ARRAY['r','p','v','m','f','S']::char[]) "
            "AND n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
            "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s)",
        ),
        (
            "routine",
            "SELECT n.nspname AS schema_name,p.proname AS object_name,"
            "pg_catalog.pg_get_function_identity_arguments(p.oid) AS arguments,"
            "acl.privilege_type,acl.is_grantable,p.prokind::text AS prokind "
            "FROM pg_catalog.pg_proc p JOIN pg_catalog.pg_namespace n ON n.oid=p.pronamespace "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(p.proacl) acl "
            "WHERE n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
            "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s)",
        ),
        (
            "type",
            "SELECT n.nspname AS schema_name,t.typname AS object_name,NULL::text AS arguments,"
            "acl.privilege_type,acl.is_grantable FROM pg_catalog.pg_type t "
            "JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace "
            "CROSS JOIN LATERAL pg_catalog.aclexplode(t.typacl) acl "
            "WHERE t.typtype IN ('c','d','e','r','m') "
            "AND n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
            "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s)",
        ),
    )
    snapshot: list[dict[str, Any]] = []
    for catalog_kind, query in queries:
        cur.execute(query, (grantee,))
        for row in cur.fetchall():
            object_kind = catalog_kind
            relation_kind = _row_value(row, "arguments", 2)
            if catalog_kind == "relation":
                object_kind = "sequence" if relation_kind == "S" else "table"
            elif catalog_kind == "routine":
                object_kind = "procedure" if _row_value(row, "prokind", 5) == "p" else "function"
            snapshot.append(
                {
                    "object_kind": object_kind,
                    "schema_name": _row_value(row, "schema_name"),
                    "object_name": _row_value(row, "object_name", 1),
                    "arguments": None if catalog_kind == "relation" else _row_value(row, "arguments", 2),
                    "privilege": _row_value(row, "privilege_type", 3),
                    "grantable": _row_value(row, "is_grantable", 4),
                }
            )
    return tuple(
        sorted(
            snapshot,
            key=lambda item: (
                item["object_kind"],
                item["schema_name"] or "",
                item["object_name"] or "",
                item["arguments"] or "",
                item["privilege"],
            ),
        )
    )


def _default_acl_snapshot(
    cur, *, grantee: str
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...]]:
    cur.execute(
        "SELECT owner.rolname AS owner_name,n.nspname AS schema_name,d.defaclobjtype,"
        "acl.privilege_type,acl.is_grantable FROM pg_catalog.pg_default_acl d "
        "JOIN pg_catalog.pg_roles owner ON owner.oid=d.defaclrole "
        "LEFT JOIN pg_catalog.pg_namespace n ON n.oid=d.defaclnamespace "
        "CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) acl "
        "WHERE acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s) "
        "ORDER BY owner.rolname,n.nspname,d.defaclobjtype,acl.privilege_type",
        (grantee,),
    )
    entries = tuple(
        {
            "owner": _row_value(row, "owner_name"),
            "schema": _row_value(row, "schema_name", 1),
            "object_type": _row_value(row, "defaclobjtype", 2),
            "privilege": _row_value(row, "privilege_type", 3),
            "grantable": _row_value(row, "is_grantable", 4),
        }
        for row in cur.fetchall()
    )
    cur.execute(
        "SELECT DISTINCT owner_name,schema_name,object_type FROM ("
        "SELECT owner.rolname AS owner_name,NULL::text AS schema_name,kind.object_type "
        "FROM pg_catalog.pg_roles owner CROSS JOIN unnest(ARRAY['r','S','f','T']) kind(object_type) "
        "WHERE owner.rolname=current_user OR EXISTS(SELECT 1 FROM pg_default_acl d WHERE d.defaclrole=owner.oid) "
        "UNION ALL SELECT owner.rolname,n.nspname,kind.object_type FROM pg_catalog.pg_roles owner "
        "CROSS JOIN pg_catalog.pg_namespace n CROSS JOIN unnest(ARRAY['r','S','f','T']) kind(object_type) "
        "WHERE n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
        "AND (owner.rolname=current_user OR EXISTS(SELECT 1 FROM pg_default_acl d WHERE d.defaclrole=owner.oid))"
        ") scopes ORDER BY owner_name,schema_name,object_type"
    )
    scopes = tuple(
        {
            "owner": _row_value(row, "owner_name"),
            "schema": _row_value(row, "schema_name", 1),
            "object_type": _row_value(row, "object_type", 2),
        }
        for row in cur.fetchall()
    )
    return entries, scopes


def _structured_regrant_snapshot(
    cur, regrants: tuple[dict[str, Any], ...]
) -> tuple[dict[str, Any], ...]:
    snapshots: list[dict[str, Any]] = []
    for grant in regrants:
        kind = grant["object_kind"]
        qualified_name = grant["qualified_name"]
        grantee = grant["grantee"]
        target: dict[str, Any] = {
            "object_kind": kind,
            "qualified_name": qualified_name,
            "grantee": grantee,
        }
        if kind.startswith("default_"):
            owner, *schema_name = qualified_name.split(".")
            object_code = "f" if kind == "default_functions" else "T"
            cur.execute(
                "SELECT acl.privilege_type,acl.is_grantable FROM pg_catalog.pg_default_acl d "
                "LEFT JOIN pg_catalog.pg_namespace n ON n.oid=d.defaclnamespace "
                "CROSS JOIN LATERAL pg_catalog.aclexplode(d.defaclacl) acl "
                "WHERE d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=%s) "
                "AND d.defaclobjtype=%s AND n.nspname IS NOT DISTINCT FROM %s "
                "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s) "
                "ORDER BY acl.privilege_type",
                (owner, object_code, schema_name[0] if schema_name else None, grantee),
            )
            target.update(owner=owner, schema=schema_name[0] if schema_name else None, object_code=object_code)
        elif kind == "schema":
            cur.execute(
                "SELECT acl.privilege_type,acl.is_grantable FROM pg_namespace n "
                "CROSS JOIN LATERAL aclexplode(n.nspacl) acl WHERE n.nspname=%s "
                "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s) ORDER BY acl.privilege_type",
                (qualified_name, grantee),
            )
            target.update(schema_name=qualified_name, object_name=None, arguments=None)
        elif kind in {"table", "sequence"}:
            schema_name, object_name = qualified_name.split(".")
            cur.execute(
                "SELECT acl.privilege_type,acl.is_grantable FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace CROSS JOIN LATERAL aclexplode(c.relacl) acl "
                "WHERE n.nspname=%s AND c.relname=%s "
                "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s) ORDER BY acl.privilege_type",
                (schema_name, object_name, grantee),
            )
            target.update(schema_name=schema_name, object_name=object_name, arguments=None)
        elif kind == "function":
            cur.execute(
                "SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) AS arguments,"
                "acl.privilege_type,acl.is_grantable FROM pg_proc p "
                "JOIN pg_namespace n ON n.oid=p.pronamespace CROSS JOIN LATERAL aclexplode(p.proacl) acl "
                "WHERE p.oid=to_regprocedure(%s) "
                "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s) ORDER BY acl.privilege_type",
                (qualified_name, grantee),
            )
            rows = cur.fetchall()
            if rows:
                target.update(
                    schema_name=_row_value(rows[0], "nspname"),
                    object_name=_row_value(rows[0], "proname", 1),
                    arguments=_row_value(rows[0], "arguments", 2),
                )
            else:
                cur.execute(
                    "SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) AS arguments "
                    "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE p.oid=to_regprocedure(%s)",
                    (qualified_name,),
                )
                identity = cur.fetchone()
                target.update(
                    schema_name=_row_value(identity, "nspname"),
                    object_name=_row_value(identity, "proname", 1),
                    arguments=_row_value(identity, "arguments", 2),
                )
                rows = ()
            target["prior_acl"] = tuple(
                {
                    "privilege": _row_value(row, "privilege_type", 3),
                    "grantable": _row_value(row, "is_grantable", 4),
                }
                for row in rows
            )
            snapshots.append(target)
            continue
        else:
            schema_name, object_name = qualified_name.split(".")
            cur.execute(
                "SELECT acl.privilege_type,acl.is_grantable FROM pg_type t "
                "JOIN pg_namespace n ON n.oid=t.typnamespace CROSS JOIN LATERAL aclexplode(t.typacl) acl "
                "WHERE n.nspname=%s AND t.typname=%s "
                "AND acl.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s) ORDER BY acl.privilege_type",
                (schema_name, object_name, grantee),
            )
            target.update(schema_name=schema_name, object_name=object_name, arguments=None)
        target["prior_acl"] = tuple(
            {
                "privilege": _row_value(row, "privilege_type"),
                "grantable": _row_value(row, "is_grantable", 1),
            }
            for row in cur.fetchall()
        )
        snapshots.append(target)
    return tuple(snapshots)


def _acl_target_sql(cur, target: Mapping[str, Any]) -> str:
    kind = target["object_kind"]
    if kind == "database":
        return sql.Identifier(target["schema_name"]).as_string(cur.connection)
    if kind == "schema":
        return sql.Identifier(target["schema_name"]).as_string(cur.connection)
    if kind in {"table", "sequence", "type"}:
        return sql.Identifier(target["schema_name"], target["object_name"]).as_string(cur.connection)
    if kind in {"function", "procedure"}:
        return sql.SQL("{}.{}({})").format(
            sql.Identifier(target["schema_name"]),
            sql.Identifier(target["object_name"]),
            sql.SQL(target["arguments"]),
        ).as_string(cur.connection)
    raise RuntimeError(f"unsupported rollback ACL object kind: {kind}")


def _grant_acl_sql(cur, *, target: Mapping[str, Any], grantee: str, acl: Iterable[Mapping[str, Any]]) -> list[str]:
    statements: list[str] = []
    object_kind = target["object_kind"].upper()
    identity = _acl_target_sql(cur, target)
    quoted_grantee = sql.Identifier(grantee).as_string(cur.connection)
    for grantable in (False, True):
        privileges = sorted(item["privilege"] for item in acl if bool(item["grantable"]) is grantable)
        if privileges:
            suffix = " WITH GRANT OPTION" if grantable else ""
            statements.append(
                f"GRANT {', '.join(privileges)} ON {object_kind} {identity} TO {quoted_grantee}{suffix};"
            )
    return statements


def _default_acl_prefix(cur, scope: Mapping[str, Any]) -> tuple[str, str]:
    owner = sql.Identifier(scope["owner"]).as_string(cur.connection)
    schema = scope.get("schema")
    prefix = f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner}"
    if schema:
        prefix += f" IN SCHEMA {sql.Identifier(schema).as_string(cur.connection)}"
    object_name = {"r": "TABLES", "S": "SEQUENCES", "f": "FUNCTIONS", "T": "TYPES"}[
        scope["object_type"]
    ]
    return prefix, object_name


def _all_direct_acl_targets(cur) -> tuple[dict[str, Any], ...]:
    cur.execute(
        "SELECT 'database' AS object_kind,current_database() AS schema_name,NULL::text AS object_name,"
        "NULL::text AS arguments UNION ALL "
        "SELECT 'schema',n.nspname,NULL,NULL FROM pg_namespace n "
        "WHERE n.nspname<>'information_schema' AND n.nspname!~'^pg_' UNION ALL "
        "SELECT CASE WHEN c.relkind='S' THEN 'sequence' ELSE 'table' END,n.nspname,c.relname,NULL "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relkind=ANY(ARRAY['r','p','v','m','f','S']::char[]) "
        "AND n.nspname<>'information_schema' AND n.nspname!~'^pg_' UNION ALL "
        "SELECT CASE WHEN p.prokind='p' THEN 'procedure' ELSE 'function' END,n.nspname,p.proname,"
        "pg_get_function_identity_arguments(p.oid) FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname<>'information_schema' AND n.nspname!~'^pg_' UNION ALL "
        "SELECT 'type',n.nspname,t.typname,NULL FROM pg_type t "
        "JOIN pg_namespace n ON n.oid=t.typnamespace WHERE t.typtype IN ('c','d','e','r','m') "
        "AND n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
        "ORDER BY object_kind,schema_name,object_name,arguments"
    )
    return tuple(
        {
            "object_kind": _row_value(row, "object_kind"),
            "schema_name": _row_value(row, "schema_name", 1),
            "object_name": _row_value(row, "object_name", 2),
            "arguments": _row_value(row, "arguments", 3),
        }
        for row in cur.fetchall()
    )


def _mapped_role_rollback_sql(
    cur,
    *,
    role_name: str,
    snapshot: Mapping[str, Any],
    regrant_snapshots: tuple[dict[str, Any], ...],
) -> str:
    quoted_role = sql.Identifier(role_name).as_string(cur.connection)
    statements = [f"DELETE FROM public.fleet_worker_principals WHERE role_name={sql.Literal(role_name).as_string(cur.connection)};"]

    for regrant in regrant_snapshots:
        if regrant["object_kind"].startswith("default_"):
            scope = {
                "owner": regrant["owner"],
                "schema": regrant["schema"],
                "object_type": regrant["object_code"],
            }
            prefix, object_name = _default_acl_prefix(cur, scope)
            grantee = sql.Identifier(regrant["grantee"]).as_string(cur.connection)
            statements.append(f"{prefix} REVOKE ALL PRIVILEGES ON {object_name} FROM {grantee};")
            for grantable in (False, True):
                privileges = sorted(
                    item["privilege"] for item in regrant["prior_acl"] if bool(item["grantable"]) is grantable
                )
                if privileges:
                    suffix = " WITH GRANT OPTION" if grantable else ""
                    statements.append(
                        f"{prefix} GRANT {', '.join(privileges)} ON {object_name} TO {grantee}{suffix};"
                    )
        else:
            object_kind = regrant["object_kind"].upper()
            identity = _acl_target_sql(cur, regrant)
            grantee = sql.Identifier(regrant["grantee"]).as_string(cur.connection)
            statements.append(f"REVOKE ALL PRIVILEGES ON {object_kind} {identity} FROM {grantee};")
            statements.extend(
                _grant_acl_sql(
                    cur,
                    target=regrant,
                    grantee=regrant["grantee"],
                    acl=regrant["prior_acl"],
                )
            )

    if not snapshot["existed"]:
        statements.extend((f"DROP OWNED BY {quoted_role};", f"DROP ROLE {quoted_role};"))
        return "\n".join(statements) + "\n"

    for target in _all_direct_acl_targets(cur):
        statements.append(
            f"REVOKE ALL PRIVILEGES ON {target['object_kind'].upper()} {_acl_target_sql(cur, target)} "
            f"FROM {quoted_role};"
        )
    for scope in snapshot["default_acl_scopes"]:
        prefix, object_name = _default_acl_prefix(cur, scope)
        statements.append(f"{prefix} REVOKE ALL PRIVILEGES ON {object_name} FROM {quoted_role};")

    grouped: dict[tuple[str, str, str, str], list[Mapping[str, Any]]] = {}
    targets: dict[tuple[str, str, str, str], Mapping[str, Any]] = {}
    for acl in snapshot["direct_acls"]:
        key = (
            acl["object_kind"],
            acl["schema_name"] or "",
            acl["object_name"] or "",
            acl["arguments"] or "",
        )
        grouped.setdefault(key, []).append(acl)
        targets[key] = acl
    for key in sorted(grouped):
        statements.extend(_grant_acl_sql(cur, target=targets[key], grantee=role_name, acl=grouped[key]))

    default_grouped: dict[tuple[str, str, str], list[Mapping[str, Any]]] = {}
    for acl in snapshot["default_acls"]:
        key = (acl["owner"], acl["schema"] or "", acl["object_type"])
        default_grouped.setdefault(key, []).append(acl)
    for key in sorted(default_grouped):
        acl = default_grouped[key]
        prefix, object_name = _default_acl_prefix(
            cur, {"owner": key[0], "schema": key[1] or None, "object_type": key[2]}
        )
        for grantable in (False, True):
            privileges = sorted(item["privilege"] for item in acl if bool(item["grantable"]) is grantable)
            if privileges:
                suffix = " WITH GRANT OPTION" if grantable else ""
                statements.append(
                    f"{prefix} GRANT {', '.join(privileges)} ON {object_name} TO {quoted_role}{suffix};"
                )

    def restore_membership(parent_role: str, member_role: str, membership: Mapping[str, Any]) -> None:
        parent = sql.Identifier(parent_role).as_string(cur.connection)
        member = sql.Identifier(member_role).as_string(cur.connection)
        grantor = sql.Identifier(membership["grantor_role"]).as_string(cur.connection)
        statements.append(f"SET LOCAL ROLE {grantor};")
        statements.append(
            f"GRANT {parent} TO {member} WITH "
            f"ADMIN {'TRUE' if membership['admin_option'] else 'FALSE'}, "
            f"INHERIT {'TRUE' if membership['inherit_option'] else 'FALSE'}, "
            f"SET {'TRUE' if membership['set_option'] else 'FALSE'} GRANTED BY {grantor};"
        )
        statements.append("RESET ROLE;")

    for membership in snapshot["memberships"]:
        restore_membership(membership["parent_role"], role_name, membership)
    for membership in snapshot["granted_memberships"]:
        restore_membership(role_name, membership["member_role"], membership)
    principal = snapshot["principal_mapping"]
    if principal is not None:
        statements.append(
            "INSERT INTO public.fleet_worker_principals(role_name,worker_id,contract,created_at) VALUES("
            f"{sql.Literal(role_name).as_string(cur.connection)},"
            f"{sql.Literal(principal['worker_id']).as_string(cur.connection)},"
            f"{sql.Literal(principal['contract']).as_string(cur.connection)},"
            f"{sql.Literal(principal['created_at']).as_string(cur.connection)});"
        )
    attributes = snapshot["attributes"]
    role_attributes = (
        "NOLOGIN",
        "INHERIT" if attributes["rolinherit"] else "NOINHERIT",
        "SUPERUSER" if attributes["rolsuper"] else "NOSUPERUSER",
        "CREATEDB" if attributes["rolcreatedb"] else "NOCREATEDB",
        "CREATEROLE" if attributes["rolcreaterole"] else "NOCREATEROLE",
        "REPLICATION" if attributes["rolreplication"] else "NOREPLICATION",
        "BYPASSRLS" if attributes["rolbypassrls"] else "NOBYPASSRLS",
        f"CONNECTION LIMIT {attributes['rolconnlimit']}",
    )
    statements.append(f"ALTER ROLE {quoted_role} {' '.join(role_attributes)};")
    return "\n".join(statements) + "\n"


def _public_acl_rollback_sql(
    cur,
    *,
    connect_allowlist: tuple[str, ...],
    database_owner_before: str,
    database_owner_after: str,
    retired_admin_roles: tuple[str, ...],
) -> str:
    cur.execute("SELECT current_database() AS database_name")
    database_name = _row_value(cur.fetchone(), "database_name")
    quoted_database = f'"{database_name.replace(chr(34), chr(34) * 2)}"'
    statements: list[str] = []
    if database_owner_before != database_owner_after:
        cur.execute("SELECT format('%%I',%s::text) AS owner_name", (database_owner_before,))
        statements.append(f"ALTER DATABASE {quoted_database} OWNER TO {_row_value(cur.fetchone(), 'owner_name')};")
    if retired_admin_roles:
        cur.execute(
            "SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
            "FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname",
            (list(retired_admin_roles),),
        )
        for row in cur.fetchall():
            cur.execute(
                "SELECT format('%%I',%s::text) AS role_name",
                (_row_value(row, "rolname"),),
            )
            quoted_role = _row_value(cur.fetchone(), "role_name")
            attributes = (
                "LOGIN" if _row_value(row, "rolcanlogin", 1) else "NOLOGIN",
                "SUPERUSER" if _row_value(row, "rolsuper", 2) else "NOSUPERUSER",
                "CREATEDB" if _row_value(row, "rolcreatedb", 3) else "NOCREATEDB",
                "CREATEROLE" if _row_value(row, "rolcreaterole", 4) else "NOCREATEROLE",
                "REPLICATION" if _row_value(row, "rolreplication", 5) else "NOREPLICATION",
                "BYPASSRLS" if _row_value(row, "rolbypassrls", 6) else "NOBYPASSRLS",
            )
            statements.append(f"ALTER ROLE {quoted_role} {' '.join(attributes)};")
    for privilege in ("CONNECT", "CREATE", "TEMPORARY"):
        cur.execute(
            "SELECT has_database_privilege('public',current_database(),%s) AS allowed",
            (privilege,),
        )
        if _row_value(cur.fetchone(), "allowed"):
            statements.append(f"GRANT {privilege} ON DATABASE {quoted_database} TO PUBLIC;")
    cur.execute(
        "SELECT grantee.rolname FROM pg_catalog.pg_database d "
        "CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) acl "
        "JOIN pg_catalog.pg_roles grantee ON grantee.oid=acl.grantee "
        "WHERE d.datname=current_database() AND acl.privilege_type='CONNECT' "
        "ORDER BY grantee.rolname"
    )
    prior_direct_connect = {_row_value(row, "rolname") for row in cur.fetchall()}
    for role_name in connect_allowlist:
        if role_name not in prior_direct_connect:
            cur.execute("SELECT format('%%I',%s::text) AS role_name", (role_name,))
            quoted_role = _row_value(cur.fetchone(), "role_name")
            statements.append(f"REVOKE CONNECT ON DATABASE {quoted_database} FROM {quoted_role};")
    for role_name in sorted(set(retired_admin_roles) & prior_direct_connect):
        cur.execute("SELECT format('%%I',%s::text) AS role_name", (role_name,))
        statements.append(f"GRANT CONNECT ON DATABASE {quoted_database} TO {_row_value(cur.fetchone(), 'role_name')};")

    queries = (
        (
            "SCHEMA",
            ("USAGE", "CREATE"),
            "SELECT format('%%I',n.nspname) AS identity FROM pg_namespace n "
            "WHERE n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
            "AND has_schema_privilege('public',n.oid,%s) ORDER BY n.nspname",
        ),
        (
            "TABLE",
            ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER"),
            "SELECT format('%%I.%%I',n.nspname,c.relname) AS identity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind IN ('r','p','v','m','f') "
            "AND n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
            "AND has_table_privilege('public',c.oid,%s) ORDER BY n.nspname,c.relname",
        ),
        (
            "SEQUENCE",
            ("USAGE", "SELECT", "UPDATE"),
            "SELECT format('%%I.%%I',n.nspname,c.relname) AS identity FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.relkind='S' "
            "AND n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
            "AND CASE WHEN c.relkind='S' THEN has_sequence_privilege('public',c.oid,%s) "
            "ELSE FALSE END ORDER BY n.nspname,c.relname",
        ),
        (
            "FUNCTION",
            ("EXECUTE",),
            "SELECT p.oid::regprocedure::text AS identity FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
            "AND has_function_privilege('public',p.oid,%s) ORDER BY p.oid::regprocedure::text",
        ),
        (
            "TYPE",
            ("USAGE",),
            "SELECT format('%%I.%%I',n.nspname,t.typname) AS identity FROM pg_type t "
            "JOIN pg_namespace n ON n.oid=t.typnamespace "
            "WHERE n.nspname<>'information_schema' AND n.nspname!~'^pg_' "
            "AND t.typtype IN ('c','d','e','r','m') "
            "AND has_type_privilege('public',t.oid,%s) ORDER BY n.nspname,t.typname",
        ),
    )
    for object_kind, privileges, query in queries:
        for privilege in privileges:
            cur.execute(query, (privilege,))
            statements.extend(
                f"GRANT {privilege} ON {object_kind} {_row_value(row, 'identity')} TO PUBLIC;" for row in cur.fetchall()
            )
    cur.execute(
        "SELECT DISTINCT owner_name FROM (SELECT current_user AS owner_name UNION ALL "
        "SELECT r.rolname FROM pg_default_acl d JOIN pg_roles r ON r.oid=d.defaclrole) owners "
        "ORDER BY owner_name"
    )
    default_owners = tuple(_row_value(row, "owner_name") for row in cur.fetchall())
    for owner_name in default_owners:
        cur.execute("SELECT format('%%I',%s::text) AS owner_name", (owner_name,))
        quoted_owner = _row_value(cur.fetchone(), "owner_name")
        for object_type, acl_code, privilege in (
            ("FUNCTIONS", "f", "EXECUTE"),
            ("TYPES", "T", "USAGE"),
        ):
            cur.execute(
                "SELECT EXISTS(SELECT 1 FROM pg_default_acl d "
                "CROSS JOIN LATERAL aclexplode(d.defaclacl) acl "
                "WHERE d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=%s) "
                "AND d.defaclnamespace=0 AND d.defaclobjtype=%s "
                "AND acl.grantee=0 AND acl.privilege_type=%s) AS explicit, "
                "EXISTS(SELECT 1 FROM aclexplode(acldefault(%s,(SELECT oid FROM pg_roles WHERE rolname=%s))) acl "
                "WHERE acl.grantee=0 AND acl.privilege_type=%s) AS implicit",
                (owner_name, acl_code, privilege, acl_code, owner_name, privilege),
            )
            default_row = cur.fetchone()
            if _row_value(default_row, "explicit") or _row_value(default_row, "implicit", 1):
                statements.append(
                    f"ALTER DEFAULT PRIVILEGES FOR ROLE {quoted_owner} GRANT {privilege} ON {object_type} TO PUBLIC;"
                )
            cur.execute(
                "SELECT format('%%I',n.nspname) AS schema_name FROM pg_default_acl d "
                "JOIN pg_namespace n ON n.oid=d.defaclnamespace "
                "CROSS JOIN LATERAL aclexplode(d.defaclacl) acl "
                "WHERE d.defaclrole=(SELECT oid FROM pg_roles WHERE rolname=%s) "
                "AND d.defaclobjtype=%s AND acl.grantee=0 AND acl.privilege_type=%s "
                "ORDER BY n.nspname",
                (owner_name, acl_code, privilege),
            )
            statements.extend(
                "ALTER DEFAULT PRIVILEGES FOR ROLE "
                f"{quoted_owner} IN SCHEMA {_row_value(row, 'schema_name')} "
                f"GRANT {privilege} ON {object_type} TO PUBLIC;"
                for row in cur.fetchall()
            )
    cur.execute("SELECT to_regprocedure('public.fleet_worker_identity()') AS identity_oid")
    if _row_value(cur.fetchone(), "identity_oid") is None:
        statements.append("DROP FUNCTION IF EXISTS public.fleet_worker_identity();")
    return "\n".join(statements) + "\n"


def _install_identity_function(cur) -> None:
    cur.execute(
        "CREATE OR REPLACE FUNCTION public.fleet_worker_identity() RETURNS JSONB "
        "LANGUAGE SQL STABLE SECURITY DEFINER SET search_path=pg_catalog "
        "AS $$ SELECT pg_catalog.jsonb_build_object("
        "'role_name',p.role_name,'worker_id',p.worker_id,'contract',p.contract) "
        "FROM public.fleet_worker_principals p WHERE p.role_name=session_user $$"
    )
    cur.execute("REVOKE ALL PRIVILEGES ON FUNCTION public.fleet_worker_identity() FROM PUBLIC")
    cur.execute(
        "SELECT owner.rolname FROM pg_class relation "
        "JOIN pg_roles owner ON owner.oid=relation.relowner "
        "WHERE relation.oid='public.fleet_worker_principals'::regclass"
    )
    principal_owner = _row_value(cur.fetchone(), "rolname")
    cur.execute(
        sql.SQL("ALTER FUNCTION public.fleet_worker_identity() OWNER TO {}").format(
            sql.Identifier(principal_owner)
        )
    )


def _harden_database_connect(
    cur,
    *,
    database: sql.Identifier,
    database_owner_role: str,
    retired_admin_roles: tuple[str, ...],
) -> None:
    cur.execute(
        "SELECT owner.rolname FROM pg_database d JOIN pg_roles owner ON owner.oid=d.datdba "
        "WHERE d.datname=current_database()"
    )
    if _row_value(cur.fetchone(), "rolname") != database_owner_role:
        cur.execute(sql.SQL("ALTER DATABASE {} OWNER TO {}").format(database, sql.Identifier(database_owner_role)))
    cur.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(database))
    for role_name in retired_admin_roles:
        role = sql.Identifier(role_name)
        cur.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(database, role))
        cur.execute(
            "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
            "FROM pg_roles WHERE rolname=%s",
            (role_name,),
        )
        attributes = cur.fetchone()
        if attributes is not None and any(
            _row_value(attributes, name, index)
            for index, name in enumerate(
                ("rolcanlogin", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")
            )
        ):
            cur.execute(
                sql.SQL(
                    "ALTER ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(role)
            )


def _effective_connect_grantees(cur) -> tuple[dict[str, Any], ...]:
    cur.execute(
        "SELECT r.rolname,r.rolcanlogin,r.rolsuper,(r.oid=d.datdba) AS database_owner,"
        "has_database_privilege(r.oid,d.oid,'CONNECT') AS effective_connect "
        "FROM pg_roles r CROSS JOIN pg_database d WHERE d.datname=current_database() "
        "AND has_database_privilege(r.oid,d.oid,'CONNECT') ORDER BY r.rolname"
    )
    return tuple(
        {
            "role_name": _row_value(row, "rolname"),
            "can_login": _row_value(row, "rolcanlogin", 1),
            "superuser": _row_value(row, "rolsuper", 2),
            "database_owner": _row_value(row, "database_owner", 3),
            "effective_connect": _row_value(row, "effective_connect", 4),
            "reconnect_capable": _row_value(row, "rolcanlogin", 1)
            and _row_value(row, "effective_connect", 4),
        }
        for row in cur.fetchall()
    )


def _revoke_legacy_privileges(cur, *, role: sql.Identifier, database: sql.Identifier) -> None:
    cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(database, role))
    cur.execute(sql.SQL("REVOKE CONNECT, CREATE, TEMPORARY ON DATABASE {} FROM PUBLIC").format(database))
    schemas = _user_schemas(cur)
    for schema_name in schemas:
        schema = sql.Identifier(schema_name)
        cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(schema, role))
        cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM PUBLIC").format(schema))
        cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}").format(schema, role))
        cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM PUBLIC").format(schema))
        cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(schema, role))
        cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM PUBLIC").format(schema))
        cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA {} FROM {}").format(schema, role))
        cur.execute(
            "SELECT t.typname FROM pg_catalog.pg_type t "
            "JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace "
            "WHERE n.nspname=%s AND t.typtype IN ('c','d','e','r','m') ORDER BY t.typname",
            (schema_name,),
        )
        for type_row in cur.fetchall():
            cur.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON TYPE {}.{} FROM {}, PUBLIC").format(
                    schema,
                    sql.Identifier(_row_value(type_row, "typname")),
                    role,
                )
            )
        # Functions are executable by PUBLIC by default, which would make every
        # authority helper callable by fleet_worker regardless of its own ACL.
        cur.execute(sql.SQL("REVOKE EXECUTE ON ALL FUNCTIONS IN SCHEMA {} FROM PUBLIC").format(schema))

    cur.execute(
        "SELECT DISTINCT owner_name FROM ("
        "SELECT current_user AS owner_name "
        "UNION ALL "
        "SELECT r.rolname FROM pg_default_acl d "
        "JOIN pg_roles r ON r.oid=d.defaclrole "
        "WHERE pg_has_role(current_user,r.oid,'USAGE')"
        ") owners"
    )
    for row in cur.fetchall():
        owner = sql.Identifier(_row_value(row, "owner_name"))
        for object_type in ("TABLES", "SEQUENCES", "FUNCTIONS", "TYPES"):
            cur.execute(
                sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL PRIVILEGES ON {} FROM {}").format(
                    owner, sql.SQL(object_type), role
                )
            )
        cur.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC").format(owner)
        )
        cur.execute(sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE ALL ON TYPES FROM PUBLIC").format(owner))
        for schema_name in schemas:
            for object_type in ("TABLES", "SEQUENCES", "FUNCTIONS", "TYPES"):
                cur.execute(
                    sql.SQL(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} REVOKE ALL PRIVILEGES ON {} FROM {}"
                    ).format(
                        owner,
                        sql.Identifier(schema_name),
                        sql.SQL(object_type),
                        role,
                    )
                )
            cur.execute(
                sql.SQL(
                    "ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
                ).format(owner, sql.Identifier(schema_name))
            )
            cur.execute(
                sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA {} REVOKE ALL ON TYPES FROM PUBLIC").format(
                    owner, sql.Identifier(schema_name)
                )
            )


def _revoke_memberships(cur, *, role_name: str, role: sql.Identifier) -> None:
    cur.execute(
        "SELECT parent.rolname FROM pg_auth_members membership "
        "JOIN pg_roles parent ON parent.oid=membership.roleid "
        "JOIN pg_roles member ON member.oid=membership.member "
        "WHERE member.rolname=%s ORDER BY parent.rolname",
        (role_name,),
    )
    for row in cur.fetchall():
        cur.execute(
            sql.SQL("REVOKE {} FROM {}").format(
                sql.Identifier(_row_value(row, "rolname")),
                role,
            )
        )


def _owned_objects(cur, role_name: str) -> list[str]:
    cur.execute("SELECT oid FROM pg_roles WHERE rolname=%s", (role_name,))
    role_oid = _row_value(cur.fetchone(), "oid")
    owned: list[str] = []

    cur.execute(
        "SELECT datname FROM pg_database WHERE datname=current_database() AND datdba=%s",
        (role_oid,),
    )
    owned.extend(f"database:{_row_value(row, 'datname')}" for row in cur.fetchall())

    cur.execute(
        "SELECT nspname FROM pg_namespace WHERE nspowner=%s "
        "AND nspname <> 'information_schema' AND nspname !~ '^pg_' ORDER BY nspname",
        (role_oid,),
    )
    owned.extend(f"schema:{_row_value(row, 'nspname')}" for row in cur.fetchall())

    cur.execute(
        "SELECT n.nspname,c.relname,c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE c.relowner=%s AND c.relkind=ANY(%s) "
        "ORDER BY CASE WHEN c.relkind='S' THEN 1 ELSE 0 END,n.nspname,c.relname",
        (role_oid, ["r", "p", "v", "m", "f", "S"]),
    )
    for row in cur.fetchall():
        kind = "sequence" if _row_value(row, "relkind", 2) == "S" else "table"
        owned.append(f"{kind}:{_row_value(row, 'nspname')}.{_row_value(row, 'relname', 1)}")

    cur.execute(
        "SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) AS args "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE p.proowner=%s ORDER BY n.nspname,p.proname,args",
        (role_oid,),
    )
    owned.extend(
        f"function:{_row_value(row, 'nspname')}.{_row_value(row, 'proname', 1)}({_row_value(row, 'args', 2)})"
        for row in cur.fetchall()
    )

    cur.execute(
        "SELECT n.nspname,t.typname,t.typtype FROM pg_type t "
        "JOIN pg_namespace n ON n.oid=t.typnamespace "
        "WHERE t.typowner=%s AND n.nspname <> 'information_schema' "
        "AND n.nspname !~ '^pg_' AND t.typtype IN ('c','d','e','r','m') "
        "ORDER BY n.nspname,t.typname",
        (role_oid,),
    )
    for row in cur.fetchall():
        kind = "domain" if _row_value(row, "typtype", 2) == "d" else "type"
        owned.append(f"{kind}:{_row_value(row, 'nspname')}.{_row_value(row, 'typname', 1)}")

    cur.execute(
        "SELECT n.nspname,o.oprname FROM pg_operator o "
        "JOIN pg_namespace n ON n.oid=o.oprnamespace "
        "WHERE o.oprowner=%s AND n.nspname <> 'information_schema' "
        "AND n.nspname !~ '^pg_' ORDER BY n.nspname,o.oprname",
        (role_oid,),
    )
    owned.extend(f"operator:{_row_value(row, 'nspname')}.{_row_value(row, 'oprname', 1)}" for row in cur.fetchall())
    return owned


def _reject_pg_temp_shadow(cur) -> None:
    cur.execute(
        "SELECT kind,name FROM ("
        "SELECT 'relation' AS kind,c.relname AS name FROM pg_catalog.pg_class c "
        "WHERE c.relnamespace=pg_catalog.pg_my_temp_schema() "
        "UNION ALL SELECT 'function',p.proname FROM pg_catalog.pg_proc p "
        "WHERE p.pronamespace=pg_catalog.pg_my_temp_schema() "
        "UNION ALL SELECT CASE WHEN t.typtype='d' THEN 'domain' ELSE 'type' END,t.typname "
        "FROM pg_catalog.pg_type t WHERE t.typnamespace=pg_catalog.pg_my_temp_schema() "
        "UNION ALL SELECT 'operator',o.oprname FROM pg_catalog.pg_operator o "
        "WHERE o.oprnamespace=pg_catalog.pg_my_temp_schema()"
        ") shadow ORDER BY kind,name"
    )
    shadows = [f"{_row_value(row, 'kind')}:{_row_value(row, 'name', 1)}" for row in cur.fetchall()]
    if shadows:
        raise RuntimeError("fleet worker reconciliation rejects pg_temp object shadowing: " + ", ".join(shadows))


def _functions_for_contract(contract: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    try:
        return (*_COMMON_FUNCTIONS, *_CONTRACT_FUNCTIONS[contract])
    except KeyError:
        raise ValueError(f"unsupported fleet worker contract: {contract}") from None


def _validate_objects(cur, functions) -> None:
    expected_columns = {table: set(columns) for table, columns in _REQUIRED_FUNCTION_COLUMNS.items()}
    expected_columns["fleet_worker_principals"] = {"role_name", "worker_id", "contract"}
    expected_columns["workers"] = {"worker_id"}
    all_required_tables = set(expected_columns)
    cur.execute(
        "SELECT table_name, column_name FROM information_schema.columns "
        "WHERE table_schema='public' AND table_name=ANY(%s)",
        (list(all_required_tables),),
    )
    actual_columns: dict[str, set[str]] = {}
    for row in cur.fetchall():
        table = _row_value(row, "table_name", 0)
        actual_columns.setdefault(table, set()).add(_row_value(row, "column_name", 1))

    missing_tables = sorted(all_required_tables - actual_columns.keys())
    if missing_tables:
        raise RuntimeError("fleet_worker required tables missing: " + ", ".join(missing_tables))

    missing_columns = {
        table: sorted(columns - actual_columns[table])
        for table, columns in expected_columns.items()
        if columns - actual_columns[table]
    }
    if missing_columns:
        detail = "; ".join(f"{table}: {', '.join(columns)}" for table, columns in sorted(missing_columns.items()))
        raise RuntimeError("fleet_worker required columns missing: " + detail)

    missing_functions: list[str] = []
    for name, argument_types in functions:
        if name == "fleet_worker_identity":
            continue
        signature = f"public.{name}({','.join(argument_type.lower() for argument_type in argument_types)})"
        cur.execute("SELECT to_regprocedure(%s) AS oid", (signature,))
        if _row_value(cur.fetchone(), "oid") is None:
            missing_functions.append(signature)
    if missing_functions:
        raise RuntimeError("fleet_worker required functions missing: " + ", ".join(missing_functions))

    return None


def _grant_columns(cur, privilege: str, grants: Mapping[str, tuple[str, ...]], role) -> None:
    for table, columns in grants.items():
        cur.execute(
            sql.SQL("GRANT {} ({}) ON TABLE {} TO {}").format(
                sql.SQL(privilege),
                _identifiers(columns),
                sql.Identifier(table),
                role,
            )
        )


def _validate_effective_boundary(cur, *, role_name: str, functions, allowed_types: tuple[str, ...] = ()) -> None:
    cur.execute(
        "SELECT n.nspname,c.relname FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_' "
        "AND c.relkind IN ('r','p','v','m','f') AND ("
        "has_table_privilege(%s,c.oid,'INSERT') OR has_table_privilege(%s,c.oid,'UPDATE') OR "
        "has_table_privilege(%s,c.oid,'DELETE') OR has_table_privilege(%s,c.oid,'TRUNCATE') OR "
        "has_table_privilege(%s,c.oid,'REFERENCES') OR has_table_privilege(%s,c.oid,'TRIGGER') OR "
        "has_table_privilege(%s,c.oid,'SELECT')) "
        "ORDER BY n.nspname,c.relname",
        (role_name, role_name, role_name, role_name, role_name, role_name, role_name),
    )
    forbidden = [f"table:{_row_value(row, 'nspname')}.{_row_value(row, 'relname', 1)}" for row in cur.fetchall()]

    cur.execute(
        "SELECT n.nspname,c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_' AND c.relkind='S' "
        "AND (has_sequence_privilege(%s,c.oid,'USAGE') "
        "OR has_sequence_privilege(%s,c.oid,'SELECT') OR has_sequence_privilege(%s,c.oid,'UPDATE')) "
        "ORDER BY n.nspname,c.relname",
        (role_name, role_name, role_name),
    )
    forbidden.extend(f"sequence:{_row_value(row, 'nspname')}.{_row_value(row, 'relname', 1)}" for row in cur.fetchall())

    cur.execute(
        "SELECT n.nspname,t.typname FROM pg_catalog.pg_type t "
        "JOIN pg_catalog.pg_namespace n ON n.oid=t.typnamespace "
        "WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_' "
        "AND pg_catalog.has_type_privilege(%s,t.oid,'USAGE') "
        "AND NOT (n.nspname='public' AND t.typname=ANY(%s)) "
        "ORDER BY n.nspname,t.typname",
        (role_name, list(allowed_types)),
    )
    forbidden.extend(f"type:{_row_value(row, 'nspname')}.{_row_value(row, 'typname', 1)}" for row in cur.fetchall())

    allowed_function_oids: list[int] = []
    for name, argument_types in functions:
        signature = f"public.{name}({','.join(argument_types).lower()})"
        cur.execute("SELECT to_regprocedure(%s)::oid AS oid", (signature,))
        allowed_function_oids.append(_row_value(cur.fetchone(), "oid"))
    cur.execute(
        "SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) AS args "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname <> 'information_schema' AND n.nspname !~ '^pg_' "
        "AND has_function_privilege(%s,p.oid,'EXECUTE') "
        "AND p.oid <> ALL(%s) "
        "ORDER BY n.nspname,p.proname,args",
        (role_name, allowed_function_oids),
    )
    forbidden.extend(
        f"function:{_row_value(row, 'nspname')}.{_row_value(row, 'proname', 1)}({_row_value(row, 'args', 2)})"
        for row in cur.fetchall()
    )
    if forbidden:
        raise RuntimeError(
            "fleet_worker retains forbidden effective privileges after reconciliation: " + ", ".join(forbidden)
        )


def _client_scram_verifier(conn, *, role_name: str, password: str) -> str:
    try:
        verifier = conn.pgconn.encrypt_password(
            password.encode("utf-8"),
            role_name.encode("utf-8"),
            algorithm=b"scram-sha-256",
        ).decode("ascii")
    except Exception:
        raise RuntimeError("could not generate fleet worker password verifier") from None
    if not verifier.startswith("SCRAM-SHA-256$"):
        raise RuntimeError("could not generate fleet worker password verifier")
    return verifier


def _validate_post_password_boundary(
    cur,
    *,
    role_name: str,
    functions,
    allowed_types: tuple[str, ...],
    connect_allowlist: tuple[str, ...],
    database_owner_role: str,
    retired_admin_roles: tuple[str, ...],
    break_glass_roles: tuple[str, ...],
) -> tuple[dict[str, Any], ...]:
    cur.execute(
        "SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, "
        "rolreplication, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=%s",
        (role_name,),
    )
    row = cur.fetchone()
    attributes = (
        None
        if row is None
        else tuple(
            _row_value(row, name, index)
            for index, name in enumerate(
                (
                    "rolcanlogin",
                    "rolinherit",
                    "rolsuper",
                    "rolcreatedb",
                    "rolcreaterole",
                    "rolreplication",
                    "rolbypassrls",
                )
            )
        )
    )
    if attributes != (True, False, False, False, False, False, False):
        raise RuntimeError("fleet worker role attributes drifted during password reconciliation")

    cur.execute(
        "SELECT 1 FROM pg_catalog.pg_auth_members membership "
        "JOIN pg_catalog.pg_roles member ON member.oid=membership.member "
        "WHERE member.rolname=%s LIMIT 1",
        (role_name,),
    )
    if cur.fetchone() is not None:
        raise RuntimeError("fleet worker role membership drifted during password reconciliation")

    if _owned_objects(cur, role_name):
        raise RuntimeError("fleet worker role ownership drifted during password reconciliation")
    _validate_effective_boundary(cur, role_name=role_name, functions=functions, allowed_types=allowed_types)
    cur.execute(
        "SELECT pg_catalog.has_database_privilege(%s,pg_catalog.current_database(),'CONNECT') AS db_ok, "
        "pg_catalog.has_schema_privilege(%s,'public','USAGE') AS schema_ok",
        (role_name, role_name),
    )
    positive = cur.fetchone()
    if not _row_value(positive, "db_ok") or not _row_value(positive, "schema_ok", 1):
        raise RuntimeError("fleet worker required database/schema privileges are missing")
    cur.execute(
        "SELECT CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE grantee.rolname END AS grantee "
        "FROM pg_catalog.pg_database d "
        "CROSS JOIN LATERAL pg_catalog.aclexplode(d.datacl) acl "
        "LEFT JOIN pg_catalog.pg_roles grantee ON grantee.oid=acl.grantee "
        "WHERE d.datname=current_database() AND acl.privilege_type='CONNECT' "
        "ORDER BY grantee"
    )
    actual_connect = tuple(_row_value(row, "grantee") for row in cur.fetchall())
    if set(actual_connect) != set(connect_allowlist):
        raise RuntimeError(
            "database CONNECT ACL does not match controller/verifier/mapped role allowlist: "
            f"expected={sorted(connect_allowlist)!r}, actual={sorted(actual_connect)!r}"
        )
    cur.execute(
        "SELECT owner.rolname,owner.rolcanlogin,owner.rolsuper "
        "FROM pg_database d JOIN pg_roles owner ON owner.oid=d.datdba "
        "WHERE d.datname=current_database()"
    )
    owner = cur.fetchone()
    if (
        _row_value(owner, "rolname") != database_owner_role
        or _row_value(owner, "rolcanlogin", 1)
        or _row_value(owner, "rolsuper", 2)
    ):
        raise RuntimeError("database owner must be the dedicated NOLOGIN NOSUPERUSER role")
    if retired_admin_roles:
        cur.execute(
            "SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
            "FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname",
            (list(retired_admin_roles),),
        )
        elevated_retired = []
        for retired in cur.fetchall():
            attributes = tuple(
                _row_value(retired, name, index)
                for index, name in enumerate(
                    (
                        "rolcanlogin",
                        "rolsuper",
                        "rolcreatedb",
                        "rolcreaterole",
                        "rolreplication",
                        "rolbypassrls",
                    ),
                    start=1,
                )
            )
            if any(attributes):
                elevated_retired.append(_row_value(retired, "rolname"))
        if elevated_retired:
            raise RuntimeError(
                "retired admin roles retain LOGIN or elevated attributes: " + ", ".join(elevated_retired)
            )
    cur.execute(
        "SELECT rolname,rolcanlogin,rolsuper FROM pg_roles WHERE rolname=ANY(%s) ORDER BY rolname",
        (list(break_glass_roles),),
    )
    invalid_break_glass = [
        _row_value(row, "rolname")
        for row in cur.fetchall()
        if not _row_value(row, "rolcanlogin", 1) or not _row_value(row, "rolsuper", 2)
    ]
    if invalid_break_glass:
        raise RuntimeError(
            "break-glass roles must remain LOGIN SUPERUSER: " + ", ".join(invalid_break_glass)
        )
    effective = _effective_connect_grantees(cur)
    reconnect_roles = {row["role_name"] for row in effective if row["reconnect_capable"]}
    cur.execute(
        "SELECT rolname FROM pg_roles WHERE rolname=ANY(%s) AND rolcanlogin ORDER BY rolname",
        (list(connect_allowlist),),
    )
    expected_reconnect_roles = {_row_value(row, "rolname") for row in cur.fetchall()}
    if reconnect_roles != expected_reconnect_roles:
        raise RuntimeError(
            "effective reconnect roles do not match exact allowlist after inspecting all roles: "
            f"expected={sorted(expected_reconnect_roles)!r}, actual={sorted(reconnect_roles)!r}"
        )
    for name, argument_types in functions:
        signature = f"public.{name}({','.join(argument_types).lower()})"
        cur.execute(
            "SELECT pg_catalog.has_function_privilege(%s,%s,'EXECUTE') AS allowed",
            (role_name, signature),
        )
        if not _row_value(cur.fetchone(), "allowed"):
            raise RuntimeError(f"fleet worker required function privilege is missing: {signature}")
    return effective


def validate_runtime_principal(conn, *, worker_id: str, contract: str) -> RuntimePrincipal:
    """Fail closed unless this login is the exact mapped per-node principal."""
    if not worker_id or contract not in _CONTRACT_FUNCTIONS:
        raise RuntimeError("worker identity and supported contract are required")
    with conn.cursor() as cur:
        cur.execute(
            "SELECT session_user AS session_name,current_user AS current_name,"
            "public.fleet_worker_identity() AS principal"
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError("runtime database login has no fleet principal mapping")
    principal = _row_value(row, "principal", 2)
    if principal is None:
        raise RuntimeError("runtime database login has no fleet principal mapping")
    identity = RuntimePrincipal(
        session_user=_row_value(row, "session_name"),
        current_user=_row_value(row, "current_name", 1),
        worker_id=principal["worker_id"],
        contract=principal["contract"],
    )
    if identity.session_user != identity.current_user:
        raise RuntimeError("runtime database role switching is forbidden")
    if identity.session_user in {"postgres", "fleet_worker"}:
        raise RuntimeError("runtime database login must be a unique mapped per-node role")
    if identity.worker_id != worker_id:
        raise RuntimeError(f"worker identity mismatch: mapped={identity.worker_id!r}, configured={worker_id!r}")
    if identity.contract != contract:
        raise RuntimeError(f"worker contract mismatch: mapped={identity.contract!r}, configured={contract!r}")
    return identity


def _bootstrap_object_rollback_sql(cur, ownership: tuple[dict[str, Any], ...]) -> str:
    statements: list[str] = []
    for item in ownership:
        object_kind = item["object_kind"]
        owner = sql.Identifier(item["owner_before"])
        if object_kind == "schema":
            statement = sql.SQL("ALTER SCHEMA {} OWNER TO {}").format(
                sql.Identifier(item["schema_name"]), owner
            )
        elif object_kind in {"function", "procedure"}:
            identity = sql.SQL("{}.{}({})").format(
                sql.Identifier(item["schema_name"]),
                sql.Identifier(item["object_name"]),
                sql.SQL(item["arguments"]),
            )
            statement = sql.SQL("ALTER {} {} OWNER TO {}").format(
                sql.SQL(object_kind.upper()), identity, owner
            )
        else:
            statement = sql.SQL("ALTER {} {} OWNER TO {}").format(
                sql.SQL(object_kind.upper()),
                sql.Identifier(item["schema_name"], item["object_name"]),
                owner,
            )
        statements.append(statement.as_string(cur.connection) + ";")
    return "\n".join(statements) + ("\n" if statements else "")


def _bootstrap_inventory(cur, *, topology: BootstrapTopology) -> dict[str, Any]:
    role_names = (
        topology.database_owner_role,
        topology.controller_role,
        topology.verifier_role,
        topology.migrator_role,
        *topology.retired_admin_roles,
        *topology.infrastructure_superuser_roles,
    )
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]{0,62}", name) for name in role_names):
        raise ValueError("bootstrap role names must be plain PostgreSQL identifiers")
    permanent_roles = {
        topology.database_owner_role,
        topology.controller_role,
        topology.verifier_role,
        topology.migrator_role,
    }
    if len(permanent_roles) != 4:
        raise RuntimeError("bootstrap owner/controller/verifier/migrator roles must be distinct")
    if not topology.retired_admin_roles:
        raise RuntimeError("bootstrap requires at least one retired provider-admin role")
    if permanent_roles & set(topology.retired_admin_roles):
        raise RuntimeError("permanent topology roles must be disjoint from retired admins")
    if set(topology.infrastructure_superuser_roles) & (
        permanent_roles | set(topology.retired_admin_roles)
    ):
        raise RuntimeError("infrastructure superusers must be disjoint from managed topology roles")

    cur.execute(
        "SELECT session_user AS session_name,current_user AS current_name,current_database() AS database_name,"
        "owner.rolname AS database_owner FROM pg_database d JOIN pg_roles owner ON owner.oid=d.datdba "
        "WHERE d.datname=current_database()"
    )
    database = cur.fetchone()
    session_user = _row_value(database, "session_name")
    if session_user != _row_value(database, "current_name", 1):
        raise RuntimeError("bootstrap forbids SET ROLE and requires the provider-admin session identity")
    if session_user not in topology.infrastructure_superuser_roles:
        raise RuntimeError("bootstrap session_user must be explicitly listed as an infrastructure break-glass role")

    cur.execute(
        "SELECT rolname,rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
        "FROM pg_roles ORDER BY rolname"
    )
    roles = tuple(
        {
            "role_name": _row_value(row, "rolname"),
            "can_login": _row_value(row, "rolcanlogin", 1),
            "superuser": _row_value(row, "rolsuper", 2),
            "createdb": _row_value(row, "rolcreatedb", 3),
            "createrole": _row_value(row, "rolcreaterole", 4),
            "replication": _row_value(row, "rolreplication", 5),
            "bypassrls": _row_value(row, "rolbypassrls", 6),
        }
        for row in cur.fetchall()
    )
    role_index = {item["role_name"]: item for item in roles}
    admin = role_index.get(session_user)
    if not admin or not admin["can_login"] or not admin["superuser"]:
        raise RuntimeError("bootstrap requires a LOGIN SUPERUSER provider-admin session")
    existing_permanent = sorted(permanent_roles & role_index.keys())
    if existing_permanent:
        raise RuntimeError("one-time bootstrap topology roles already exist: " + ", ".join(existing_permanent))
    missing_retired = sorted(set(topology.retired_admin_roles) - role_index.keys())
    if missing_retired:
        raise RuntimeError("retired provider-admin roles do not exist: " + ", ".join(missing_retired))

    infrastructure = set(topology.infrastructure_superuser_roles)
    missing_infrastructure = sorted(infrastructure - role_index.keys())
    if missing_infrastructure:
        raise RuntimeError("infrastructure superuser roles do not exist: " + ", ".join(missing_infrastructure))
    unsafe_infrastructure = sorted(
        name
        for name in infrastructure
        if not role_index[name]["can_login"] or not role_index[name]["superuser"]
    )
    if unsafe_infrastructure:
        raise RuntimeError(
            "infrastructure break-glass roles must be LOGIN SUPERUSER roles: "
            + ", ".join(unsafe_infrastructure)
        )
    unknown_superusers = sorted(
        item["role_name"]
        for item in roles
        if item["superuser"]
        and item["role_name"] not in set(topology.retired_admin_roles)
        and item["role_name"] not in infrastructure
    )
    if unknown_superusers:
        raise RuntimeError("unknown superuser dependencies block bootstrap: " + ", ".join(unknown_superusers))

    cur.execute(
        "SELECT DISTINCT usename FROM pg_stat_activity WHERE datname=current_database() "
        "AND pid<>pg_backend_pid() AND usename IS NOT NULL ORDER BY usename"
    )
    active_services = tuple(_row_value(row, "usename") for row in cur.fetchall())
    expected_services = set(topology.expected_service_roles)
    unknown_services = sorted(set(active_services) - expected_services)
    missing_services = sorted(expected_services - set(active_services))
    if unknown_services or missing_services:
        raise RuntimeError(
            "bootstrap active service inventory mismatch: "
            f"unknown={unknown_services!r}, missing={missing_services!r}"
        )

    cur.execute(
        "SELECT CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE grantee.rolname END AS grantee "
        "FROM pg_database d CROSS JOIN LATERAL aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) acl "
        "LEFT JOIN pg_roles grantee ON grantee.oid=acl.grantee "
        "WHERE d.datname=current_database() AND acl.privilege_type='CONNECT' "
        "AND (acl.grantee=0 OR acl.grantee<>d.datdba) ORDER BY grantee"
    )
    direct_connect = tuple(_row_value(row, "grantee") for row in cur.fetchall())
    unknown_connect = sorted(
        set(direct_connect)
        - {"PUBLIC"}
        - set(topology.retired_admin_roles)
        - infrastructure
    )
    if unknown_connect:
        raise RuntimeError(
            "unknown explicit CONNECT dependencies block bootstrap: " + ", ".join(unknown_connect)
        )

    ownership = _transfer_application_ownership(
        cur, new_owner_role=topology.migrator_role, apply=False
    )
    return {
        "session_user": session_user,
        "database_name": _row_value(database, "database_name", 2),
        "database_owner_before": _row_value(database, "database_owner", 3),
        "roles": roles,
        "active_services": active_services,
        "direct_connect_before": direct_connect,
        "application_ownership": ownership,
    }


def _validate_bootstrap_boundary(cur, *, topology: BootstrapTopology) -> tuple[dict[str, Any], ...]:
    expected_attributes = {
        topology.database_owner_role: (False, False, False, False, False, False),
        topology.migrator_role: (False, False, False, False, False, False),
        topology.verifier_role: (False, False, False, False, False, False),
        topology.controller_role: (True, False, False, True, False, False),
    }
    for role_name, expected in expected_attributes.items():
        cur.execute(
            "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
            "FROM pg_roles WHERE rolname=%s",
            (role_name,),
        )
        row = cur.fetchone()
        actual = tuple(
            _row_value(row, name, index)
            for index, name in enumerate(
                ("rolcanlogin", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")
            )
        )
        if actual != expected:
            raise RuntimeError(f"bootstrap role attributes invalid for {role_name}: {actual!r}")
    for role_name in topology.retired_admin_roles:
        cur.execute(
            "SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
            "FROM pg_roles WHERE rolname=%s",
            (role_name,),
        )
        row = cur.fetchone()
        if row is None or any(
            _row_value(row, name, index)
            for index, name in enumerate(
                ("rolcanlogin", "rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")
            )
        ):
            raise RuntimeError(f"retired provider admin remains elevated: {role_name}")
    for role_name in topology.infrastructure_superuser_roles:
        cur.execute(
            "SELECT rolcanlogin,rolsuper FROM pg_roles WHERE rolname=%s",
            (role_name,),
        )
        row = cur.fetchone()
        if row is None or not _row_value(row, "rolcanlogin") or not _row_value(row, "rolsuper", 1):
            raise RuntimeError(f"infrastructure break-glass role was not preserved: {role_name}")

    cur.execute(
        "SELECT owner.rolname FROM pg_database d JOIN pg_roles owner ON owner.oid=d.datdba "
        "WHERE d.datname=current_database()"
    )
    if _row_value(cur.fetchone(), "rolname") != topology.database_owner_role:
        raise RuntimeError("bootstrap database ownership transfer failed")
    cur.execute(
        "SELECT CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE grantee.rolname END AS grantee "
        "FROM pg_database d CROSS JOIN LATERAL aclexplode(d.datacl) acl "
        "LEFT JOIN pg_roles grantee ON grantee.oid=acl.grantee "
        "WHERE d.datname=current_database() AND acl.privilege_type='CONNECT' ORDER BY grantee"
    )
    actual_connect = {_row_value(row, "grantee") for row in cur.fetchall()}
    expected_connect = {
        topology.database_owner_role,
        topology.controller_role,
        topology.verifier_role,
        *topology.infrastructure_superuser_roles,
    }
    if actual_connect != expected_connect:
        raise RuntimeError(
            "bootstrap CONNECT ACL mismatch: "
            f"expected={sorted(expected_connect)!r}, actual={sorted(actual_connect)!r}"
        )
    effective = _effective_connect_grantees(cur)
    reconnect = {row["role_name"] for row in effective if row["reconnect_capable"]}
    expected_reconnect = {topology.controller_role, *topology.infrastructure_superuser_roles}
    if reconnect != expected_reconnect:
        raise RuntimeError(
            "bootstrap reconnect principals are not controller plus break-glass only: "
            f"expected={sorted(expected_reconnect)!r}, actual={sorted(reconnect)!r}"
        )
    return effective


def bootstrap_database_roles(
    conn,
    controller_password: str,
    *,
    topology: BootstrapTopology,
    evidence_writer: Callable[[Mapping[str, Any], str], None] | None,
) -> BootstrapReceipt:
    """One-time provider-admin bootstrap with pre-mutation durable evidence."""
    if evidence_writer is None:
        raise RuntimeError("durable bootstrap evidence writer is required")
    if conn.info.transaction_status.name != "IDLE":
        raise RuntimeError("bootstrap requires an idle provider-admin connection")
    verifier = _client_scram_verifier(
        conn, role_name=topology.controller_role, password=controller_password
    )
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL search_path=pg_catalog")
            cur.execute(
                "SELECT pg_advisory_xact_lock(pg_catalog.hashtext(%s))",
                ("applypilot:fleet-role-bootstrap:v1",),
            )
            inventory = _bootstrap_inventory(cur, topology=topology)
            connect_allowlist = (
                topology.database_owner_role,
                topology.controller_role,
                topology.verifier_role,
                *topology.infrastructure_superuser_roles,
            )
            rollback_sql = _public_acl_rollback_sql(
                cur,
                connect_allowlist=connect_allowlist,
                database_owner_before=inventory["database_owner_before"],
                database_owner_after=topology.database_owner_role,
                retired_admin_roles=topology.retired_admin_roles,
            )
            rollback_sql += _bootstrap_object_rollback_sql(
                cur, inventory["application_ownership"]
            )
            for granted_role in (topology.database_owner_role, topology.migrator_role):
                rollback_sql += sql.SQL("REVOKE {} FROM {};\n").format(
                    sql.Identifier(granted_role), sql.Identifier(topology.controller_role)
                ).as_string(conn)
            for role_name in (
                topology.controller_role,
                topology.verifier_role,
                topology.migrator_role,
                topology.database_owner_role,
            ):
                rollback_sql += sql.SQL("DROP OWNED BY {};\nDROP ROLE {};\n").format(
                    sql.Identifier(role_name), sql.Identifier(role_name)
                ).as_string(conn)

            prepared_at = datetime.now(timezone.utc).isoformat()
            evidence_writer(
                {
                    **inventory,
                    "topology": {
                        "database_owner_role": topology.database_owner_role,
                        "controller_role": topology.controller_role,
                        "verifier_role": topology.verifier_role,
                        "migrator_role": topology.migrator_role,
                        "retired_admin_roles": topology.retired_admin_roles,
                        "infrastructure_superuser_roles": topology.infrastructure_superuser_roles,
                    },
                    "prepared_at": prepared_at,
                    "escalation_required": False,
                },
                rollback_sql,
            )

            cur.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(topology.database_owner_role))
            )
            cur.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(topology.migrator_role))
            )
            cur.execute(
                sql.SQL(
                    "CREATE ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                ).format(sql.Identifier(topology.verifier_role))
            )
            cur.execute(
                sql.SQL(
                    "CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB CREATEROLE NOREPLICATION "
                    "NOBYPASSRLS PASSWORD {}"
                ).format(sql.Identifier(topology.controller_role), sql.Literal(verifier))
            )
            for granted_role in (topology.database_owner_role, topology.migrator_role):
                cur.execute(
                    sql.SQL("GRANT {} TO {}").format(
                        sql.Identifier(granted_role), sql.Identifier(topology.controller_role)
                    )
                )
            _transfer_application_ownership(cur, new_owner_role=topology.migrator_role)
            database = sql.Identifier(inventory["database_name"])
            cur.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}").format(
                    database, sql.Identifier(topology.database_owner_role)
                )
            )
            cur.execute(sql.SQL("REVOKE CONNECT ON DATABASE {} FROM PUBLIC").format(database))
            for role_name in topology.retired_admin_roles:
                cur.execute(
                    sql.SQL("REVOKE CONNECT ON DATABASE {} FROM {}").format(
                        database, sql.Identifier(role_name)
                    )
                )
            for role_name in connect_allowlist:
                cur.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                        database, sql.Identifier(role_name)
                    )
                )
            for role_name in topology.retired_admin_roles:
                cur.execute(
                    sql.SQL(
                        "ALTER ROLE {} NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier(role_name))
                )
            effective = _validate_bootstrap_boundary(cur, topology=topology)
        conn.commit()
        return BootstrapReceipt(
            database_name=inventory["database_name"],
            session_user=inventory["session_user"],
            topology={
                "database_owner_role": topology.database_owner_role,
                "controller_role": topology.controller_role,
                "verifier_role": topology.verifier_role,
                "migrator_role": topology.migrator_role,
                "retired_admin_roles": topology.retired_admin_roles,
                "infrastructure_superuser_roles": topology.infrastructure_superuser_roles,
            },
            inventory=inventory,
            effective_connect_grantees=effective,
            rollback_sql=rollback_sql,
            escalation_required=False,
            bootstrapped_at=prepared_at,
        )
    except BaseException:
        conn.rollback()
        raise


def ensure_fleet_worker_role(
    conn,
    password: str,
    *,
    role: str = DEFAULT_ROLE,
    worker_id: str | None = None,
    contract: str | None = None,
    regrant_manifest: RegrantManifest | None = None,
    evidence_writer: Callable[[Mapping[str, Any], str], None] | None = None,
) -> RoleReconciliationReceipt:
    """Create or reconcile the remote-worker role on the current database.

    A locked, secret-free inventory and rollback SQL are handed to the required
    durable evidence writer before any persistent mutation. All database changes
    and final validation then commit in one transaction.
    """
    if contract is not None and contract not in _CONTRACT_FUNCTIONS:
        raise ValueError(f"unsupported fleet worker contract: {contract}")
    if regrant_manifest is None:
        raise RuntimeError("explicit regrant manifest is required before database-wide ACL changes")
    if evidence_writer is None:
        raise RuntimeError("durable evidence writer is required before database hardening")
    if conn.info.transaction_status.name != "IDLE":
        raise RuntimeError("role reconciliation requires an idle connection for its single transaction")
    password_verifier = _client_scram_verifier(conn, role_name=role, password=password)
    role_identifier = sql.Identifier(role)
    try:
        with conn.cursor() as cur:
            cur.execute("SET LOCAL search_path=pg_catalog")
            cur.execute("SELECT pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtext(%s))", (_ROLE_LOCK_KEY,))
            cur.execute("SELECT 1 FROM pg_catalog.pg_roles WHERE rolname=%s", (role,))
            role_exists = cur.fetchone() is not None
            if role_exists:
                cur.execute(
                    "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                    "FROM pg_catalog.pg_roles WHERE rolname=%s",
                    (role,),
                )
                elevated = cur.fetchone()
                if any(
                    _row_value(elevated, name, index)
                    for index, name in enumerate(
                        ("rolsuper", "rolcreatedb", "rolcreaterole", "rolreplication", "rolbypassrls")
                    )
                ):
                    raise RuntimeError(
                        "elevated worker role requires one-time provider-admin cleanup before reconciliation"
                    )

            cur.execute("SELECT current_database() AS current_database")
            database = sql.Identifier(_row_value(cur.fetchone(), "current_database"))
            inventory = _database_inventory(cur, role_name=role, manifest=regrant_manifest)
            _reject_pg_temp_shadow(cur)
            owned = _owned_objects(cur, role) if role_exists else []
            if owned:
                raise RuntimeError(
                    "fleet_worker owns database objects that bypass ACL hardening; "
                    "ownership must be repaired explicitly: " + ", ".join(owned)
                )

            target_snapshot = _target_role_snapshot(cur, role_name=role, role_exists=role_exists)
            existing_principal = target_snapshot["principal_mapping"]
            if existing_principal is not None:
                mapped_worker = existing_principal["worker_id"]
                mapped_contract = existing_principal["contract"]
                if worker_id is not None and worker_id != mapped_worker:
                    raise RuntimeError("fleet worker role is already mapped to a different node identity")
                if contract is not None and contract != mapped_contract:
                    raise RuntimeError("fleet worker role is already mapped to a different contract")
            effective_worker = worker_id or (
                existing_principal["worker_id"] if existing_principal else None
            )
            effective_contract = contract or (
                existing_principal["contract"] if existing_principal else None
            )
            if not effective_worker or not effective_contract:
                raise RuntimeError("fleet worker principal mapping is required before LOGIN can be enabled")
            functions = _functions_for_contract(effective_contract)
            allowed_types = _CONTRACT_TYPES[effective_contract]
            _validate_objects(cur, functions)
            regrant_snapshots = _structured_regrant_snapshot(cur, inventory["regrants"])
            cur.execute(
                "SELECT 1 FROM public.workers WHERE worker_id=%s",
                (effective_worker,),
            )
            if cur.fetchone() is None:
                raise RuntimeError(f"fleet worker enrollment is missing for worker_id {effective_worker!r}")

            rollback_sql = _public_acl_rollback_sql(
                cur,
                connect_allowlist=inventory["connect_allowlist"],
                database_owner_before=inventory["database_owner_before"],
                database_owner_after=inventory["database_owner_after"],
                retired_admin_roles=inventory["retired_admin_roles"],
            )
            rollback_sql += _mapped_role_rollback_sql(
                cur,
                role_name=role,
                snapshot=target_snapshot,
                regrant_snapshots=regrant_snapshots,
            )
            prepared_at = datetime.now(timezone.utc).isoformat()
            evidence_inventory = {
                **inventory,
                "target_role": role,
                "target_role_existed": role_exists,
                "target_role_snapshot": target_snapshot,
                "structured_regrant_acl_snapshots": regrant_snapshots,
                "credential_forward_reconcile_required": role_exists,
                "worker_id": effective_worker,
                "contract": effective_contract,
                "prepared_at": prepared_at,
            }
            evidence_writer(evidence_inventory, rollback_sql)

            if not role_exists:
                cur.execute(sql.SQL("CREATE ROLE {}").format(role_identifier))
            _install_identity_function(cur)
            _harden_database_connect(
                cur,
                database=database,
                database_owner_role=regrant_manifest.database_owner_role,
                retired_admin_roles=regrant_manifest.retired_admin_roles,
            )
            _revoke_legacy_privileges(cur, role=role_identifier, database=database)
            _revoke_memberships(cur, role_name=role, role=role_identifier)
            cur.execute(sql.SQL("ALTER ROLE {} NOLOGIN NOINHERIT").format(role_identifier))
            cur.execute(
                "INSERT INTO public.fleet_worker_principals(role_name,worker_id,contract) "
                "VALUES(%s,%s,%s) ON CONFLICT(role_name) DO UPDATE SET "
                "worker_id=EXCLUDED.worker_id,contract=EXCLUDED.contract",
                (role, effective_worker, effective_contract),
            )
            cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, role_identifier))
            for allowed_role in inventory["connect_allowlist"]:
                cur.execute(
                    sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(database, sql.Identifier(allowed_role))
                )
            cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier("public"), role_identifier))
            for name, argument_types in functions:
                function = _function_identifier(name, argument_types)
                cur.execute(sql.SQL("REVOKE ALL PRIVILEGES ON FUNCTION {} FROM PUBLIC").format(function))
                cur.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(function, role_identifier))
            for type_name in allowed_types:
                if type_name.startswith("_"):
                    continue
                cur.execute(
                    sql.SQL("GRANT USAGE ON TYPE {}.{} TO {}").format(
                        sql.Identifier("public"), sql.Identifier(type_name), role_identifier
                    )
                )
            _apply_regrants(cur, inventory["regrants"])
            _validate_effective_boundary(cur, role_name=role, functions=functions, allowed_types=allowed_types)
            try:
                cur.execute(
                    sql.SQL("ALTER ROLE {} LOGIN NOINHERIT PASSWORD {}").format(
                        role_identifier, sql.Literal(password_verifier)
                    )
                )
            except Exception:
                raise RuntimeError("fleet worker password update failed") from None
            effective_connect_grantees = _validate_post_password_boundary(
                cur,
                role_name=role,
                functions=functions,
                allowed_types=allowed_types,
                connect_allowlist=inventory["connect_allowlist"],
                database_owner_role=regrant_manifest.database_owner_role,
                retired_admin_roles=regrant_manifest.retired_admin_roles,
                break_glass_roles=regrant_manifest.infrastructure_superuser_roles,
            )
        conn.commit()
        return RoleReconciliationReceipt(
            role_name=role,
            worker_id=effective_worker,
            contract=effective_contract,
            connect_allowlist=inventory["connect_allowlist"],
            effective_connect_grantees=effective_connect_grantees,
            inventory=evidence_inventory,
            rollback_sql=rollback_sql,
            credential_forward_reconcile_required=role_exists,
            reconciled_at=prepared_at,
        )
    except BaseException:
        conn.rollback()
        raise
