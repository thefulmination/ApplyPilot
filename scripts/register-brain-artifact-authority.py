#!/usr/bin/env python3
"""Register a signed immutable S3 artifact manifest without implicit secret inputs."""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import secrets
import stat
import sys
from pathlib import Path
from typing import Any, Sequence

from applypilot.brain.artifact_authority import (
    ArtifactAuthorityError,
    canonical_manifest_bytes,
    coordinate_artifact_registration,
    parse_json_strict,
)


MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_SECRET_BYTES = 16 * 1024


def _absolute_without_resolving(path: Path) -> Path:
    return path if path.is_absolute() else Path.cwd() / path


def _reject_symlink_components(path: Path, *, include_leaf: bool) -> Path:
    absolute = _absolute_without_resolving(path)
    parts = absolute.parts
    if any(part in {".", ".."} for part in parts[1:]):
        raise ArtifactAuthorityError("dot path components are forbidden")
    current = Path(parts[0])
    end = len(parts) if include_leaf else len(parts) - 1
    for part in parts[1:end]:
        current /= part
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            raise ArtifactAuthorityError(f"path component does not exist: {current}") from None
        if stat.S_ISLNK(mode) or getattr(os.path, "isjunction", lambda _path: False)(current):
            raise ArtifactAuthorityError(f"symbolic link path component is forbidden: {current}")
        if current != absolute and not stat.S_ISDIR(mode):
            raise ArtifactAuthorityError(f"path component is not a directory: {current}")
    return absolute


