#!/usr/bin/env python3
"""One-time provider-admin bootstrap for canonical fleet PostgreSQL roles."""

from __future__ import annotations

import argparse
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import tempfile

import psycopg
from psycopg.rows import dict_row

from applypilot.brain.schema import ensure_brain_schema_v4, verify_brain_schema_v4
from applypilot.fleet.pg_roles import (
    BootstrapTopology,
    bootstrap_database_roles,
    ensure_brain_candidate_roles,
)


def _safe_path(value: str) -> Path:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise argparse.ArgumentTypeError("artifact path contains empty or control-character data")
    return Path(value).resolve()


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    _fsync_parent(path)


def _replace_durable(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".applypilot-bootstrap-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_parent(path)
    finally:
        temporary.unlink(missing_ok=True)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-owner-role", required=True)
    parser.add_argument("--controller-role", required=True)
    parser.add_argument("--verifier-role", required=True)
    parser.add_argument("--migrator-role", required=True)
    parser.add_argument("--retired-admin-role", action="append", required=True)
    parser.add_argument("--expected-service-role", action="append", default=[])
    parser.add_argument("--infrastructure-superuser-role", action="append", default=[])
    parser.add_argument("--receipt-path", required=True, type=_safe_path)
    parser.add_argument("--rollback-sql", required=True, type=_safe_path)
    return parser


def _install_v4_authority(conn):
    """Install and verify V4 only after its capability roles exist."""
    ensure_brain_candidate_roles(conn)
    ensure_brain_schema_v4(conn)
    candidate_roles = ensure_brain_candidate_roles(conn)
    verify_brain_schema_v4(conn)
    return candidate_roles


def main() -> int:
    args = _parser().parse_args()
    if args.receipt_path == args.rollback_sql:
        raise SystemExit("bootstrap receipt and rollback SQL paths must be distinct")
    collisions = [path for path in (args.receipt_path, args.rollback_sql) if path.exists()]
    if collisions:
        raise SystemExit("bootstrap refuses to overwrite evidence: " + ", ".join(map(str, collisions)))

    admin_dsn = os.environ.pop("APPLYPILOT_ADMIN_PG_DSN", "")
    controller_password = os.environ.pop("APPLYPILOT_CONTROLLER_PG_PASSWORD", "")
    if not admin_dsn or not controller_password:
        raise SystemExit(
            "APPLYPILOT_ADMIN_PG_DSN and APPLYPILOT_CONTROLLER_PG_PASSWORD are required only for bootstrap"
        )
    topology = BootstrapTopology(
        database_owner_role=args.database_owner_role,
        controller_role=args.controller_role,
        verifier_role=args.verifier_role,
        migrator_role=args.migrator_role,
        retired_admin_roles=tuple(args.retired_admin_role),
        expected_service_roles=tuple(args.expected_service_role),
        infrastructure_superuser_roles=tuple(args.infrastructure_superuser_role),
    )

    def write_evidence(inventory, rollback_sql: str) -> None:
        encoded = rollback_sql.encode("utf-8")
        receipt = {
            "status": "prepared_before_database_mutation",
            "inventory": inventory,
            "rollback_sql_path": str(args.rollback_sql),
            "rollback_sql_sha256": hashlib.sha256(encoded).hexdigest(),
            "escalation_required": True,
            "in_doubt": True,
        }
        _write_exclusive(args.rollback_sql, encoded)
        _write_exclusive(
            args.receipt_path,
            (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        )

    with psycopg.connect(admin_dsn, row_factory=dict_row) as conn:
        receipt = bootstrap_database_roles(
            conn,
            controller_password,
            topology=topology,
            evidence_writer=write_evidence,
        )
        candidate_roles = _install_v4_authority(conn)
    result = asdict(receipt)
    rollback_sql = result.pop("rollback_sql")
    result.update(
        status="bootstrap_committed",
        escalation_required=False,
        in_doubt=False,
        rollback_sql_path=str(args.rollback_sql),
        rollback_sql_sha256=hashlib.sha256(rollback_sql.encode("utf-8")).hexdigest(),
        candidate_roles=asdict(candidate_roles),
    )
    _replace_durable(
        args.receipt_path,
        (json.dumps(result, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    print(f"bootstrap committed; receipt={args.receipt_path}; rollback={args.rollback_sql}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
