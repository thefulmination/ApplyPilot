"""Shared fail-closed primitives for ApplyPilot release evidence."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import math
import os
import re
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


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
        "GIT_NO_REPLACE_OBJECTS": "1",
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
EXECUTABLE_TRUST_POLICY = "protected-handle-pre-post-exact-path-sha256-v2"
_EXECUTABLE_APPROVAL_ENV = {
    "runtime-python": "APPLYPILOT_RUNTIME_TEST_PYTHON",
    "brain-node": "APPLYPILOT_BRAIN_TEST_NODE",
    "brain-npm-cli": "APPLYPILOT_BRAIN_TEST_NPM_CLI",
    "railway-cli": "APPLYPILOT_RAILWAY_CLI",
    "release-git": "APPLYPILOT_RELEASE_GIT",
}
_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
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


def strict_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"non-finite JSON number is not permitted: {value}")
    return parsed


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
            parse_float=strict_json_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not valid UTF-8 JSON") from exc
    except ValueError as exc:
        raise RuntimeError(f"{label} is invalid: {exc}") from exc


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")


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


def _stable_file_sha256(path: Path, label: str) -> str:
    before = path.stat()
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    after = path.stat()
    if file_identity(before) != file_identity(after):
        raise RuntimeError(f"{label} changed while hashing: {path}")
    return digest.hexdigest()


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_path_has_reparse_component(path: Path) -> bool:
    if not _is_windows():
        return False
    attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    absolute = Path(os.path.abspath(path))
    for component in (*reversed(absolute.parents), absolute):
        try:
            metadata = component.lstat()
        except FileNotFoundError:
            continue
        if getattr(metadata, "st_file_attributes", 0) & attribute:
            return True
    return False


def observation_environment() -> tuple[dict[str, str], dict[str, str]]:
    ambient = {name.upper(): value for name, value in os.environ.items()}
    inherited = {
        name: ambient[name]
        for name in TEST_ENVIRONMENT_POLICY["inheritedAllowlist"]
        if name in ambient
    }
    if "PATH" in inherited:
        path_entries: list[str] = []
        seen: set[str] = set()
        for entry in inherited["PATH"].split(os.pathsep):
            if not entry or not Path(entry).is_absolute():
                continue
            normalized = os.path.normcase(os.path.normpath(entry))
            if normalized not in seen:
                seen.add(normalized)
                path_entries.append(entry)
        inherited["PATH"] = os.pathsep.join(path_entries)
    fixed = {
        name: os.devnull if value == "<OS_DEVNULL>" else value
        for name, value in TEST_ENVIRONMENT_POLICY["fixed"].items()
    }
    child = {**inherited, **fixed}
    value_hashes = {name: hashlib.sha256(value.encode("utf-8")).hexdigest() for name, value in sorted(inherited.items())}
    return child, value_hashes


def trusted_executable(purpose: str) -> dict[str, str]:
    try:
        prefix = _EXECUTABLE_APPROVAL_ENV[purpose]
    except KeyError as exc:
        raise RuntimeError(f"unsupported executable trust purpose: {purpose}") from exc
    configured_path = os.environ.get(f"{prefix}_PATH")
    configured_sha256 = os.environ.get(f"{prefix}_SHA256")
    if not nonempty_string(configured_path) or not isinstance(configured_sha256, str):
        raise RuntimeError(f"{purpose} approved executable path and SHA-256 are required")
    if _SHA256_RE.fullmatch(configured_sha256) is None:
        raise RuntimeError(f"{purpose} approved executable SHA-256 must be 64 lowercase hex characters")
    path = Path(configured_path)
    if not path.is_absolute():
        raise RuntimeError(f"{purpose} approved executable path must be absolute")
    path = Path(os.path.abspath(path))
    path = regular_file(path, f"{purpose} approved executable")
    actual_sha256 = _stable_file_sha256(path, f"{purpose} approved executable")
    if not hmac.compare_digest(actual_sha256, configured_sha256):
        raise RuntimeError(
            f"{purpose} executable does not match its approved SHA-256: {path}; "
            f"expected {configured_sha256}, got {actual_sha256}"
        )
    return {
        "path": str(path),
        "sha256": actual_sha256,
        "purpose": purpose,
        "trustPolicy": EXECUTABLE_TRUST_POLICY,
    }


def _hash_open_file(handle: Any) -> str:
    handle.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
        digest.update(chunk)
    handle.seek(0)
    return digest.hexdigest()


@contextmanager
def protected_executable_execution(executable: dict[str, str]) -> Iterator[dict[str, Any]]:
    """Hold and verify the approved executable across process creation and completion.

    POSIX executes the already-open descriptor. Windows holds a no-write/no-delete
    CreateFile handle while CreateProcess opens the approved path for execution.
    Both platforms revalidate the path and approved digest before releasing the handle.
    """
    if trusted_executable(executable.get("purpose", "")) != executable:
        raise RuntimeError("approved executable identity changed before protected execution")
    path = Path(executable["path"])
    handle: Any
    execution_path = str(path)
    pass_fds: tuple[int, ...] = ()
    if os.name == "nt":
        import _winapi
        import msvcrt

        windows_handle = _winapi.CreateFile(
            str(path),
            0x80000000,  # GENERIC_READ
            0x00000001,  # FILE_SHARE_READ: deny replacement, deletion, and writes
            0,
            3,  # OPEN_EXISTING
            0x00000080,  # FILE_ATTRIBUTE_NORMAL
            0,
        )
        descriptor = msvcrt.open_osfhandle(windows_handle, os.O_RDONLY)
        handle = os.fdopen(descriptor, "rb")
    elif os.name == "posix":
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        handle = os.fdopen(descriptor, "rb", closefd=False)
        descriptor_root = Path("/proc/self/fd")
        if not descriptor_root.is_dir():
            descriptor_root = Path("/dev/fd")
        if not descriptor_root.is_dir():
            os.close(descriptor)
            raise RuntimeError("platform lacks an executable descriptor path for protected execution")
        execution_path = str(descriptor_root / str(descriptor))
        pass_fds = (descriptor,)
    else:
        raise RuntimeError("platform lacks a protected executable execution boundary")
    try:
        opened_stat = os.fstat(handle.fileno())
        path_stat = path.stat()
        if (opened_stat.st_dev, opened_stat.st_ino) != (path_stat.st_dev, path_stat.st_ino):
            raise RuntimeError("approved executable path changed while opening protected execution")
        if not hmac.compare_digest(_hash_open_file(handle), executable["sha256"]):
            raise RuntimeError("approved executable handle does not match its recorded SHA-256")
        yield {"path": execution_path, "passFds": pass_fds}
        final_path_stat = path.stat()
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            final_path_stat.st_dev,
            final_path_stat.st_ino,
        ) or trusted_executable(executable["purpose"]) != executable:
            raise RuntimeError(
                f"approved {executable['purpose']} executable changed during protected execution"
            )
    finally:
        handle.close()
        if os.name == "posix":
            os.close(descriptor)


def reject_symlink_components(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    if _windows_path_has_reparse_component(absolute):
        raise RuntimeError(f"path must not contain Windows reparse points or junctions: {path}")
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            raise RuntimeError(f"path must not contain symlinks: {path}")


def _posix_parent_descriptor(path: Path) -> tuple[int, str]:
    required = (os.open, os.link, os.unlink)
    if os.name != "posix" or any(function not in os.supports_dir_fd for function in required):
        raise RuntimeError("platform lacks descriptor-relative release-evidence publication")
    absolute = Path(os.path.abspath(path))
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise RuntimeError("platform lacks no-follow directory opens for release-evidence publication")
    descriptor = os.open(absolute.anchor, directory_flags)
    try:
        for component in absolute.parent.parts[1:]:
            child = os.open(component, directory_flags | nofollow, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, absolute.name


def _atomic_write_posix(path: Path, content: bytes, mode: int) -> None:
    parent_descriptor, target_name = _posix_parent_descriptor(path)
    temporary_name = f".{target_name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    temporary_descriptor: int | None = None
    linked = False
    try:
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0),
            mode,
            dir_fd=parent_descriptor,
        )
        os.fchmod(temporary_descriptor, mode)
        with os.fdopen(temporary_descriptor, "wb") as handle:
            temporary_descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(
            temporary_name,
            target_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        linked = True
        os.fsync(parent_descriptor)
    except BaseException:
        if linked:
            os.unlink(target_name, dir_fd=parent_descriptor)
        raise
    finally:
        if temporary_descriptor is not None:
            os.close(temporary_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        os.close(parent_descriptor)


def _windows_parent_handle(path: Path) -> tuple[int, str]:
    import ctypes
    from ctypes import wintypes

    reject_symlink_components(path.parent)
    absolute_parent = Path(os.path.abspath(path.parent))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    handle = create_file(
        str(absolute_parent),
        0x0001 | 0x0020 | 0x00100000,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        get_final_path = kernel32.GetFinalPathNameByHandleW
        get_final_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
        get_final_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_final_path(handle, buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise ctypes.WinError(ctypes.get_last_error())
        observed = buffer.value
        if observed.startswith("\\\\?\\UNC\\"):
            observed = "\\\\" + observed[8:]
        elif observed.startswith("\\\\?\\"):
            observed = observed[4:]
        if os.path.normcase(os.path.abspath(observed)) != os.path.normcase(str(absolute_parent)):
            raise RuntimeError("output parent resolved through an alias or reparse point")
        return int(handle), path.name
    except BaseException:
        kernel32.CloseHandle(handle)
        raise


def _windows_open_relative(parent_handle: int, name: str, disposition: int) -> int:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = (("Length", wintypes.USHORT), ("MaximumLength", wintypes.USHORT), ("Buffer", wintypes.LPWSTR))

    class ObjectAttributes(ctypes.Structure):
        _fields_ = (
            ("Length", wintypes.ULONG),
            ("RootDirectory", wintypes.HANDLE),
            ("ObjectName", ctypes.POINTER(UnicodeString)),
            ("Attributes", wintypes.ULONG),
            ("SecurityDescriptor", wintypes.LPVOID),
            ("SecurityQualityOfService", wintypes.LPVOID),
        )

    class IoStatusBlock(ctypes.Structure):
        _fields_ = (("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t))

    encoded_name = name.encode("utf-16-le")
    name_buffer = ctypes.create_unicode_buffer(name)
    unicode_name = UnicodeString(
        len(encoded_name),
        len(encoded_name) + 2,
        ctypes.cast(name_buffer, wintypes.LPWSTR),
    )
    attributes = ObjectAttributes(
        ctypes.sizeof(ObjectAttributes),
        parent_handle,
        ctypes.pointer(unicode_name),
        0x40,
        None,
        None,
    )
    io_status = IoStatusBlock()
    native_handle = wintypes.HANDLE()
    nt_create_file = ctypes.WinDLL("ntdll").NtCreateFile
    nt_create_file.restype = ctypes.c_long
    status = nt_create_file(
        ctypes.byref(native_handle),
        0x0002 | 0x0100 | 0x00010000 | 0x00100000,
        ctypes.byref(attributes),
        ctypes.byref(io_status),
        None,
        0x80,
        0x1,
        disposition,
        0x20 | 0x40 | 0x00200000,
        None,
        0,
    )
    if status < 0:
        if ctypes.c_ulong(status).value == 0xC0000035:
            raise FileExistsError(name)
        if ctypes.c_ulong(status).value in {0xC0000034, 0xC000000F}:
            raise FileNotFoundError(name)
        raise OSError(f"NtCreateFile failed for release evidence with NTSTATUS 0x{ctypes.c_ulong(status).value:08x}")
    return msvcrt.open_osfhandle(int(native_handle.value), os.O_WRONLY | os.O_BINARY)


def _windows_link_relative(file_descriptor: int, parent_handle: int, target_name: str) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileLinkInfo(ctypes.Structure):
        _fields_ = (
            ("ReplaceIfExists", wintypes.BOOLEAN),
            ("RootDirectory", wintypes.HANDLE),
            ("FileNameLength", wintypes.DWORD),
            ("FileName", wintypes.WCHAR * 1),
        )

    class IoStatusBlock(ctypes.Structure):
        _fields_ = (("Status", ctypes.c_void_p), ("Information", ctypes.c_size_t))

    encoded_name = target_name.encode("utf-16-le")
    offset = FileLinkInfo.FileName.offset
    buffer = ctypes.create_string_buffer(offset + len(encoded_name))
    info = FileLinkInfo.from_buffer(buffer)
    info.ReplaceIfExists = False
    info.RootDirectory = parent_handle
    info.FileNameLength = len(encoded_name)
    ctypes.memmove(ctypes.addressof(buffer) + offset, encoded_name, len(encoded_name))
    file_handle = msvcrt.get_osfhandle(file_descriptor)
    io_status = IoStatusBlock()
    set_information = ctypes.WinDLL("ntdll").NtSetInformationFile
    set_information.restype = ctypes.c_long
    status = set_information(file_handle, ctypes.byref(io_status), buffer, len(buffer), 11)
    if status < 0:
        unsigned_status = ctypes.c_ulong(status).value
        if unsigned_status in {0xC0000035, 0xC0000043}:
            raise FileExistsError(target_name)
        raise OSError(
            f"NtSetInformationFile failed for release evidence with NTSTATUS 0x{unsigned_status:08x}"
        )


def _windows_mark_delete(file_descriptor: int) -> None:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileDispositionInfo(ctypes.Structure):
        _fields_ = (("DeleteFile", wintypes.BOOLEAN),)

    disposition = FileDispositionInfo(True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    set_information.restype = wintypes.BOOL
    file_handle = msvcrt.get_osfhandle(file_descriptor)
    if not set_information(file_handle, 4, ctypes.byref(disposition), ctypes.sizeof(disposition)):
        raise ctypes.WinError(ctypes.get_last_error())


def _atomic_write_windows(path: Path, content: bytes, mode: int) -> None:
    del mode
    import ctypes

    parent_handle, target_name = _windows_parent_handle(path)
    temporary_name = f".{target_name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    descriptor: int | None = None
    try:
        descriptor = _windows_open_relative(parent_handle, temporary_name, 2)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _windows_link_relative(descriptor, parent_handle, target_name)
    finally:
        if descriptor is not None:
            try:
                _windows_mark_delete(descriptor)
            finally:
                os.close(descriptor)
        ctypes.WinDLL("kernel32").CloseHandle(parent_handle)


def remove_published_file(path: Path) -> None:
    """Remove a published file relative to an anchored, no-reparse parent."""
    path = Path(path)
    if _is_windows():
        parent_handle, target_name = _windows_parent_handle(path)
        descriptor: int | None = None
        try:
            descriptor = _windows_open_relative(parent_handle, target_name, 1)
            _windows_mark_delete(descriptor)
        finally:
            if descriptor is not None:
                os.close(descriptor)
            import ctypes

            ctypes.WinDLL("kernel32").CloseHandle(parent_handle)
        return
    parent_descriptor, target_name = _posix_parent_descriptor(path)
    try:
        os.unlink(target_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)


def atomic_write_no_overwrite(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    """Publish complete evidence relative to an anchored parent without overwrite."""
    path = Path(path)
    if not path.name or path.name in {".", ".."}:
        raise RuntimeError("release-evidence output requires a file name")
    if _is_windows():
        if _windows_path_has_reparse_component(path.parent):
            raise RuntimeError(f"path must not contain Windows reparse points or junctions: {path}")
        _atomic_write_windows(path, content, mode)
        return
    _atomic_write_posix(path, content, mode)


def regular_file(path: Path, label: str) -> Path:
    reject_symlink_components(path)
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} does not exist: {path}") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} must be a regular file: {path}")
    return path
