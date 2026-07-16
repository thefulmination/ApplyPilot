#!/usr/bin/env python3
"""Verify and atomically execute a prepared fleet PostgreSQL rollback."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import tempfile

import psycopg
from psycopg.rows import dict_row


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--rollback-sql", required=True, type=Path)
    parser.add_argument("--restore-hba", action="store_true")
    return parser


def _verified_inputs(receipt_path: Path, rollback_path: Path) -> tuple[dict, str]:
    receipt = json.loads(receipt_path.read_text(encoding="utf-8-sig"))
    rollback_bytes = rollback_path.read_bytes()
    expected = receipt.get("rollback_sql_sha256", "")
    actual = hashlib.sha256(rollback_bytes).hexdigest()
    if not expected or not hmac.compare_digest(expected, actual):
        raise SystemExit("rollback SQL SHA-256 does not match the durable receipt")
    return receipt, rollback_bytes.decode("utf-8")


def _break_glass_roles(receipt: dict) -> set[str]:
    inventory = receipt.get("inventory", {})
    topology = receipt.get("topology", {})
    return set(
        inventory.get("infrastructure_superuser_roles", ())
        or topology.get("infrastructure_superuser_roles", ())
    )


def _replace_atomic(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".applypilot-hba-rollback-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_hba_and_reload(conn, receipt: dict) -> None:
    hba_path = Path(receipt.get("hba_path", "")).resolve()
    backup_path = Path(receipt.get("hba_backup", "")).resolve()
    if not hba_path.is_file() or not backup_path.is_file() or hba_path == backup_path:
        raise SystemExit("receipt does not contain distinct existing HBA and backup paths")
    _replace_atomic(hba_path, backup_path.read_bytes())
    with conn.transaction():
        if not conn.execute("SELECT pg_reload_conf() AS reloaded").fetchone()["reloaded"]:
            raise RuntimeError("pg_reload_conf returned false after HBA restoration")


def main() -> int:
    args = _parser().parse_args()
    receipt, rollback_sql = _verified_inputs(args.receipt.resolve(), args.rollback_sql.resolve())
    admin_dsn = os.environ.pop("APPLYPILOT_ADMIN_PG_DSN", "")
    if not admin_dsn:
        raise SystemExit("APPLYPILOT_ADMIN_PG_DSN is required only for break-glass rollback")

    # The authenticated break-glass connection is deliberately established
    # before HBA replacement/reload so rollback authority cannot be locked out.
    with psycopg.connect(admin_dsn, row_factory=dict_row) as conn:
        identity = conn.execute(
            "SELECT session_user,current_user,rolsuper FROM pg_roles WHERE rolname=session_user"
        ).fetchone()
        conn.commit()
        if (
            identity["session_user"] != identity["current_user"]
            or not identity["rolsuper"]
            or identity["session_user"] not in _break_glass_roles(receipt)
        ):
            raise SystemExit("rollback session must be an explicitly receipted break-glass superuser")
        if args.restore_hba:
            _restore_hba_and_reload(conn, receipt)
        # psycopg's transaction context is the executable equivalent of
        # psql --single-transaction --set=ON_ERROR_STOP=on.
        with conn.transaction():
            conn.execute(rollback_sql)
    print("rollback committed atomically after SHA-256 verification")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
