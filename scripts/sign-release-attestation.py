#!/usr/bin/env python3
"""Sign an unsigned release receipt for the compatibility manifest verifier."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import hmac
import json
import os
import stat
from pathlib import Path
from typing import Any


_ALGORITHM = "HMAC-SHA256"
_KEY_ENV = "APPLYPILOT_RELEASE_ATTESTATION_KEY_B64"
_KEY_ID_ENV = "APPLYPILOT_RELEASE_ATTESTATION_KEY_ID"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="strict unsigned JSON receipt")
    parser.add_argument("--output", type=Path, required=True, help="new signed JSON receipt")
    return parser


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _attestation_configuration() -> tuple[bytes, str]:
    encoded_key = os.environ.get(_KEY_ENV)
    key_id = os.environ.get(_KEY_ID_ENV)
    if not _nonempty_string(encoded_key) or not _nonempty_string(key_id):
        raise RuntimeError("release attestation key and key ID environment variables are required")
    try:
        key = base64.b64decode(encoded_key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("release attestation key must be valid base64") from exc
    if len(key) < 32:
        raise RuntimeError("release attestation key must decode to at least 32 bytes")
    return key, key_id


def _absolute_lexical_path(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _reject_symlink_components(path: Path) -> None:
    absolute = _absolute_lexical_path(path)
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            raise RuntimeError(f"path must not contain symlinks: {path}")


def _validate_paths(input_path: Path, output_path: Path) -> tuple[Path, Path]:
    source = _absolute_lexical_path(input_path)
    target = _absolute_lexical_path(output_path)
    _reject_symlink_components(source)
    _reject_symlink_components(target)
    if os.path.normcase(str(source)) == os.path.normcase(str(target)):
        raise RuntimeError("input and output must not be the same file")
    try:
        source_stat = source.stat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"input file does not exist: {input_path}") from exc
    if not stat.S_ISREG(source_stat.st_mode):
        raise RuntimeError(f"input must be a regular file: {input_path}")
    if target.exists() or target.is_symlink():
        raise RuntimeError(f"output already exists; refusing to overwrite: {output_path}")
    if not target.parent.is_dir():
        raise RuntimeError(f"output parent directory does not exist: {output_path.parent}")
    return source, target


def _file_identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        stat_result.st_dev,
        stat_result.st_ino,
        stat_result.st_size,
        stat_result.st_mtime_ns,
    )


def _stable_read_bytes(path: Path) -> bytes:
    before = path.stat()
    first = path.read_bytes()
    middle = path.stat()
    second = path.read_bytes()
    after = path.stat()
    if len({_file_identity(item) for item in (before, middle, after)}) != 1 or first != second:
        raise RuntimeError(f"release receipt changed while reading: {path}")
    return second


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not permitted: {value}")


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key is not permitted: {key}")
        result[key] = value
    return result


def _parse_receipt(content: bytes, *, require_unsigned: bool) -> dict[str, Any]:
    try:
        text = content.decode("utf-8-sig")
        receipt = json.loads(text, object_pairs_hook=_strict_object, parse_constant=_reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("release receipt must be valid UTF-8 JSON") from exc
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if not isinstance(receipt, dict):
        raise RuntimeError("release receipt must be a JSON object")
    if require_unsigned and "authentication" in receipt:
        raise RuntimeError("unsigned release receipt already contains authentication")
    return receipt


def _canonical_receipt_payload(receipt: dict[str, Any]) -> bytes:
    payload = {key: value for key, value in receipt.items() if key != "authentication"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _signed_receipt(receipt: dict[str, Any], key: bytes, key_id: str) -> dict[str, Any]:
    signature = base64.b64encode(hmac.digest(key, _canonical_receipt_payload(receipt), hashlib.sha256)).decode("ascii")
    return {
        **receipt,
        "authentication": {
            "algorithm": _ALGORITHM,
            "keyId": key_id,
            "signature": signature,
        },
    }


def _verify_signed_receipt(receipt: dict[str, Any], key: bytes, key_id: str) -> None:
    authentication = receipt.get("authentication")
    if not isinstance(authentication, dict) or set(authentication) != {"algorithm", "keyId", "signature"}:
        raise RuntimeError("signed release receipt has an invalid authentication object")
    if authentication.get("algorithm") != _ALGORITHM or authentication.get("keyId") != key_id:
        raise RuntimeError("signed release receipt authentication metadata does not match")
    signature = authentication.get("signature")
    if not _nonempty_string(signature):
        raise RuntimeError("signed release receipt authentication signature is missing")
    try:
        supplied = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("signed release receipt authentication signature is not valid base64") from exc
    expected = hmac.digest(key, _canonical_receipt_payload(receipt), hashlib.sha256)
    if not hmac.compare_digest(supplied, expected):
        raise RuntimeError("signed release receipt authentication signature is invalid")


def _fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_exclusive_fsync(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    created = False
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        created = True
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if descriptor is not None:
            os.close(descriptor)
        if created:
            try:
                path.unlink(missing_ok=True)
            finally:
                _fsync_directory(path.parent)
        raise
    _fsync_directory(path.parent)


def sign_release_receipt(input_path: Path, output_path: Path) -> None:
    source, target = _validate_paths(input_path, output_path)
    receipt = _parse_receipt(_stable_read_bytes(source), require_unsigned=True)
    key, key_id = _attestation_configuration()
    signed = _signed_receipt(receipt, key, key_id)
    encoded = (json.dumps(signed, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode("utf-8")
    try:
        _write_exclusive_fsync(target, encoded)
    except FileExistsError as exc:
        raise RuntimeError(f"output already exists; refusing to overwrite: {output_path}") from exc
    try:
        written = _parse_receipt(_stable_read_bytes(target), require_unsigned=False)
        _verify_signed_receipt(written, key, key_id)
    except BaseException:
        try:
            target.unlink(missing_ok=True)
        finally:
            _fsync_directory(target.parent)
        raise


def main() -> int:
    args = _parser().parse_args()
    try:
        sign_release_receipt(args.input, args.output)
    except (OSError, RuntimeError) as exc:
        raise SystemExit(f"error: {exc}") from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
