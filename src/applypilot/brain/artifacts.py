"""Coordinator-neutral immutable artifact contracts and a local backend."""

from __future__ import annotations

import base64
import errno
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import stat
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import BinaryIO, Callable, Iterator, Protocol, runtime_checkable

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl


_OPAQUE_KEY = re.compile(r"[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_LOCAL_PROVIDER_VERSION = "local-v1"
_CHAIN_FORMAT_VERSION = 1
_CHAIN_GENESIS = "0" * 64
_ROOT_BINDING_FORMAT_VERSION = 1
_WINDOWS_REPARSE_POINT = 0x400
_ROOT_LOCKS: dict[str, threading.RLock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


if os.name == "nt":
    class _SecurityAttributes(ctypes.Structure):
        _fields_ = (
            ("nLength", wintypes.DWORD),
            ("lpSecurityDescriptor", wintypes.LPVOID),
            ("bInheritHandle", wintypes.BOOL),
        )


    class _SidAndAttributes(ctypes.Structure):
        _fields_ = (("Sid", wintypes.LPVOID), ("Attributes", wintypes.DWORD))


    class _TokenUser(ctypes.Structure):
        _fields_ = (("User", _SidAndAttributes),)


    class _AclSizeInformation(ctypes.Structure):
        _fields_ = (
            ("AceCount", wintypes.DWORD),
            ("AclBytesInUse", wintypes.DWORD),
            ("AclBytesFree", wintypes.DWORD),
        )


    class _AceHeader(ctypes.Structure):
        _fields_ = (
            ("AceType", ctypes.c_ubyte),
            ("AceFlags", ctypes.c_ubyte),
            ("AceSize", wintypes.WORD),
        )


    class _AccessAllowedAce(ctypes.Structure):
        _fields_ = (
            ("Header", _AceHeader),
            ("Mask", wintypes.DWORD),
            ("SidStart", wintypes.DWORD),
        )


class ArtifactError(RuntimeError):
    """Base class for artifact storage failures."""


class ArtifactIntegrityError(ArtifactError):
    """Artifact bytes or metadata do not match a coordinator receipt."""


class ArtifactConflictError(ArtifactError):
    """A durable request id was reused with different immutable input."""


class ArtifactLockTimeout(ArtifactError):
    """A cross-process claim could not be acquired before its deadline."""


class ArtifactState(str, Enum):
    PENDING = "pending"
    OBJECT_VERIFIED = "object_verified"
    COMMITTED = "committed"


@dataclass(frozen=True, slots=True)
class StoreCapabilities:
    provider_version: str
    directory_fsync_supported: bool
    durability_state: str
    coordinator_atomic: bool
    anchor_path_separated: bool
    anchor_same_volume: bool


@dataclass(frozen=True, slots=True)
class ArtifactWriteRequest:
    """Opaque idempotency identity and immutable storage requirements."""

    request_id: str
    media_type: str = "application/octet-stream"
    schema_version: int = 1
    encryption_algorithm: str = "none"
    encryption_provider_version: str | None = None
    provider_version: str = _LOCAL_PROVIDER_VERSION
    durability_state: str = "pending"


@dataclass(frozen=True, slots=True)
class PendingGeneration:
    generation_id: str
    object_key: str
    request_id: str
    content_sha256: str
    byte_count: int
    media_type: str
    schema_version: int
    created_at: str
    encryption_algorithm: str
    encryption_provider_version: str | None
    provider_version: str
    durability_state: str
    state: ArtifactState = ArtifactState.PENDING


@dataclass(frozen=True, slots=True)
class VerificationReceipt:
    generation_id: str
    object_key: str
    request_id: str
    content_sha256: str
    byte_count: int
    media_type: str
    schema_version: int
    created_at: str
    encryption_algorithm: str
    encryption_provider_version: str | None
    provider_version: str
    durability_state: str
    verified_at: str
    state: ArtifactState = ArtifactState.OBJECT_VERIFIED


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    generation_id: str
    object_key: str
    request_id: str
    content_sha256: str
    byte_count: int
    media_type: str
    schema_version: int
    created_at: str
    committed_at: str
    encryption_algorithm: str
    encryption_provider_version: str | None
    provider_version: str
    durability_state: str
    coordinator_atomic: bool = False
    state: ArtifactState = ArtifactState.COMMITTED


@runtime_checkable
class ArtifactStore(Protocol):
    """Storage half of a future coordinator-owned artifact transaction."""

    @property
    def capabilities(self) -> StoreCapabilities: ...

    def stage(
        self, source: BinaryIO | str | os.PathLike[str], request: ArtifactWriteRequest
    ) -> PendingGeneration: ...

    def verify_pending(self, pending: PendingGeneration) -> VerificationReceipt: ...

    def commit(
        self,
        pending: PendingGeneration,
        receipt: VerificationReceipt | None = None,
    ) -> ArtifactRef: ...

    def open_verified(self, artifact: ArtifactRef) -> BinaryIO: ...

    def verify_committed(self, artifact: ArtifactRef) -> VerificationReceipt: ...

    def reconcile_pending(
        self, stale_after: timedelta | float = timedelta(hours=1)
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class _SourceSnapshot:
    device: int
    inode: int
    byte_count: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class _RequestRecords:
    pending: PendingGeneration | None = None
    verified: VerificationReceipt | None = None
    intent: ArtifactRef | None = None
    committed: ArtifactRef | None = None


@dataclass(frozen=True, slots=True)
class _CoordinatorKeyState:
    secret: bytes
    key_id: str
    sequence: int
    chain_head: str
    root_binding_hash: str | None


@dataclass(frozen=True, slots=True)
class _ClaimOwnership:
    acquired: bool
    created: bool


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_json(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def _validate_generation_id(value: str) -> None:
    if _OPAQUE_KEY.fullmatch(value) is None:
        raise ValueError("generation identifiers must be 32-character opaque keys")


def _validate_object_key(value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError("object keys must be lowercase SHA-256 content addresses")


def _strict_fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _probe_directory_fsync(path: Path) -> bool:
    if os.name == "nt":
        return False
    try:
        _strict_fsync_directory(path)
    except OSError:
        return False
    return True


def _lexical_absolute(path: str | os.PathLike[str]) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_link_components(path: Path, *, require_single_link_file: bool = False) -> None:
    absolute = _lexical_absolute(path)
    components = [absolute, *absolute.parents]
    for component in reversed(components):
        try:
            status = os.lstat(component)
        except FileNotFoundError:
            continue
        attributes = getattr(status, "st_file_attributes", 0)
        if stat.S_ISLNK(status.st_mode) or attributes & _WINDOWS_REPARSE_POINT:
            raise ArtifactIntegrityError(f"storage path contains a symlink or reparse point: {component}")
        if (
            require_single_link_file
            and component == absolute
            and stat.S_ISREG(status.st_mode)
            and status.st_nlink != 1
        ):
            raise ArtifactIntegrityError(f"storage file has an unsafe hardlink count: {component}")


def _windows_current_user_sid() -> str:
    get_current_process = ctypes.windll.kernel32.GetCurrentProcess
    get_current_process.argtypes = ()
    get_current_process.restype = wintypes.HANDLE
    open_process_token = ctypes.windll.advapi32.OpenProcessToken
    open_process_token.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    open_process_token.restype = wintypes.BOOL
    get_token_information = ctypes.windll.advapi32.GetTokenInformation
    get_token_information.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    get_token_information.restype = wintypes.BOOL
    convert_sid = ctypes.windll.advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = (wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR))
    convert_sid.restype = wintypes.BOOL
    token = wintypes.HANDLE()
    if not open_process_token(
        get_current_process(),
        0x0008,
        ctypes.byref(token),
    ):
        raise ctypes.WinError()
    try:
        required = wintypes.DWORD()
        get_token_information(token, 1, None, 0, ctypes.byref(required))
        buffer = ctypes.create_string_buffer(required.value)
        if not get_token_information(
            token,
            1,
            buffer,
            required,
            ctypes.byref(required),
        ):
            raise ctypes.WinError()
        user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        sid_text = wintypes.LPWSTR()
        if not convert_sid(user.User.Sid, ctypes.byref(sid_text)):
            raise ctypes.WinError()
        try:
            return sid_text.value
        finally:
            ctypes.windll.kernel32.LocalFree(sid_text)
    finally:
        ctypes.windll.kernel32.CloseHandle(token)


def _windows_security_descriptor(*, directory: bool) -> wintypes.LPVOID:
    inheritance = "OICI" if directory else ""
    sddl = (
        f"D:P(A;{inheritance};FA;;;{_windows_current_user_sid()})"
        f"(A;{inheritance};FA;;;SY)(A;{inheritance};FA;;;BA)"
    )
    descriptor = wintypes.LPVOID()
    if not ctypes.windll.advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        sddl,
        1,
        ctypes.byref(descriptor),
        None,
    ):
        raise ctypes.WinError()
    return descriptor


def _windows_allowed_sid_pointers() -> list[wintypes.LPVOID]:
    pointers: list[wintypes.LPVOID] = []
    for sid_text in (
        _windows_current_user_sid(),
        "S-1-5-18",
        "S-1-5-32-544",
    ):
        pointer = wintypes.LPVOID()
        if not ctypes.windll.advapi32.ConvertStringSidToSidW(sid_text, ctypes.byref(pointer)):
            for allocated in pointers:
                ctypes.windll.kernel32.LocalFree(allocated)
            raise ctypes.WinError()
        pointers.append(pointer)
    return pointers


def _windows_verify_restricted_dacl(
    security_descriptor: wintypes.LPVOID,
    dacl: wintypes.LPVOID,
    *,
    directory: bool,
) -> None:
    control = wintypes.WORD()
    revision = wintypes.DWORD()
    if not ctypes.windll.advapi32.GetSecurityDescriptorControl(
        security_descriptor,
        ctypes.byref(control),
        ctypes.byref(revision),
    ):
        raise ctypes.WinError()
    if not control.value & 0x1000:
        raise ArtifactIntegrityError("Windows storage DACL is not protected")
    information = _AclSizeInformation()
    if not ctypes.windll.advapi32.GetAclInformation(
        dacl,
        ctypes.byref(information),
        ctypes.sizeof(information),
        2,
    ):
        raise ctypes.WinError()
    if information.AceCount != 3:
        raise ArtifactIntegrityError("Windows storage DACL contains unexpected ACEs")

    allowed = _windows_allowed_sid_pointers()
    seen: set[int] = set()
    try:
        expected_inheritance = 0x03 if directory else 0
        for index in range(information.AceCount):
            ace_pointer = wintypes.LPVOID()
            if not ctypes.windll.advapi32.GetAce(dacl, index, ctypes.byref(ace_pointer)):
                raise ctypes.WinError()
            ace = ctypes.cast(ace_pointer, ctypes.POINTER(_AccessAllowedAce)).contents
            if (
                ace.Header.AceType != 0
                or ace.Header.AceFlags != expected_inheritance
                or ace.Mask != 0x1F01FF
            ):
                raise ArtifactIntegrityError("Windows storage DACL contains an unsafe ACE")
            sid_pointer = ctypes.byref(ace, _AccessAllowedAce.SidStart.offset)
            matches = [
                allowed_index
                for allowed_index, allowed_sid in enumerate(allowed)
                if ctypes.windll.advapi32.EqualSid(sid_pointer, allowed_sid)
            ]
            if len(matches) != 1 or matches[0] in seen:
                raise ArtifactIntegrityError("Windows storage DACL grants an unexpected identity")
            seen.add(matches[0])
        if seen != {0, 1, 2}:
            raise ArtifactIntegrityError("Windows storage DACL omits a required identity")
    finally:
        for pointer in allowed:
            ctypes.windll.kernel32.LocalFree(pointer)


def _windows_set_and_verify_restricted_dacl(path: Path, *, directory: bool) -> None:
    descriptor = _windows_security_descriptor(directory=directory)
    dacl = wintypes.LPVOID()
    present = wintypes.BOOL()
    defaulted = wintypes.BOOL()
    try:
        if not ctypes.windll.advapi32.GetSecurityDescriptorDacl(
            descriptor,
            ctypes.byref(present),
            ctypes.byref(dacl),
            ctypes.byref(defaulted),
        ) or not present:
            raise ArtifactIntegrityError("restricted Windows security descriptor has no DACL")
        result = ctypes.windll.advapi32.SetNamedSecurityInfoW(
            str(path),
            1,
            0x80000004,
            None,
            None,
            dacl,
            None,
        )
        if result != 0:
            raise ctypes.WinError(result)
    finally:
        ctypes.windll.kernel32.LocalFree(descriptor)

    owner = wintypes.LPVOID()
    group = wintypes.LPVOID()
    actual_dacl = wintypes.LPVOID()
    sacl = wintypes.LPVOID()
    actual_descriptor = wintypes.LPVOID()
    result = ctypes.windll.advapi32.GetNamedSecurityInfoW(
        str(path),
        1,
        0x00000004,
        ctypes.byref(owner),
        ctypes.byref(group),
        ctypes.byref(actual_dacl),
        ctypes.byref(sacl),
        ctypes.byref(actual_descriptor),
    )
    if result != 0:
        raise ctypes.WinError(result)
    try:
        _windows_verify_restricted_dacl(
            actual_descriptor,
            actual_dacl,
            directory=directory,
        )
    finally:
        ctypes.windll.kernel32.LocalFree(actual_descriptor)


def _windows_create_restricted_file(path: Path) -> int:
    descriptor = _windows_security_descriptor(directory=False)
    attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes),
        descriptor,
        False,
    )
    handle = None
    try:
        create_file = ctypes.windll.kernel32.CreateFileW
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x40020000,
            0,
            ctypes.byref(attributes),
            1,
            0x00200080,
            None,
        )
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError()

        owner = wintypes.LPVOID()
        group = wintypes.LPVOID()
        dacl = wintypes.LPVOID()
        sacl = wintypes.LPVOID()
        actual_descriptor = wintypes.LPVOID()
        result = ctypes.windll.advapi32.GetSecurityInfo(
            handle,
            1,
            0x00000004,
            ctypes.byref(owner),
            ctypes.byref(group),
            ctypes.byref(dacl),
            ctypes.byref(sacl),
            ctypes.byref(actual_descriptor),
        )
        if result != 0:
            raise ctypes.WinError(result)
        try:
            _windows_verify_restricted_dacl(actual_descriptor, dacl, directory=False)
        finally:
            ctypes.windll.kernel32.LocalFree(actual_descriptor)

        file_descriptor = msvcrt.open_osfhandle(handle, os.O_WRONLY | os.O_BINARY)
        handle = None
        return file_descriptor
    finally:
        if handle not in {None, wintypes.HANDLE(-1).value}:
            ctypes.windll.kernel32.CloseHandle(handle)
        ctypes.windll.kernel32.LocalFree(descriptor)


