"""Shared fail-closed primitives for ApplyPilot release evidence."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from pathlib import Path
from typing import Any


ALGORITHM = "HMAC-SHA256"
PRODUCER = "applypilot-release-evidence-producer-v2"
PRODUCER_VERSION = "2.0.0"
NON_RELEASE_PRODUCER = "applypilot-nonrelease-claim-signer-v1"
TEST_SUITE_POLICIES = {
    "runtime": {
        "purpose": "runtime-tests",
        "suiteIdentity": "applypilot-runtime-release-v2",
        "commands": (
            ("pytest", "python -m pytest -q", ("-m", "pytest", "-q")),
            ("ruff", "python -m ruff check .", ("-m", "ruff", "check", ".")),
        ),
    },
    "brain": {
        "purpose": "brain-tests",
        "suiteIdentity": "applypilot-brain-release-v2",
        "commands": (
            ("typecheck", "npm run typecheck", ("npm", "run", "typecheck")),
            ("tests", "npm test", ("npm", "test")),
        ),
    },
}
TEST_ENVIRONMENT_POLICY = {
    "schemaVersion": "applypilot-test-environment-policy-v1",
    "inheritedAllowlist": (
        "APPDATA",
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LOCALAPPDATA",
        "PATH",
        "PATHEXT",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "TZ",
        "USERPROFILE",
        "WINDIR",
    ),
    "fixed": {
        "GIT_CONFIG_GLOBAL": "<OS_DEVNULL>",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "NPM_CONFIG_AUDIT": "false",
        "NPM_CONFIG_FUND": "false",
        "NPM_CONFIG_USERCONFIG": "<OS_DEVNULL>",
        "PYTHONNOUSERSITE": "1",
        "PYTHONUTF8": "1",
    },
    "rejectedExact": (
        "BABEL_ENV",
        "GREP",
        "JEST_SHARD",
        "MOCHA_GREP",
        "NODE_ENV",
        "NODE_OPTIONS",
        "NODE_PATH",
        "ONLY",
        "PYTHONBREAKPOINT",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PYTHONWARNINGS",
        "PYTEST_ADDOPTS",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "PYTEST_PLUGINS",
        "SKIP",
        "TEST",
        "TEST_FILTER",
        "TEST_GREP",
        "TEST_NAME_PATTERN",
        "TEST_PATH_PATTERN",
        "TEST_PATTERN",
        "TESTS",
        "VITEST",
        "VITEST_RELATED",
        "VITEST_SHARD",
    ),
    "rejectedPrefixes": ("NPM_CONFIG_",),
}
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_PURPOSE_ENV = {
    "runtime-tests": "APPLYPILOT_RUNTIME_TEST_ATTESTATION",
    "brain-tests": "APPLYPILOT_BRAIN_TEST_ATTESTATION",
    "railway-topology": "APPLYPILOT_RAILWAY_TOPOLOGY_ATTESTATION",
    "nonrelease-claims": "APPLYPILOT_NONRELEASE_ATTESTATION",
}


def nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_release_binding(release_id: Any, release_nonce: Any) -> tuple[str, str]:
    if not nonempty_string(release_id):
        raise RuntimeError("release ID must be a non-empty string")
    if not isinstance(release_nonce, str) or _NONCE_RE.fullmatch(release_nonce) is None:
        raise RuntimeError("release nonce must contain 32-128 URL-safe characters")
    return release_id, release_nonce


def reject_json_constant(value: str) -> None:
    raise ValueError(f"non-standard JSON constant is not permitted: {value}")


def strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key is not permitted: {key}")
        result[key] = value
    return result


def strict_json_loads(content: bytes, label: str) -> Any:
    try:
        return json.loads(
            content.decode("utf-8-sig"),
            object_pairs_hook=strict_json_object,
            parse_constant=reject_json_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not valid UTF-8 JSON") from exc
    except ValueError as exc:
        raise RuntimeError(f"{label} is invalid: {exc}") from exc


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def canonical_receipt_payload(receipt: dict[str, Any]) -> bytes:
    return canonical_json({key: value for key, value in receipt.items() if key != "authentication"})


def purpose_key(purpose: str) -> tuple[bytes, str]:
    try:
        prefix = _PURPOSE_ENV[purpose]
    except KeyError as exc:
        raise RuntimeError(f"unsupported attestation purpose: {purpose}") from exc
    encoded_key = os.environ.get(f"{prefix}_KEY_B64")
    key_id = os.environ.get(f"{prefix}_KEY_ID")
    if not nonempty_string(encoded_key) or not nonempty_string(key_id):
        raise RuntimeError(f"{purpose} attestation key and key ID environment variables are required")
    try:
        key = base64.b64decode(encoded_key, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"{purpose} attestation key must be valid base64") from exc
    if len(key) < 32:
        raise RuntimeError(f"{purpose} attestation key must decode to at least 32 bytes")
    return key, key_id


def assert_separated_keys(configurations: dict[str, tuple[bytes, str]]) -> None:
    key_ids = [configuration[1] for configuration in configurations.values()]
    key_fingerprints = [hashlib.sha256(configuration[0]).digest() for configuration in configurations.values()]
    if len(key_ids) != len(set(key_ids)) or len(key_fingerprints) != len(set(key_fingerprints)):
        raise RuntimeError("release receipt purposes must use distinct keys and key IDs")


def sign_receipt(receipt: dict[str, Any], purpose: str) -> dict[str, Any]:
    key, key_id = purpose_key(purpose)
    signature = base64.b64encode(hmac.digest(key, canonical_receipt_payload(receipt), hashlib.sha256)).decode("ascii")
    return {
        **receipt,
        "authentication": {"algorithm": ALGORITHM, "keyId": key_id, "signature": signature},
    }


def verify_receipt(
    receipt: dict[str, Any],
    *,
    key: bytes,
    expected_key_id: str,
    label: str,
) -> str:
    authentication = receipt.get("authentication")
    if not isinstance(authentication, dict) or set(authentication) != {"algorithm", "keyId", "signature"}:
        raise RuntimeError(f"{label} must contain exactly one authentication object")
    if authentication.get("algorithm") != ALGORITHM:
        raise RuntimeError(f"{label} authentication algorithm is unsupported")
    if authentication.get("keyId") != expected_key_id:
        raise RuntimeError(f"{label} authentication key ID does not match the expected purpose key")
    signature = authentication.get("signature")
    if not nonempty_string(signature):
        raise RuntimeError(f"{label} authentication signature is missing")
    try:
        supplied = base64.b64decode(signature, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError(f"{label} authentication signature is not valid base64") from exc
    expected = hmac.digest(key, canonical_receipt_payload(receipt), hashlib.sha256)
    if not hmac.compare_digest(supplied, expected):
        raise RuntimeError(f"{label} authentication signature is invalid")
    return expected_key_id


def file_identity(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (stat_result.st_dev, stat_result.st_ino, stat_result.st_size, stat_result.st_mtime_ns)


def stable_read_bytes(path: Path, label: str) -> bytes:
    before = path.stat()
    first = path.read_bytes()
    middle = path.stat()
    second = path.read_bytes()
    after = path.stat()
    if len({file_identity(item) for item in (before, middle, after)}) != 1 or first != second:
        raise RuntimeError(f"{label} changed while reading: {path}")
    return second


def reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            raise RuntimeError(f"path must not contain symlinks: {path}")


def fsync_directory(directory: Path) -> None:
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        if os.name == "posix":
            raise
        return
    try:
        os.fsync(descriptor)
    except OSError:
        if os.name == "posix":
            raise
    finally:
        os.close(descriptor)


def atomic_write_no_overwrite(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Durably publish via a sibling temporary file and an atomic hard-link."""
    reject_symlink_components(path)
    if not path.parent.is_dir():
        raise RuntimeError(f"output parent directory does not exist: {path.parent}")
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")
    descriptor: int | None = None
    linked = False
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        if os.name == "posix":
            os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        linked = True
        fsync_directory(path.parent)
    except BaseException:
        if linked:
            path.unlink(missing_ok=True)
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
        fsync_directory(path.parent)


def regular_file(path: Path, label: str) -> Path:
    reject_symlink_components(path)
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} must be a regular file: {path}")
    return path