def read_secure_regular_file(path: Path, *, max_bytes: int, require_private: bool = False) -> bytes:
    absolute = _reject_symlink_components(path, include_leaf=True)
    try:
        leaf = os.lstat(absolute)
    except FileNotFoundError:
        raise ArtifactAuthorityError(f"input file does not exist: {absolute}") from None
    if stat.S_ISLNK(leaf.st_mode) or getattr(os.path, "isjunction", lambda _path: False)(absolute):
        raise ArtifactAuthorityError(f"symbolic link input is forbidden: {absolute}")
    if not stat.S_ISREG(leaf.st_mode):
        raise ArtifactAuthorityError(f"input is not a regular file: {absolute}")
    if require_private and os.name != "nt" and stat.S_IMODE(leaf.st_mode) & 0o077:
        raise ArtifactAuthorityError(f"secret input permissions are too broad: {absolute}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(absolute, flags)
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (leaf.st_dev, leaf.st_ino):
            raise ArtifactAuthorityError(f"input file changed while opening: {absolute}")
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        completed = os.fstat(descriptor)
        if (completed.st_size, completed.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns):
            raise ArtifactAuthorityError(f"input file changed while reading: {absolute}")
    finally:
        os.close(descriptor)
    content = b"".join(chunks)
    if len(content) > max_bytes:
        raise ArtifactAuthorityError(f"input file exceeds {max_bytes} bytes: {absolute}")
    return content


def assert_receipt_destination_available(path: Path) -> Path:
    absolute = _reject_symlink_components(path, include_leaf=False)
    parent = absolute.parent
    try:
        parent_mode = os.lstat(parent).st_mode
    except FileNotFoundError:
        raise ArtifactAuthorityError(f"receipt directory does not exist: {parent}") from None
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        raise ArtifactAuthorityError(f"receipt parent must be a real directory: {parent}")
    try:
        os.lstat(absolute)
    except FileNotFoundError:
        return absolute
    raise ArtifactAuthorityError(f"receipt already exists: {absolute}")


def _write_all(descriptor: int, payload: bytes) -> None:
    view = memoryview(payload)
    while view:
        written = os.write(descriptor, view)
        if written <= 0:
            raise ArtifactAuthorityError("receipt write made no progress")
        view = view[written:]


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def publish_receipt_create_only(path: Path, receipt: dict[str, Any]) -> None:
    destination = assert_receipt_destination_available(path)
    payload = canonical_manifest_bytes(receipt)
    temp = destination.with_name(f".{destination.name}.{secrets.token_hex(16)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temp, flags, 0o600)
    linked = False
    try:
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        try:
            os.link(temp, destination, follow_symlinks=False)
        except FileExistsError:
            raise ArtifactAuthorityError(f"receipt already exists: {destination}") from None
        except OSError as exc:
            if exc.errno in {errno.EEXIST, errno.EACCES} and destination.exists():
                raise ArtifactAuthorityError(f"receipt already exists: {destination}") from None
            raise ArtifactAuthorityError("atomic create-only receipt publication failed") from exc
        linked = True
        _fsync_directory(destination.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        if linked:
            _fsync_directory(destination.parent)


def _load_text_secret(path: Path, label: str) -> str:
    raw = read_secure_regular_file(path, max_bytes=MAX_SECRET_BYTES, require_private=True)
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactAuthorityError(f"{label} must be UTF-8") from exc
    value = value.strip()
    if not value or "\x00" in value or "\n" in value or "\r" in value:
        raise ArtifactAuthorityError(f"{label} must contain one non-empty line")
    return value


def _aws_client(credentials_path: Path, region: str) -> Any:
    if not re.fullmatch(r"[a-z0-9-]+", region):
        raise ArtifactAuthorityError("AWS region is invalid")
    credentials = parse_json_strict(
        read_secure_regular_file(credentials_path, max_bytes=MAX_SECRET_BYTES, require_private=True)
    )
    expected = {"accessKeyId", "secretAccessKey", "sessionToken"}
    if credentials.keys() != expected:
        raise ArtifactAuthorityError("AWS credentials must contain only accessKeyId, secretAccessKey, sessionToken")
    access_key = credentials["accessKeyId"]
    secret_key = credentials["secretAccessKey"]
    token = credentials["sessionToken"]
    if not isinstance(access_key, str) or not access_key or not isinstance(secret_key, str) or not secret_key:
        raise ArtifactAuthorityError("AWS credential fields must be non-empty text")
    if token is not None and (not isinstance(token, str) or not token):
        raise ArtifactAuthorityError("AWS sessionToken must be null or non-empty text")
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=f"https://s3.{region}.amazonaws.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        aws_session_token=token,
        config=Config(signature_version="s3v4", retries={"mode": "standard", "max_attempts": 3}),
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--signature", required=True, type=Path)
    parser.add_argument("--hmac-key-file", required=True, type=Path)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--aws-credentials", required=True, type=Path)
    parser.add_argument("--aws-region", required=True)
    parser.add_argument("--expected-system-id", required=True)
    parser.add_argument("--expected-database-name", required=True)
    parser.add_argument("--database-dsn-file", type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--recover-committed", action="store_true")
    return parser


def run(args: argparse.Namespace) -> dict[str, Any]:
    assert_receipt_destination_available(args.receipt)
    if args.dry_run and args.database_dsn_file is not None:
        raise ArtifactAuthorityError("dry-run forbids database inputs")
    if args.dry_run and args.recover_committed:
        raise ArtifactAuthorityError("dry-run cannot recover a committed request")
    if not args.dry_run and args.database_dsn_file is None:
        raise ArtifactAuthorityError("--database-dsn-file is required unless --dry-run is used")
    manifest = parse_json_strict(read_secure_regular_file(args.manifest, max_bytes=MAX_JSON_BYTES))
    envelope = parse_json_strict(read_secure_regular_file(args.signature, max_bytes=MAX_SECRET_BYTES))
    if envelope.get("keyId") != args.key_id:
        raise ArtifactAuthorityError("signature keyId does not match explicit --key-id")
    hmac_key = read_secure_regular_file(
        args.hmac_key_file, max_bytes=MAX_SECRET_BYTES, require_private=True
    )
    if len(hmac_key) < 32:
        raise ArtifactAuthorityError("HMAC key file must contain at least 32 bytes")
    s3_client = _aws_client(args.aws_credentials, args.aws_region)

    connection_factory = None
    if not args.dry_run:
        dsn = _load_text_secret(args.database_dsn_file, "database DSN")

        def connection_factory() -> Any:
            import psycopg

            return psycopg.connect(dsn)

    receipt = coordinate_artifact_registration(
        manifest,
        envelope,
        keys={args.key_id: hmac_key},
        expected_system_id=args.expected_system_id,
        expected_database_name=args.expected_database_name,
        s3_client=s3_client,
        connection_factory=connection_factory,
        dry_run=args.dry_run,
        allow_expired_replay=args.recover_committed,
    )
    publish_receipt_create_only(args.receipt, receipt)
    return receipt


def main(argv: Sequence[str] | None = None) -> int:
    try:
        receipt = run(_parser().parse_args(argv))
    except ArtifactAuthorityError as exc:
        print(f"artifact authority registration refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("artifact authority registration failed safely; no receipt was published", file=sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