def _create_restricted_directory(path: Path) -> None:
    if path.exists():
        _reject_link_components(path)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        path.mkdir(mode=0o700)
        return

    security_descriptor = _windows_security_descriptor(directory=True)
    attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes),
        security_descriptor,
        False,
    )
    try:
        create = ctypes.windll.kernel32.CreateDirectoryW
        create.argtypes = (wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes))
        create.restype = wintypes.BOOL
        if not create(str(path), ctypes.byref(attributes)):
            error = ctypes.windll.kernel32.GetLastError()
            if error != 183:
                raise ctypes.WinError(error)
        _reject_link_components(path)
    finally:
        ctypes.windll.kernel32.LocalFree(security_descriptor)


def _thread_lock_for(root: Path) -> threading.RLock:
    key = os.path.normcase(str(root))
    with _ROOT_LOCKS_GUARD:
        return _ROOT_LOCKS.setdefault(key, threading.RLock())


def _try_lock_descriptor_once(descriptor: int) -> bool:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK, 13, 36}:
                return False
            raise
        return True
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _lock_descriptor(
    descriptor: int,
    *,
    blocking: bool,
    timeout_seconds: float = 30.0,
    retry_interval_seconds: float = 0.05,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    deadline: float | None = None,
) -> bool:
    if not blocking:
        return _try_lock_descriptor_once(descriptor)
    if deadline is None:
        deadline = monotonic() + timeout_seconds
    while not _try_lock_descriptor_once(descriptor):
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ArtifactLockTimeout("cross-process artifact lock timed out")
        sleeper(min(retry_interval_seconds, remaining))
    return True


def _unlock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


class _VerifiedSnapshot(io.BufferedIOBase):
    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def seekable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)

    def readinto(self, buffer) -> int:
        return self._stream.readinto(buffer)

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        return self._stream.seek(offset, whence)

    def tell(self) -> int:
        return self._stream.tell()

    def fileno(self) -> int:
        raise io.UnsupportedOperation("verified snapshots do not expose a mutable file descriptor")

    def close(self) -> None:
        if not self.closed:
            self._stream.close()
        super().close()


class LocalArtifactStore:
    """Non-authoritative local coordinator for migration and development.

    The restricted coordinator key and rollback anchor live outside the artifact
    root and detect rollback of that root while the anchor remains current. This
    does not protect against compromise of the same OS identity, administrator or
    root access, host compromise, or coordinated rollback of both roots. Postgres
    remains the future authority; this backend never claims coordinator atomicity.
    """

    CHUNK_SIZE = 1024 * 1024

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        anchor_root: str | os.PathLike[str] | None = None,
        fault_injector: Callable[[str], None] | None = None,
        lock_timeout_seconds: float = 30.0,
        lock_retry_interval_seconds: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.root = _lexical_absolute(root)
        configured_anchor = (
            Path(anchor_root)
            if anchor_root is not None
            else self.root.parent / f".{self.root.name}.artifact-anchor"
        )
        self.anchor_root = _lexical_absolute(configured_anchor)
        _reject_link_components(self.root)
        _reject_link_components(self.anchor_root)
        if (
            self.anchor_root == self.root
            or self.anchor_root.is_relative_to(self.root)
            or self.root.is_relative_to(self.anchor_root)
        ):
            raise ValueError("anchor_root must be path-separated from the artifact root")
        if lock_timeout_seconds <= 0 or lock_retry_interval_seconds <= 0:
            raise ValueError("artifact lock timeout and retry interval must be positive")
        self._lock_timeout_seconds = float(lock_timeout_seconds)
        self._lock_retry_interval_seconds = float(lock_retry_interval_seconds)
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._fault_injector = fault_injector
        _create_restricted_directory(self.root)
        _reject_link_components(self.root)
        self._restrict_directory_permissions(self.root)
        _reject_link_components(self.root)
        self._root_thread_lock = _thread_lock_for(self.root)
        self._root_coordination_path = self.root / ".artifact-root.lock"
        self._ensure_lock_file(self._root_coordination_path)
        self._binding_path = self.root / ".artifact-anchor-binding.json"
        self._pending_dir = self.root / "pending"
        self._committed_dir = self.root / "committed"
        self._quarantine_dir = self.root / "quarantine"
        self._registry_dir = self.root / "registry"
        deadline = self._new_deadline()
        with self._root_coordination(deadline=deadline):
            self._reject_mismatched_bound_anchor_before_creation()
            _create_restricted_directory(self.anchor_root)
            _reject_link_components(self.anchor_root)
            self._restrict_directory_permissions(self.anchor_root)
            _reject_link_components(self.anchor_root)
            self._anchor_same_volume = (
                os.stat(self.root).st_dev == os.stat(self.anchor_root).st_dev
            )
            self._directory_fsync_supported = _probe_directory_fsync(
                self.root
            ) and _probe_directory_fsync(self.anchor_root)
            self.capabilities = StoreCapabilities(
                provider_version=_LOCAL_PROVIDER_VERSION,
                directory_fsync_supported=self._directory_fsync_supported,
                durability_state="committed_unprotected",
                coordinator_atomic=False,
                anchor_path_separated=True,
                anchor_same_volume=self._anchor_same_volume,
            )
            self._thread_lock = _thread_lock_for(self.anchor_root)
            self._coordination_path = self.anchor_root / ".coordination.lock"
            self._ensure_lock_file(self._coordination_path)
            self._key_path = self.anchor_root / ".coordinator.key"
            with self._coordination(deadline=deadline):
                self._initialize_coordinator_key()
                self._initialize_or_verify_root_binding()
                for directory in (
                    self._pending_dir,
                    self._committed_dir,
                    self._quarantine_dir,
                    self._registry_dir,
                ):
                    directory.mkdir(parents=False, exist_ok=True, mode=0o700)
                    self._assert_storage_path(directory, expect_directory=True)
                self._verified_registry_chain()

    def stage(
        self, source: BinaryIO | str | os.PathLike[str], request: ArtifactWriteRequest
    ) -> PendingGeneration:
        self._validate_request(request)
        deadline = self._new_deadline()
        generation_id = secrets.token_hex(16)
        data_path = self._pending_path(generation_id, ".blob")
        metadata_path = self._pending_path(generation_id, ".json")
        active_path = self._pending_path(generation_id, ".active")
        source_path: Path | None = None
        before: _SourceSnapshot | None = None

        if isinstance(source, (str, os.PathLike)):
            source_path = Path(source)
            before = self._source_snapshot(source_path)
            stream: BinaryIO = source_path.open("rb")
            close_stream = True
        elif hasattr(source, "read"):
            stream = source
            close_stream = False
        else:
            raise TypeError("source must be a binary stream or filesystem path")

        try:
            # Caller-controlled I/O is isolated to this generation claim. No
            # global coordinator lock is held while source.read() executes.
            with self._generation_claim(active_path, deadline=deadline) as heartbeat:
                try:
                    digest, byte_count = self._write_stream_exclusive(data_path, stream, heartbeat)
                    if source_path is not None and before != self._source_snapshot(source_path):
                        raise ArtifactIntegrityError("source file changed during staging")
                    heartbeat()
                except Exception:
                    self._cleanup_files(data_path, active_path)
                    raise
                self._kill_point("after_pending_data")

            # Authenticated request publication uses the common global-then-
            # generation order shared by commit and reconciliation.
            with self._coordination(deadline=deadline):
                with self._generation_claim(
                    active_path,
                    remove_on_success=True,
                    deadline=deadline,
                ) as heartbeat:
                    records = self._request_records(request.request_id)
                    if records.pending is not None:
                        self._validate_idempotent_retry(records.pending, request, digest, byte_count)
                        self._cleanup_files(data_path, metadata_path)
                        return records.pending

                    pending = PendingGeneration(
                        generation_id=generation_id,
                        object_key=digest,
                        request_id=request.request_id,
                        content_sha256=digest,
                        byte_count=byte_count,
                        media_type=request.media_type,
                        schema_version=request.schema_version,
                        created_at=_utc_now(),
                        encryption_algorithm=request.encryption_algorithm,
                        encryption_provider_version=request.encryption_provider_version,
                        provider_version=request.provider_version,
                        durability_state="pending",
                    )
                    self._publish_metadata(metadata_path, self._pending_metadata(pending))
                    self._kill_point("after_pending_metadata")
                    self._publish_registry("pending", pending)
                    self._kill_point("after_pending_registry")
                return pending
        finally:
            if close_stream:
                stream.close()

    def verify_pending(self, pending: PendingGeneration) -> VerificationReceipt:
        self._validate_pending_handle(pending)
        deadline = self._new_deadline()
        active_path = self._pending_path(pending.generation_id, ".active")
        with self._coordination(deadline=deadline):
            with self._generation_claim(
                active_path,
                remove_on_exit=True,
                deadline=deadline,
            ):
                records = self._request_records(pending.request_id)
                trusted = records.pending
                if trusted is None or trusted != pending:
                    raise ArtifactIntegrityError("pending handle does not match the registry receipt")
                if records.verified is not None:
                    self._validate_verification_for_pending(trusted, records.verified)
                    return records.verified
                return self._verify_pending_locked(trusted)

    def commit(
        self,
        pending: PendingGeneration,
        receipt: VerificationReceipt | None = None,
    ) -> ArtifactRef:
        self._validate_pending_handle(pending)
        deadline = self._new_deadline()
        active_path = self._pending_path(pending.generation_id, ".active")
        with self._coordination(deadline=deadline):
            with self._generation_claim(
                active_path,
                remove_on_exit=True,
                deadline=deadline,
            ):
                self._kill_point("after_commit_claim")
                records = self._request_records(pending.request_id)
                trusted = records.pending
                if trusted is None or trusted != pending:
                    raise ArtifactIntegrityError("pending handle does not match the registry receipt")
                verified = records.verified or self._verify_pending_locked(trusted)
                if receipt is not None and receipt != verified:
                    raise ArtifactIntegrityError("verification receipt does not match the registry receipt")
                self._validate_verification_for_pending(trusted, verified)
                return self._complete_commit_locked(trusted, verified, records)

    def open_verified(self, artifact: ArtifactRef) -> BinaryIO:
        return self._open_verified_with_deadline(artifact, self._new_deadline())

    def _open_verified_with_deadline(
        self,
        artifact: ArtifactRef,
        deadline: float,
    ) -> BinaryIO:
        self._validate_artifact_handle(artifact)
        with self._coordination(deadline=deadline):
            records = self._request_records(artifact.request_id)
            if records.committed is None or records.committed != artifact:
                raise ArtifactIntegrityError("artifact reference does not match the registry receipt")
            metadata = self._read_canonical_metadata(self._committed_path(artifact.object_key, ".json"))
            if metadata != self._committed_metadata(artifact):
                raise ArtifactIntegrityError("committed sidecar does not match the registry receipt")
            source_path = self._committed_path(artifact.object_key, ".blob")
            source = os.fdopen(self._open_storage_file(source_path, os.O_RDONLY), "rb")

        snapshot = tempfile.TemporaryFile(mode="w+b")
        try:
            before = os.fstat(source.fileno())
            digest = hashlib.sha256()
            byte_count = 0
            while True:
                chunk = source.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                snapshot.write(chunk)
                digest.update(chunk)
                byte_count += len(chunk)
            after = os.fstat(source.fileno())
            if (
                before.st_size,
                before.st_mtime_ns,
                digest.hexdigest(),
                byte_count,
            ) != (
                after.st_size,
                after.st_mtime_ns,
                artifact.content_sha256,
                artifact.byte_count,
            ):
                raise ArtifactIntegrityError("committed object changed or failed verification")
            snapshot.flush()
            snapshot.seek(0)
            return _VerifiedSnapshot(snapshot)
        except Exception:
            snapshot.close()
            raise
        finally:
            source.close()

    def verify_committed(self, artifact: ArtifactRef) -> VerificationReceipt:
        deadline = self._new_deadline()
        with self._open_verified_with_deadline(artifact, deadline):
            pass
        with self._coordination(deadline=deadline):
            records = self._request_records(artifact.request_id)
            if records.verified is None:
                raise ArtifactIntegrityError("committed object has no object_verified receipt")
            return records.verified

    def reconcile_pending(
        self, stale_after: timedelta | float = timedelta(hours=1)
    ) -> tuple[str, ...]:
        seconds = stale_after.total_seconds() if isinstance(stale_after, timedelta) else float(stale_after)
        if seconds < 0:
            raise ValueError("stale_after must not be negative")
        cutoff = time.time() - seconds
        deadline = self._new_deadline()
        quarantined: list[str] = []
        with self._coordination(deadline=deadline):
            self._assert_storage_path(self._pending_dir, expect_directory=True)
            generation_ids: set[str] = set()
            for path in self._pending_dir.iterdir():
                self._assert_storage_path(path)
                if stat.S_ISREG(os.lstat(path).st_mode) and path.suffix in {
                    ".blob",
                    ".json",
                    ".active",
                }:
                    generation_ids.add(path.name.rsplit(".", 1)[0])
            for generation_id in sorted(generation_ids):
                try:
                    _validate_generation_id(generation_id)
                    active_path = self._pending_path(generation_id, ".active")
                    moved = False
                    remove_claim = False
                    quarantine_active = False
                    with self._try_generation_claim(active_path) as ownership:
                        if ownership.acquired:
                            records = self._generation_records(generation_id)
                            if records.committed is not None:
                                if records.pending is None:
                                    raise ArtifactIntegrityError(
                                        "committed registry transition has no pending receipt"
                                    )
                                self._verify_committed_locked(records.committed)
                                self._cleanup_pending_generation(records.pending)
                                remove_claim = True
                            elif records.intent is not None:
                                pending = records.pending
                                verified = records.verified
                                if pending is None or verified is None:
                                    raise ArtifactIntegrityError(
                                        "commit intent is missing authenticated prior receipts"
                                    )
                                self._validate_verification_for_pending(pending, verified)
                                self._verify_pending_material(
                                    pending,
                                    quarantine_on_failure=False,
                                )
                                self._complete_commit_locked(pending, verified, records)
                                remove_claim = True
                            else:
                                paths = [
                                    self._pending_path(generation_id, suffix)
                                    for suffix in (".blob", ".json")
                                ]
                                if not ownership.created:
                                    paths.append(active_path)
                                present = [path for path in paths if path.exists()]
                                remove_claim = ownership.created
                                active_recent = not ownership.created and active_path.exists() and (
                                    time.time() - active_path.stat().st_mtime < 1.0
                                )
                                if (
                                    present
                                    and not active_recent
                                    and max(path.stat().st_mtime for path in present) <= cutoff
                                ):
                                    moved = self._quarantine_generation(
                                        generation_id,
                                        include_active=False,
                                    )
                                    quarantine_active = not ownership.created
                    if not ownership.acquired:
                        continue
                    if remove_claim:
                        self._cleanup_files(active_path)
                    elif quarantine_active:
                        moved = self._quarantine_paths(active_path) or moved
                    if moved:
                        quarantined.append(generation_id)
                except (FileNotFoundError, PermissionError):
                    # Windows sharing violations mean another actor may still own the generation.
                    continue
        return tuple(quarantined)

    def _verify_pending_locked(self, pending: PendingGeneration) -> VerificationReceipt:
        self._verify_pending_material(pending, quarantine_on_failure=True)
        receipt = VerificationReceipt(
            generation_id=pending.generation_id,
            object_key=pending.object_key,
            request_id=pending.request_id,
            content_sha256=pending.content_sha256,
            byte_count=pending.byte_count,
            media_type=pending.media_type,
            schema_version=pending.schema_version,
            created_at=pending.created_at,
            encryption_algorithm=pending.encryption_algorithm,
            encryption_provider_version=pending.encryption_provider_version,
            provider_version=pending.provider_version,
            durability_state=pending.durability_state,
            verified_at=_utc_now(),
        )
        self._publish_registry("verified", receipt)
        self._kill_point("after_verified_registry")
        return receipt

    def _verify_pending_material(
        self,
        pending: PendingGeneration,
        *,
        quarantine_on_failure: bool,
    ) -> None:
        data_path = self._pending_path(pending.generation_id, ".blob")
        metadata_path = self._pending_path(pending.generation_id, ".json")
        try:
            metadata = self._read_canonical_metadata(metadata_path)
            if metadata != self._pending_metadata(pending):
                raise ArtifactIntegrityError("pending sidecar does not match the registry receipt")
            digest, byte_count = self._hash_path(data_path)
            if (digest, byte_count) != (pending.content_sha256, pending.byte_count):
                raise ArtifactIntegrityError("pending bytes do not match the registry receipt")
        except (ArtifactIntegrityError, FileNotFoundError) as exc:
            if quarantine_on_failure:
                self._quarantine_generation(pending.generation_id, include_active=False)
            if isinstance(exc, ArtifactIntegrityError):
                raise
            raise ArtifactIntegrityError("pending generation is incomplete") from exc

    def _complete_commit_locked(
        self,
        pending: PendingGeneration,
        verified: VerificationReceipt,
        records: _RequestRecords,
    ) -> ArtifactRef:
        self._validate_verification_for_pending(pending, verified)
        if records.committed is not None:
            self._validate_ref_for_pending(pending, records.committed)
            self._verify_committed_locked(records.committed)
            self._cleanup_pending_generation(pending)
            return records.committed

        ref = records.intent
        if ref is None:
            ref = self._new_artifact_ref(pending)
            self._publish_registry("commit_intent", ref)
            self._kill_point("after_commit_intent")
        else:
            self._validate_ref_for_pending(pending, ref)

        self._publish_committed_data(pending, ref)
        self._kill_point("after_committed_data")
        self._publish_or_validate_committed_metadata(ref)
        self._kill_point("after_committed_metadata")
        self._verify_committed_paths(ref)
        self._publish_registry("committed", ref)
        self._kill_point("after_committed_registry")
        self._cleanup_pending_generation(pending)
        return ref

    def _publish_committed_data(self, pending: PendingGeneration, ref: ArtifactRef) -> None:
        source = self._pending_path(pending.generation_id, ".blob")
        destination = self._committed_path(ref.object_key, ".blob")
        if not destination.exists():
            temporary = self._committed_dir / f"{secrets.token_hex(16)}.blob.tmp"
            try:
                source_descriptor = self._open_storage_file(source, os.O_RDONLY)
                try:
                    temp_descriptor = self._open_storage_file(
                        temporary,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    )
                except Exception:
                    os.close(source_descriptor)
                    raise
                digest = hashlib.sha256()
                byte_count = 0
                with (
                    os.fdopen(source_descriptor, "rb") as input_stream,
                    os.fdopen(temp_descriptor, "wb") as output_stream,
                ):
                    while True:
                        chunk = input_stream.read(self.CHUNK_SIZE)
                        if not chunk:
                            break
                        output_stream.write(chunk)
                        digest.update(chunk)
                        byte_count += len(chunk)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
                if (digest.hexdigest(), byte_count) != (
                    ref.content_sha256,
                    ref.byte_count,
                ):
                    raise ArtifactIntegrityError("pending bytes changed during committed copy")
                os.link(temporary, destination)
                temporary.unlink()
                self._assert_storage_path(destination)
            except FileExistsError:
                pass
            finally:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            self._sync_directory(self._committed_dir)
        digest, byte_count = self._hash_path(destination)
        if (digest, byte_count) != (ref.content_sha256, ref.byte_count):
            raise FileExistsError(f"committed object key already exists: {ref.object_key}")

    def _publish_or_validate_committed_metadata(self, ref: ArtifactRef) -> None:
        path = self._committed_path(ref.object_key, ".json")
        expected = self._committed_metadata(ref)
        if path.exists():
            if self._read_canonical_metadata(path) != expected:
                raise ArtifactIntegrityError("committed sidecar conflicts with commit intent")
            return
        try:
            self._publish_metadata(path, expected)
        except FileExistsError:
            if self._read_canonical_metadata(path) != expected:
                raise ArtifactIntegrityError("committed sidecar conflicts with commit intent") from None

    def _verify_committed_locked(self, ref: ArtifactRef) -> None:
        metadata = self._read_canonical_metadata(self._committed_path(ref.object_key, ".json"))
        if metadata != self._committed_metadata(ref):
            raise ArtifactIntegrityError("committed sidecar does not match the registry receipt")
        self._verify_committed_paths(ref)

    def _verify_committed_paths(self, ref: ArtifactRef) -> None:
        digest, byte_count = self._hash_path(self._committed_path(ref.object_key, ".blob"))
        if (digest, byte_count) != (ref.content_sha256, ref.byte_count):
            raise ArtifactIntegrityError("committed bytes do not match the registry receipt")

    def _request_records(self, request_id: str) -> _RequestRecords:
        return self._matching_records("request_id", request_id)

    def _generation_records(self, generation_id: str) -> _RequestRecords:
        return self._matching_records("generation_id", generation_id)

    def _matching_records(self, field_name: str, expected_value: str) -> _RequestRecords:
        records: dict[str, object] = {}
        for value in self._verified_registry_chain():
            record_type = value["lifecycle"]
            payload = value["receipt"]
            if payload.get(field_name) != expected_value:
                continue
            receipt = self._decode_registry_receipt(record_type, payload)
            prior = records.get(record_type)
            if prior is not None and prior != receipt:
                raise ArtifactIntegrityError(f"registry contains conflicting {record_type} receipts")
            records[record_type] = receipt
        result = _RequestRecords(
            pending=records.get("pending"),
            verified=records.get("verified"),
            intent=records.get("commit_intent"),
            committed=records.get("committed"),
        )
        self._validate_record_chain(result)
        return result

    def _publish_registry(
        self,
        record_type: str,
        receipt: PendingGeneration | VerificationReceipt | ArtifactRef,
    ) -> None:
        existing = self._request_records(receipt.request_id)
        current = {
            "pending": existing.pending,
            "verified": existing.verified,
            "commit_intent": existing.intent,
            "committed": existing.committed,
        }[record_type]
        if current is not None:
            if current != receipt:
                raise ArtifactIntegrityError(f"registry {record_type} receipt conflict")
            return
        state = self._load_coordinator_key()
        sequence = state.sequence + 1
        unsigned = {
            "format_version": _CHAIN_FORMAT_VERSION,
            "sequence": sequence,
            "previous_chain_head": state.chain_head,
            "lifecycle": record_type,
            "request_id": receipt.request_id,
            "receipt": self._serialize_receipt(receipt),
            "key_id": state.key_id,
        }
        entry_hmac = hmac.new(state.secret, _canonical_json(unsigned), hashlib.sha256).hexdigest()
        signed = {**unsigned, "entry_hmac": entry_hmac}
        chain_head = hashlib.sha256(_canonical_json(signed)).hexdigest()
        destination = self._registry_dir / f"{secrets.token_hex(16)}.json"
        self._publish_metadata(destination, {**signed, "chain_head": chain_head})
        self._kill_point(f"after_registry_record_publish:{record_type}")
        self._write_coordinator_key(
            _CoordinatorKeyState(
                secret=state.secret,
                key_id=state.key_id,
                sequence=sequence,
                chain_head=chain_head,
                root_binding_hash=state.root_binding_hash,
            ),
            create=False,
        )
        self._kill_point(f"after_registry_anchor_update:{record_type}")

    def _reject_mismatched_bound_anchor_before_creation(self) -> None:
        self._assert_storage_path(self._binding_path)
        if not self._binding_path.exists():
            return
        self._assert_storage_path(self._binding_path)
        value = self._read_canonical_metadata(self._binding_path)
        configured = os.path.normcase(str(self.anchor_root))
        recorded = value.get("anchor_root")
        if not isinstance(recorded, str) or os.path.normcase(recorded) != configured:
            raise ArtifactIntegrityError("artifact root is already bound to another anchor identity")

    def _initialize_or_verify_root_binding(self) -> None:
        state = self._load_coordinator_key()
        root_status = os.stat(self.root)
        anchor_status = os.stat(self.anchor_root)
        unsigned = {
            "format_version": _ROOT_BINDING_FORMAT_VERSION,
            "artifact_root": os.path.normcase(str(self.root)),
            "artifact_root_device": root_status.st_dev,
            "artifact_root_inode": root_status.st_ino,
            "anchor_root": os.path.normcase(str(self.anchor_root)),
            "anchor_root_device": anchor_status.st_dev,
            "anchor_root_inode": anchor_status.st_ino,
            "key_id": state.key_id,
        }
        binding_hmac = hmac.new(
            state.secret,
            _canonical_json(unsigned),
            hashlib.sha256,
        ).hexdigest()
        expected = {**unsigned, "binding_hmac": binding_hmac}
        binding_hash = hashlib.sha256(_canonical_json(expected)).hexdigest()
        if self._binding_path.exists():
            actual = self._read_canonical_metadata(self._binding_path)
            if actual != expected or not hmac.compare_digest(
                str(actual.get("binding_hmac", "")),
                binding_hmac,
            ):
                raise ArtifactIntegrityError(
                    "artifact root binding HMAC, identity, or rollback check failed"
                )
            if state.root_binding_hash not in {None, binding_hash}:
                raise ArtifactIntegrityError("artifact root binding anchor indicates rollback")
            if state.root_binding_hash is None:
                self._write_coordinator_key(
                    _CoordinatorKeyState(
                        secret=state.secret,
                        key_id=state.key_id,
                        sequence=state.sequence,
                        chain_head=state.chain_head,
                        root_binding_hash=binding_hash,
                    ),
                    create=False,
                )
            return
        if state.root_binding_hash is not None:
            raise ArtifactIntegrityError("artifact root binding was deleted or rolled back")
        self._publish_metadata(self._binding_path, expected)
        self._write_coordinator_key(
            _CoordinatorKeyState(
                secret=state.secret,
                key_id=state.key_id,
                sequence=state.sequence,
                chain_head=state.chain_head,
                root_binding_hash=binding_hash,
            ),
            create=False,
        )

    def _new_deadline(self) -> float:
        return self._monotonic() + self._lock_timeout_seconds

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise ArtifactLockTimeout("artifact operation lock deadline timed out")
        return remaining

    def _assert_storage_path(
        self,
        path: Path,
        *,
        expect_directory: bool = False,
    ) -> None:
        absolute = _lexical_absolute(path)
        roots = (self.root, self.anchor_root)

        def contained_by(base: Path) -> bool:
            try:
                return os.path.commonpath((str(absolute), str(base))) == str(base)
            except ValueError:
                return False

        if not any(contained_by(base) for base in roots):
            raise ArtifactIntegrityError(f"storage path escapes configured roots: {absolute}")
        _reject_link_components(
            absolute,
            require_single_link_file=not expect_directory,
        )
        if absolute.exists():
            resolved = absolute.resolve(strict=True)
            if os.path.normcase(str(resolved)) != os.path.normcase(str(absolute)):
                raise ArtifactIntegrityError(f"storage path resolves outside lexical containment: {absolute}")
            status = os.stat(absolute)
            if expect_directory and not stat.S_ISDIR(status.st_mode):
                raise ArtifactIntegrityError(f"storage directory is not a directory: {absolute}")

    def _open_storage_file(
        self,
        path: Path,
        flags: int,
        mode: int = 0o600,
    ) -> int:
        self._assert_storage_path(path)
        descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)
        try:
            self._assert_storage_path(path)
            path_status = os.lstat(path)
            handle_status = os.fstat(descriptor)
            if (
                path_status.st_dev,
                path_status.st_ino,
            ) != (
                handle_status.st_dev,
                handle_status.st_ino,
            ):
                raise ArtifactIntegrityError("storage path changed while opening its handle")
            if not stat.S_ISREG(handle_status.st_mode) or handle_status.st_nlink != 1:
                raise ArtifactIntegrityError("storage handle is not a single-link regular file")
            return descriptor
        except Exception:
            os.close(descriptor)
            raise

    def _initialize_coordinator_key(self) -> None:
        self._assert_storage_path(self._key_path)
        if self._key_path.exists():
            self._restrict_key_permissions(self._key_path)
            self._load_coordinator_key()
            return
        if self._registry_dir.exists():
            self._assert_storage_path(self._registry_dir, expect_directory=True)
        if self._registry_dir.exists() and any(self._registry_dir.glob("*.json")):
            raise ArtifactIntegrityError("coordinator key is missing for an existing registry chain")
        secret = secrets.token_bytes(32)
        self._write_coordinator_key(
            _CoordinatorKeyState(
                secret=secret,
                key_id=hashlib.sha256(secret).hexdigest(),
                sequence=0,
                chain_head=_CHAIN_GENESIS,
                root_binding_hash=None,
            ),
            create=True,
        )

    def _load_coordinator_key(self) -> _CoordinatorKeyState:
        try:
            value = self._read_canonical_metadata(self._key_path)
            expected_fields = {
                "format_version",
                "secret_b64",
                "key_id",
                "sequence",
                "chain_head",
                "root_binding_hash",
            }
            if set(value) != expected_fields or value["format_version"] != _CHAIN_FORMAT_VERSION:
                raise ArtifactIntegrityError("coordinator key state has invalid fields")
            secret = base64.b64decode(value["secret_b64"], validate=True)
            key_id = value["key_id"]
            sequence = value["sequence"]
            chain_head = value["chain_head"]
            root_binding_hash = value["root_binding_hash"]
            if (
                len(secret) != 32
                or not isinstance(key_id, str)
                or key_id != hashlib.sha256(secret).hexdigest()
                or not isinstance(sequence, int)
                or sequence < 0
                or not isinstance(chain_head, str)
                or _SHA256.fullmatch(chain_head) is None
                or (
                    root_binding_hash is not None
                    and (
                        not isinstance(root_binding_hash, str)
                        or _SHA256.fullmatch(root_binding_hash) is None
                    )
                )
            ):
                raise ArtifactIntegrityError("coordinator key state is invalid")
            return _CoordinatorKeyState(
                secret,
                key_id,
                sequence,
                chain_head,
                root_binding_hash,
            )
        except (ArtifactIntegrityError, FileNotFoundError, ValueError, TypeError, KeyError) as exc:
            raise ArtifactIntegrityError("coordinator key state is malformed") from exc

    def _write_coordinator_key(self, state: _CoordinatorKeyState, *, create: bool) -> None:
        payload = _canonical_json(
            {
                "format_version": _CHAIN_FORMAT_VERSION,
                "secret_b64": base64.b64encode(state.secret).decode("ascii"),
                "key_id": state.key_id,
                "sequence": state.sequence,
                "chain_head": state.chain_head,
                "root_binding_hash": state.root_binding_hash,
            }
        )
        temporary = self.anchor_root / f"{secrets.token_hex(16)}.key.tmp"
        self._assert_storage_path(temporary)
        self._assert_storage_path(self.anchor_root, expect_directory=True)
        if os.name == "nt":
            descriptor = _windows_create_restricted_file(temporary)
        else:
            descriptor = self._open_storage_file(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
            os.fchmod(descriptor, 0o600)
        try:
            self._kill_point("before_coordinator_secret_write")
            self._assert_storage_path(temporary)
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                descriptor = -1
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            self._assert_storage_path(self._key_path)
            if create:
                os.link(temporary, self._key_path)
                temporary.unlink()
            else:
                os.replace(temporary, self._key_path)
            self._restrict_key_permissions(self._key_path)
            self._assert_storage_path(self._key_path)
            self._sync_directory(self.anchor_root)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _verified_registry_chain(self) -> list[dict[str, object]]:
        state = self._load_coordinator_key()
        parsed: list[dict[str, object]] = []
        try:
            self._assert_storage_path(self._registry_dir, expect_directory=True)
            for path in self._registry_dir.glob("*.json"):
                parsed.append(self._read_canonical_metadata(path))
        except ArtifactIntegrityError as exc:
            raise ArtifactIntegrityError("registry chain record is malformed") from exc
        expected_fields = {
            "format_version",
            "sequence",
            "previous_chain_head",
            "lifecycle",
            "request_id",
            "receipt",
            "key_id",
            "entry_hmac",
            "chain_head",
        }
        sequences = [value.get("sequence") for value in parsed]
        if any(not isinstance(sequence, int) for sequence in sequences):
            raise ArtifactIntegrityError("registry chain sequence is malformed")
        if len(sequences) != len(set(sequences)):
            raise ArtifactIntegrityError("registry chain contains a duplicate sequence")
        parsed.sort(key=lambda value: value["sequence"])
        head = _CHAIN_GENESIS
        heads = {0: head}
        for expected_sequence, value in enumerate(parsed, start=1):
            if set(value) != expected_fields:
                raise ArtifactIntegrityError("registry chain record fields are incomplete")
            lifecycle = value["lifecycle"]
            receipt = value["receipt"]
            if (
                value["format_version"] != _CHAIN_FORMAT_VERSION
                or value["sequence"] != expected_sequence
                or value["previous_chain_head"] != head
                or lifecycle not in {"pending", "verified", "commit_intent", "committed"}
                or not isinstance(value["request_id"], str)
                or not isinstance(receipt, dict)
                or receipt.get("request_id") != value["request_id"]
                or value["key_id"] != state.key_id
            ):
                raise ArtifactIntegrityError("registry chain order or immutable fields are invalid")
            unsigned = {
                key: value[key]
                for key in (
                    "format_version",
                    "sequence",
                    "previous_chain_head",
                    "lifecycle",
                    "request_id",
                    "receipt",
                    "key_id",
                )
            }
            expected_hmac = hmac.new(
                state.secret,
                _canonical_json(unsigned),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(value["entry_hmac"], expected_hmac):
                raise ArtifactIntegrityError("registry chain HMAC verification failed")
            expected_head = hashlib.sha256(
                _canonical_json({**unsigned, "entry_hmac": expected_hmac})
            ).hexdigest()
            if value["chain_head"] != expected_head:
                raise ArtifactIntegrityError("registry chain head verification failed")
            head = expected_head
            heads[expected_sequence] = head

        tail_sequence = len(parsed)
        if state.sequence > tail_sequence:
            raise ArtifactIntegrityError("registry chain is truncated or rolled back")
        if heads.get(state.sequence) != state.chain_head:
            raise ArtifactIntegrityError("registry chain anchor indicates rollback")
        if state.sequence < tail_sequence:
            self._write_coordinator_key(
                _CoordinatorKeyState(
                    secret=state.secret,
                    key_id=state.key_id,
                    sequence=tail_sequence,
                    chain_head=head,
                    root_binding_hash=state.root_binding_hash,
                ),
                create=False,
            )
        return parsed

    @staticmethod
    def _restrict_key_permissions(path: Path) -> None:
        if os.name != "nt":
            os.chmod(path, 0o600)
            return
        _windows_set_and_verify_restricted_dacl(path, directory=False)

    @staticmethod
    def _restrict_directory_permissions(path: Path) -> None:
        if os.name != "nt":
            os.chmod(path, 0o700)
            return
        _windows_set_and_verify_restricted_dacl(path, directory=True)

    @staticmethod
    def _decode_registry_receipt(
        record_type: str, payload: dict[str, object]
    ) -> PendingGeneration | VerificationReceipt | ArtifactRef:
        classes = {
            "pending": PendingGeneration,
            "verified": VerificationReceipt,
            "commit_intent": ArtifactRef,
            "committed": ArtifactRef,
        }
        cls = classes.get(record_type)
        if cls is None:
            raise ArtifactIntegrityError(f"unknown registry record type: {record_type}")
        expected_fields = {field.name for field in fields(cls)}
        if set(payload) != expected_fields:
            raise ArtifactIntegrityError(f"registry {record_type} fields are incomplete")
        decoded = dict(payload)
        decoded["state"] = ArtifactState(decoded["state"])
        try:
            return cls(**decoded)
        except (TypeError, ValueError) as exc:
            raise ArtifactIntegrityError(f"registry {record_type} receipt is malformed") from exc

    @staticmethod
    def _serialize_receipt(
        receipt: PendingGeneration | VerificationReceipt | ArtifactRef,
    ) -> dict[str, object]:
        return asdict(receipt)

    @staticmethod
    def _validate_record_chain(records: _RequestRecords) -> None:
        if records.pending is None and any((records.verified, records.intent, records.committed)):
            raise ArtifactIntegrityError("registry transition is missing pending receipt")
        if records.verified is None and any((records.intent, records.committed)):
            raise ArtifactIntegrityError("registry transition is missing object_verified receipt")
        if records.intent is None and records.committed is not None:
            raise ArtifactIntegrityError("registry transition is missing commit intent")
        if records.pending and records.verified:
            LocalArtifactStore._validate_verification_for_pending(records.pending, records.verified)
        if records.pending and records.intent:
            LocalArtifactStore._validate_ref_for_pending(records.pending, records.intent)
        if records.intent and records.committed and records.intent != records.committed:
            raise ArtifactIntegrityError("committed receipt does not match commit intent")

    @staticmethod
    def _validate_idempotent_retry(
        pending: PendingGeneration,
        request: ArtifactWriteRequest,
        digest: str,
        byte_count: int,
    ) -> None:
        expected = (
            pending.request_id,
            pending.media_type,
            pending.schema_version,
            pending.encryption_algorithm,
            pending.encryption_provider_version,
            pending.provider_version,
            pending.durability_state,
            pending.content_sha256,
            pending.byte_count,
        )
        actual = (
            request.request_id,
            request.media_type,
            request.schema_version,
            request.encryption_algorithm,
            request.encryption_provider_version,
            request.provider_version,
            request.durability_state,
            digest,
            byte_count,
        )
        if actual != expected:
            raise ArtifactConflictError("request_id already exists with different immutable input")

    @staticmethod
    def _validate_verification_for_pending(
        pending: PendingGeneration, receipt: VerificationReceipt
    ) -> None:
        expected = (
            pending.generation_id,
            pending.object_key,
            pending.request_id,
            pending.content_sha256,
            pending.byte_count,
            pending.media_type,
            pending.schema_version,
            pending.created_at,
            pending.encryption_algorithm,
            pending.encryption_provider_version,
            pending.provider_version,
            pending.durability_state,
            ArtifactState.OBJECT_VERIFIED,
        )
        actual = (
            receipt.generation_id,
            receipt.object_key,
            receipt.request_id,
            receipt.content_sha256,
            receipt.byte_count,
            receipt.media_type,
            receipt.schema_version,
            receipt.created_at,
            receipt.encryption_algorithm,
            receipt.encryption_provider_version,
            receipt.provider_version,
            receipt.durability_state,
            receipt.state,
        )
        if actual != expected or not receipt.verified_at:
            raise ArtifactIntegrityError("object_verified receipt does not match pending receipt")

    @staticmethod
    def _validate_ref_for_pending(pending: PendingGeneration, ref: ArtifactRef) -> None:
        expected = (
            pending.generation_id,
            pending.object_key,
            pending.request_id,
            pending.content_sha256,
            pending.byte_count,
            pending.media_type,
            pending.schema_version,
            pending.created_at,
            pending.encryption_algorithm,
            pending.encryption_provider_version,
            pending.provider_version,
            "committed_unprotected",
            False,
            ArtifactState.COMMITTED,
        )
        actual = (
            ref.generation_id,
            ref.object_key,
            ref.request_id,
            ref.content_sha256,
            ref.byte_count,
            ref.media_type,
            ref.schema_version,
            ref.created_at,
            ref.encryption_algorithm,
            ref.encryption_provider_version,
            ref.provider_version,
            ref.durability_state,
            ref.coordinator_atomic,
            ref.state,
        )
        if actual != expected or not ref.committed_at:
            raise ArtifactIntegrityError("commit receipt does not match pending receipt")

    @staticmethod
    def _new_artifact_ref(pending: PendingGeneration) -> ArtifactRef:
        return ArtifactRef(
            generation_id=pending.generation_id,
            object_key=pending.object_key,
            request_id=pending.request_id,
            content_sha256=pending.content_sha256,
            byte_count=pending.byte_count,
            media_type=pending.media_type,
            schema_version=pending.schema_version,
            created_at=pending.created_at,
            committed_at=_utc_now(),
            encryption_algorithm=pending.encryption_algorithm,
            encryption_provider_version=pending.encryption_provider_version,
            provider_version=pending.provider_version,
            durability_state="committed_unprotected",
            coordinator_atomic=False,
        )

    @contextmanager
    def _root_coordination(self, *, deadline: float) -> Iterator[None]:
        with self._locked_coordination(
            self._root_thread_lock,
            self._root_coordination_path,
            deadline=deadline,
        ):
            yield

    @contextmanager
    def _coordination(self, *, deadline: float | None = None) -> Iterator[None]:
        if deadline is None:
            deadline = self._new_deadline()
        with self._locked_coordination(
            self._thread_lock,
            self._coordination_path,
            deadline=deadline,
        ):
            yield

    @contextmanager
    def _locked_coordination(
        self,
        thread_lock: threading.RLock,
        lock_path: Path,
        *,
        deadline: float,
    ) -> Iterator[None]:
        acquired_thread = thread_lock.acquire(timeout=self._remaining(deadline))
        if not acquired_thread:
            raise ArtifactLockTimeout("in-process artifact coordination lock timed out")
        try:
            self._assert_storage_path(lock_path)
            descriptor = self._open_storage_file(lock_path, os.O_RDWR)
            locked = False
            try:
                _lock_descriptor(
                    descriptor,
                    blocking=True,
                    timeout_seconds=self._lock_timeout_seconds,
                    retry_interval_seconds=self._lock_retry_interval_seconds,
                    monotonic=self._monotonic,
                    sleeper=self._sleeper,
                    deadline=deadline,
                )
                locked = True
                yield
            finally:
                if locked:
                    _unlock_descriptor(descriptor)
                os.close(descriptor)
        finally:
            thread_lock.release()

    @contextmanager
    def _generation_claim(
        self,
        path: Path,
        *,
        remove_on_success: bool = False,
        remove_on_exit: bool = False,
        deadline: float | None = None,
    ) -> Iterator[Callable[[], None]]:
        if deadline is None:
            deadline = self._new_deadline()
        descriptor, _created = self._open_claim_file(path, deadline=deadline)
        succeeded = False
        locked = False
        try:
            _lock_descriptor(
                descriptor,
                blocking=True,
                timeout_seconds=self._lock_timeout_seconds,
                retry_interval_seconds=self._lock_retry_interval_seconds,
                monotonic=self._monotonic,
                sleeper=self._sleeper,
                deadline=deadline,
            )
            locked = True

            def heartbeat() -> None:
                try:
                    os.utime(path, None)
                except FileNotFoundError:
                    pass

            heartbeat()
            yield heartbeat
            succeeded = True
        finally:
            try:
                if locked:
                    _unlock_descriptor(descriptor)
            finally:
                os.close(descriptor)
            if remove_on_exit or (succeeded and remove_on_success):
                self._cleanup_files(path)

    @contextmanager
    def _try_generation_claim(self, path: Path) -> Iterator[_ClaimOwnership]:
        try:
            descriptor, created = self._open_claim_file(path)
        except PermissionError:
            yield _ClaimOwnership(False, False)
            return
        locked = False
        try:
            locked = _lock_descriptor(descriptor, blocking=False)
            yield _ClaimOwnership(locked, created)
        finally:
            if locked:
                _unlock_descriptor(descriptor)
            os.close(descriptor)

    def _open_claim_file(
        self,
        path: Path,
        *,
        deadline: float | None = None,
    ) -> tuple[int, bool]:
        while True:
            if deadline is not None:
                self._remaining(deadline)
            self._assert_storage_path(path)
            try:
                descriptor = self._open_storage_file(path, os.O_RDWR)
                return descriptor, False
            except PermissionError:
                if deadline is None:
                    raise
                self._sleeper(
                    min(
                        self._lock_retry_interval_seconds,
                        self._remaining(deadline),
                    )
                )
            except FileNotFoundError:
                try:
                    descriptor = self._open_storage_file(
                        path,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL,
                    )
                except FileExistsError:
                    continue
                except PermissionError:
                    if deadline is None:
                        raise
                    self._sleeper(
                        min(
                            self._lock_retry_interval_seconds,
                            self._remaining(deadline),
                        )
                    )
                    continue
                try:
                    os.write(descriptor, b"\0")
                    os.fsync(descriptor)
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    return descriptor, True
                except Exception:
                    os.close(descriptor)
                    raise

    def _ensure_lock_file(self, path: Path) -> None:
        self._assert_storage_path(path)
        descriptor = self._open_storage_file(path, os.O_RDWR | os.O_CREAT)
        try:
            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write_stream_exclusive(
        self,
        destination: Path,
        stream: BinaryIO,
        heartbeat: Callable[[], None],
    ) -> tuple[str, int]:
        descriptor = self._open_storage_file(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
        digest = hashlib.sha256()
        byte_count = 0
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                while True:
                    chunk = stream.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    if not isinstance(chunk, bytes):
                        raise TypeError("artifact streams must return bytes")
                    output.write(chunk)
                    digest.update(chunk)
                    byte_count += len(chunk)
                    heartbeat()
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            self._cleanup_files(destination)
            raise
        self._sync_directory(destination.parent)
        return digest.hexdigest(), byte_count

    def _publish_metadata(self, destination: Path, value: dict[str, object]) -> None:
        self._assert_storage_path(destination)
        self._assert_storage_path(destination.parent, expect_directory=True)
        temporary = destination.parent / f"{secrets.token_hex(16)}.tmp"
        payload = _canonical_json(value)
        descriptor = self._open_storage_file(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, destination)
            temporary.unlink()
            self._assert_storage_path(destination)
            self._sync_directory(destination.parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _read_canonical_metadata(self, path: Path) -> dict[str, object]:
        descriptor = self._open_storage_file(path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as stream:
            payload = stream.read()
        try:
            value = json.loads(payload.decode("ascii"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ArtifactIntegrityError("metadata is malformed") from exc
        if not isinstance(value, dict) or payload != _canonical_json(value):
            raise ArtifactIntegrityError("metadata is not canonical JSON")
        return value

    def _hash_path(self, path: Path) -> tuple[str, int]:
        descriptor = self._open_storage_file(path, os.O_RDONLY)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            byte_count = 0
            while True:
                chunk = stream.read(self.CHUNK_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                byte_count += len(chunk)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ArtifactIntegrityError("artifact changed during verification")
        return digest.hexdigest(), byte_count

    @staticmethod
    def _source_snapshot(path: Path) -> _SourceSnapshot:
        stat = path.stat()
        if not path.is_file():
            raise ValueError("artifact source path must be a regular file")
        return _SourceSnapshot(stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)

    def _quarantine_generation(self, generation_id: str, *, include_active: bool = True) -> bool:
        _validate_generation_id(generation_id)
        suffixes = (".blob", ".json", ".active") if include_active else (".blob", ".json")
        paths = tuple(self._pending_path(generation_id, suffix) for suffix in suffixes)
        moved = self._quarantine_paths(*paths)
        if moved:
            self._sync_directory(self._quarantine_dir)
            self._sync_directory(self._pending_dir)
        return moved

    def _quarantine_paths(self, *paths: Path) -> bool:
        publications: list[tuple[Path, Path]] = []
        for source in paths:
            if not source.exists():
                continue
            self._assert_storage_path(source)
            generation_id = source.name.rsplit(".", 1)[0]
            destination = self._quarantine_dir / f"{generation_id}.{secrets.token_hex(16)}{source.suffix}"
            self._assert_storage_path(destination)
            try:
                self._copy_file_exclusive(source, destination)
            except PermissionError:
                self._cleanup_files(*(published for _, published in publications))
                return False
            publications.append((source, destination))
        if not publications:
            return False
        self._sync_directory(self._quarantine_dir)
        for source, _ in publications:
            try:
                self._assert_storage_path(source)
                source.unlink()
            except (FileNotFoundError, PermissionError):
                # The complete quarantine copy is already published. A sharing
                # violation leaves only retryable pending residue, never data loss.
                pass
        return True

    def _copy_file_exclusive(self, source: Path, destination: Path) -> None:
        source_descriptor = self._open_storage_file(source, os.O_RDONLY)
        try:
            destination_descriptor = self._open_storage_file(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            )
        except Exception:
            os.close(source_descriptor)
            raise
        try:
            with (
                os.fdopen(source_descriptor, "rb") as input_stream,
                os.fdopen(destination_descriptor, "wb") as output_stream,
            ):
                while True:
                    chunk = input_stream.read(self.CHUNK_SIZE)
                    if not chunk:
                        break
                    output_stream.write(chunk)
                output_stream.flush()
                os.fsync(output_stream.fileno())
        except Exception:
            self._cleanup_files(destination)
            raise
        self._assert_storage_path(destination)

    def _cleanup_pending_generation(self, pending: PendingGeneration) -> None:
        self._cleanup_files(
            self._pending_path(pending.generation_id, ".json"),
            self._pending_path(pending.generation_id, ".blob"),
            self._pending_path(pending.generation_id, ".active"),
        )
        self._sync_directory(self._pending_dir)

    def _cleanup_files(self, *paths: Path) -> None:
        for path in paths:
            try:
                self._assert_storage_path(path)
                path.unlink()
            except (FileNotFoundError, PermissionError):
                pass

    def _sync_directory(self, path: Path) -> None:
        self._assert_storage_path(path, expect_directory=True)
        if not self._directory_fsync_supported:
            return
        try:
            _strict_fsync_directory(path)
        except OSError:
            self._directory_fsync_supported = False
            self.capabilities = StoreCapabilities(
                provider_version=_LOCAL_PROVIDER_VERSION,
                directory_fsync_supported=False,
                durability_state="committed_unprotected",
                coordinator_atomic=False,
                anchor_path_separated=True,
                anchor_same_volume=self._anchor_same_volume,
            )

    def _kill_point(self, point: str) -> None:
        if self._fault_injector is not None:
            self._fault_injector(point)

    @staticmethod
    def _validate_request(request: ArtifactWriteRequest) -> None:
        if not request.request_id or "\x00" in request.request_id:
            raise ValueError("request_id must be a non-empty opaque value")
        if not request.media_type or request.schema_version < 1:
            raise ValueError("media_type and positive schema_version are required")
        if request.encryption_algorithm != "none" or request.encryption_provider_version is not None:
            raise ValueError("LocalArtifactStore supports only encryption_algorithm='none'")
        if request.provider_version != _LOCAL_PROVIDER_VERSION or request.durability_state != "pending":
            raise ValueError(f"LocalArtifactStore requires {_LOCAL_PROVIDER_VERSION} pending writes")

    @staticmethod
    def _validate_pending_handle(pending: PendingGeneration) -> None:
        _validate_generation_id(pending.generation_id)
        _validate_object_key(pending.object_key)
        if _SHA256.fullmatch(pending.content_sha256) is None:
            raise ValueError("content_sha256 must be lowercase SHA-256")
        if pending.object_key != pending.content_sha256:
            raise ArtifactIntegrityError("pending object key is not its content address")
        if pending.state is not ArtifactState.PENDING or pending.durability_state != "pending":
            raise ArtifactIntegrityError("generation is not pending")

    @staticmethod
    def _validate_artifact_handle(artifact: ArtifactRef) -> None:
        _validate_generation_id(artifact.generation_id)
        _validate_object_key(artifact.object_key)
        if _SHA256.fullmatch(artifact.content_sha256) is None:
            raise ValueError("content_sha256 must be lowercase SHA-256")
        if artifact.object_key != artifact.content_sha256:
            raise ArtifactIntegrityError("committed object key is not its content address")
        if (
            artifact.state is not ArtifactState.COMMITTED
            or artifact.durability_state != "committed_unprotected"
            or artifact.coordinator_atomic
        ):
            raise ArtifactIntegrityError("local artifact has an invalid committed state")

    @staticmethod
    def _pending_metadata(pending: PendingGeneration) -> dict[str, object]:
        return {"lifecycle": ArtifactState.PENDING.value, **asdict(pending)}

    @staticmethod
    def _committed_metadata(artifact: ArtifactRef) -> dict[str, object]:
        return {
            "lifecycle": "committed_unprotected",
            "object_key": artifact.object_key,
            "content_sha256": artifact.content_sha256,
            "byte_count": artifact.byte_count,
            "encryption_algorithm": artifact.encryption_algorithm,
            "encryption_provider_version": artifact.encryption_provider_version,
            "provider_version": artifact.provider_version,
            "durability_state": artifact.durability_state,
        }

    def _pending_path(self, generation_id: str, suffix: str) -> Path:
        _validate_generation_id(generation_id)
        path = self._pending_dir / f"{generation_id}{suffix}"
        self._assert_storage_path(path)
        return path

    def _committed_path(self, object_key: str, suffix: str) -> Path:
        _validate_object_key(object_key)
        path = self._committed_dir / f"{object_key}{suffix}"
        self._assert_storage_path(path)
        return path
