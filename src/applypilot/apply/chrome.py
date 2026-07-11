"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import hashlib
import logging
import os
import platform
import signal
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from applypilot import config
from applypilot.apply.lifecycle_fault import (
    enforce_no_lifecycle_faults,
    require_browser_cleanup,
)
from applypilot.apply.process_guard import (
    darwin_process_executable,
    parse_ps_lstart_local,
)

logger = logging.getLogger(__name__)

# CDP port base — each worker uses BASE_CDP_PORT + worker_id. Default 9400 deliberately AVOIDS
# Chrome's universal default debug port 9222: any stray/other Chrome on a box (e.g. an
# interactive scoring/login window) grabs 9222, which broke slot 0 (port 9222+0) specifically
# on GGGTower 2026-07-04 while slots 1-3 on 9223-9225 were fine. Override with
# APPLYPILOT_BASE_CDP_PORT; must not overlap LINKEDIN_LOGIN_CDP_PORT (9333) for the slot range.
try:
    BASE_CDP_PORT = int(os.environ.get("APPLYPILOT_BASE_CDP_PORT") or 9400)
except (TypeError, ValueError):
    BASE_CDP_PORT = 9400

# Dedicated Chrome profile holding the one-time LinkedIn login (the li_at session). Apply
# workers clone from it so they inherit the authenticated LinkedIn session. Populated by
# `applypilot linkedin-login`. A separate CDP port keeps the login window off the apply
# workers' ports so it never collides with a live run.
SEED_PROFILE_NAME = "linkedin-seed"
LINKEDIN_LOGIN_CDP_PORT = 9333
LINKEDIN_LOGIN_SLOT = -1

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()
_browser_reservations: dict[int, "_BrowserReservation"] = {}

# Windows Job Object handles per worker. Each launched Chrome is assigned to a job with
# KILL_ON_JOB_CLOSE, so the whole browser tree dies when THIS python process dies for ANY
# reason (graceful exit, crash, OOM, or an external taskkill of the parent) -- not only on
# the graceful cleanup_worker path. This is what stops orphaned Chrome trees from lingering
# and locking worker profiles. Keeping the handle referenced here holds the job open; the
# OS closes it on process exit, which fires the kill.
class _OwnedJobHandle:
    def __init__(self, worker_id: int, pid: int, handle: int) -> None:
        self.worker_id = worker_id
        self.pid = pid
        self.handle = handle


_job_handles: dict[int, _OwnedJobHandle] = {}


class BrowserSlotOccupiedError(RuntimeError):
    """The requested browser profile or CDP port is reserved or already occupied."""


@dataclass(frozen=True)
class BrowserProcessIdentity:
    pid: int
    created_at: float
    executable: str
    command: str
    profile_dir: str
    port: int
    parent_pid: int = 0
    parent_created_at: float = 0.0
    parent_executable: str = ""
    parent_command: str = ""


@dataclass
class _SpawnGuard:
    process: subprocess.Popen
    kind: str
    handle: int
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self.kind == "pidfd":
            try:
                os.close(self.handle)
            except OSError:
                pass

    def terminate_and_reap(self) -> bool:
        if self._released:
            return False
        try:
            if self.process.poll() is None:
                if self.kind == "windows":
                    if not _terminate_windows_handle(self.handle, self.process.pid):
                        return False
                elif self.kind == "pidfd":
                    sender = getattr(signal, "pidfd_send_signal", None)
                    if sender is None:
                        return False
                    sender(self.handle, getattr(signal, "SIGKILL", 9))
                elif self.kind == "darwin-child":
                    self.process.kill()
                else:
                    return False
            self.process.wait(timeout=20)
            return self.process.poll() is not None
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        finally:
            self.release()


class _BrowserReservation:
    def __init__(
        self,
        handles,
        *,
        worker_id: int,
        port: int,
        profile_dir: Path,
        metadata_path: Path,
        browser_identity: BrowserProcessIdentity | None,
    ) -> None:
        self._handles = handles
        self.worker_id = worker_id
        self.port = port
        self.profile_dir = profile_dir
        self.metadata_path = metadata_path
        self.browser_identity = browser_identity
        self._released = False

    def record_browser_identity(self, identity: BrowserProcessIdentity) -> None:
        if (
            identity.port != self.port
            or _normalized_path(identity.profile_dir) != _normalized_path(self.profile_dir)
        ):
            raise ValueError("browser identity does not match reservation")
        temp = self.metadata_path.with_name(
            f"{self.metadata_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        temp.write_text(json.dumps(asdict(identity), sort_keys=True), encoding="utf-8")
        os.replace(temp, self.metadata_path)
        self.browser_identity = identity

    def release(self, *, clear_identity: bool = False) -> None:
        if self._released:
            return
        if clear_identity:
            try:
                self.metadata_path.unlink(missing_ok=True)
                self.browser_identity = None
            except OSError:
                logger.debug("Browser identity metadata cleanup failed", exc_info=True)
        self._released = True
        for handle in reversed(self._handles):
            try:
                handle.seek(0)
                if platform.system() == "Windows":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                logger.debug("Browser reservation unlock failed", exc_info=True)
            finally:
                handle.close()
        self._handles.clear()


def _browser_lock_dir() -> Path:
    configured = os.environ.get("APPLYPILOT_BROWSER_LOCK_DIR")
    path = Path(configured) if configured else Path(config.APP_DIR) / "browser-locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _normalized_path(value) -> str:
    return os.path.normcase(os.path.abspath(str(value))).replace("\\", "/")


def _load_browser_identity(path: Path) -> BrowserProcessIdentity | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return BrowserProcessIdentity(
            pid=int(raw["pid"]),
            created_at=float(raw["created_at"]),
            executable=str(raw["executable"]),
            command=str(raw["command"]),
            profile_dir=str(raw["profile_dir"]),
            port=int(raw["port"]),
            parent_pid=int(raw.get("parent_pid") or 0),
            parent_created_at=float(raw.get("parent_created_at") or 0.0),
            parent_executable=str(raw.get("parent_executable") or ""),
            parent_command=str(raw.get("parent_command") or ""),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _worker_profile_dir(worker_id: int, browser: str | None) -> Path:
    suffix = _browser_profile_suffix(browser)
    return config.CHROME_WORKER_DIR / f"worker-{worker_id}{suffix}"


def _try_lock_reservation_file(path: Path):
    handle = open(path, "a+b", buffering=0)
    try:
        if path.stat().st_size == 0:
            handle.write(b"\0")
        handle.seek(0)
        if platform.system() == "Windows":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps({
            "pid": os.getpid(),
            "owner": uuid.uuid4().hex,
            "acquired_at": time.time(),
        }).encode("ascii"))
        handle.flush()
        handle.seek(0)
        return handle
    except (OSError, BlockingIOError):
        handle.close()
        raise BrowserSlotOccupiedError("browser slot or port is already reserved") from None


def _acquire_browser_reservation(
    worker_id: int,
    port: int,
    profile_dir: Path,
) -> _BrowserReservation:
    """Atomically reserve both the machine CDP port and the profile slot."""
    profile_key = hashlib.sha256(
        os.path.normcase(str(profile_dir.resolve())).encode("utf-8")
    ).hexdigest()[:32]
    paths = sorted((
        _browser_lock_dir() / f"port-{port}.lock",
        _browser_lock_dir() / f"profile-{profile_key}.lock",
        _browser_lock_dir() / f"slot-{worker_id}.lock",
    ))
    metadata_path = _browser_lock_dir() / f"identity-{worker_id}-{port}-{profile_key}.json"
    handles = []
    try:
        for path in paths:
            handles.append(_try_lock_reservation_file(path))
    except Exception:
        _BrowserReservation(
            handles,
            worker_id=worker_id,
            port=port,
            profile_dir=profile_dir,
            metadata_path=metadata_path,
            browser_identity=None,
        ).release()
        raise
    return _BrowserReservation(
        handles,
        worker_id=worker_id,
        port=port,
        profile_dir=profile_dir,
        metadata_path=metadata_path,
        browser_identity=_load_browser_identity(metadata_path),
    )


def _port_is_listening(port: int) -> bool:
    return bool(_listener_pids(port))


def _listener_pids(port: int) -> list[int]:
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            pids = set()
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) >= 5 and fields[3].upper() == "LISTENING":
                    local = fields[1].rsplit(":", 1)
                    if len(local) == 2 and local[1] == str(port) and fields[-1].isdigit():
                        pids.add(int(fields[-1]))
            return sorted(pids)
        result = subprocess.run(
            ["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return sorted({int(value) for value in result.stdout.split() if value.isdigit()})
    except (OSError, subprocess.SubprocessError):
        return []


def _process_identity(pid: int) -> BrowserProcessIdentity | None:
    try:
        if platform.system() == "Windows":
            script = (
                f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\";"
                "$parent=if($p){Get-CimInstance Win32_Process -Filter "
                "\"ProcessId=$($p.ParentProcessId)\"};"
                "if($p){[pscustomobject]@{Pid=$p.ProcessId;"
                "Created=([DateTimeOffset]$p.CreationDate).ToUnixTimeMilliseconds()/1000;"
                "Executable=$p.ExecutablePath;Command=$p.CommandLine;"
                "ParentPid=$p.ParentProcessId;"
                "ParentCreated=$(if($parent){"
                "([DateTimeOffset]$parent.CreationDate).ToUnixTimeMilliseconds()/1000}else{0});"
                "ParentExecutable=$(if($parent){$parent.ExecutablePath}else{''});"
                "ParentCommand=$(if($parent){$parent.CommandLine}else{''})}"
                "|ConvertTo-Json -Compress}"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return None
            raw = json.loads(result.stdout)
            command = str(raw.get("Command") or "")
            profile, port = _browser_markers(command)
            return BrowserProcessIdentity(
                pid=int(raw["Pid"]),
                created_at=float(raw["Created"]),
                executable=str(raw.get("Executable") or ""),
                command=command,
                profile_dir=profile,
                port=port,
                parent_pid=int(raw.get("ParentPid") or 0),
                parent_created_at=float(raw.get("ParentCreated") or 0.0),
                parent_executable=str(raw.get("ParentExecutable") or ""),
                parent_command=str(raw.get("ParentCommand") or ""),
            )

        if platform.system() == "Darwin":
            return _darwin_process_identity(pid)

        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()
        boot_line = next(
            line for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines()
            if line.startswith("btime ")
        )
        created = float(boot_line.split()[1]) + float(stat_fields[21]) / os.sysconf("SC_CLK_TCK")
        executable = os.readlink(f"/proc/{pid}/exe")
        command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode(
            "utf-8", "replace"
        ).strip()
        profile, port = _browser_markers(command)
        parent_pid = int(stat_fields[3])
        parent_stat = Path(f"/proc/{parent_pid}/stat").read_text(encoding="utf-8").split()
        parent_created = (
            float(boot_line.split()[1])
            + float(parent_stat[21]) / os.sysconf("SC_CLK_TCK")
        )
        parent_executable = os.readlink(f"/proc/{parent_pid}/exe")
        parent_command = Path(f"/proc/{parent_pid}/cmdline").read_bytes().replace(
            b"\0", b" "
        ).decode("utf-8", "replace").strip()
        return BrowserProcessIdentity(
            pid,
            created,
            executable,
            command,
            profile,
            port,
            parent_pid,
            parent_created,
            parent_executable,
            parent_command,
        )
    except (OSError, ValueError, KeyError, StopIteration, json.JSONDecodeError):
        return None


def _darwin_process_identity(pid: int) -> BrowserProcessIdentity | None:
    def row(process_id: int):
        result = subprocess.run(
            ["ps", "-p", str(process_id), "-o", "ppid=,lstart=,command="],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        parts = result.stdout.strip().split(None, 6)
        if result.returncode != 0 or len(parts) != 7:
            return None
        created = parse_ps_lstart_local(" ".join(parts[1:6]))
        executable = darwin_process_executable(process_id)
        command = parts[6]
        if not executable or not command:
            return None
        return int(parts[0]), created, executable, command

    current = row(pid)
    if current is None:
        return None
    parent_pid, created, executable, command = current
    parent = row(parent_pid)
    profile, port = _browser_markers(command)
    return BrowserProcessIdentity(
        pid=pid,
        created_at=created,
        executable=executable,
        command=command,
        profile_dir=profile,
        port=port,
        parent_pid=parent_pid,
        parent_created_at=parent[1] if parent else 0.0,
        parent_executable=parent[2] if parent else "",
        parent_command=parent[3] if parent else "",
    )


def _browser_markers(command: str) -> tuple[str, int]:
    import re

    port_match = re.search(r"--remote-debugging-port(?:=|\s+)(\d+)", command)
    profile_match = re.search(
        r'--user-data-dir(?:=|\s+)(?:"([^"]+)"|\'([^\']+)\'|(\S+))',
        command,
    )
    profile = next((value for value in profile_match.groups() if value), "") if profile_match else ""
    return profile, int(port_match.group(1)) if port_match else 0


def _browser_identity_matches(
    expected: BrowserProcessIdentity | None,
    actual: BrowserProcessIdentity | None,
) -> bool:
    if expected is None or actual is None:
        return False
    return (
        actual.pid == expected.pid
        and abs(actual.created_at - expected.created_at) < 0.001
        and bool(actual.executable)
        and _normalized_path(actual.executable) == _normalized_path(expected.executable)
        and actual.port == expected.port
        and actual.port > 0
        and _normalized_path(actual.profile_dir) == _normalized_path(expected.profile_dir)
    )


def _browser_parent_identity_matches(
    expected: BrowserProcessIdentity | None,
    actual: BrowserProcessIdentity | None,
) -> bool:
    if expected is None or actual is None:
        return False
    return (
        expected.parent_pid > 0
        and actual.parent_pid == expected.parent_pid
        and expected.parent_created_at > 0
        and abs(actual.parent_created_at - expected.parent_created_at) < 0.001
        and bool(expected.parent_executable)
        and bool(actual.parent_executable)
        and _normalized_path(actual.parent_executable)
        == _normalized_path(expected.parent_executable)
        and bool(expected.parent_command)
        and actual.parent_command == expected.parent_command
    )


def _original_parent_state(expected: BrowserProcessIdentity) -> str:
    current = _process_identity(expected.parent_pid)
    if current is not None:
        if (
            abs(current.created_at - expected.parent_created_at) < 0.001
            and _normalized_path(current.executable)
            == _normalized_path(expected.parent_executable)
        ):
            return "matching"
        return "reused"
    existence = _process_exists(expected.parent_pid)
    return "gone" if existence is False else "uncertain"


def _process_exists(pid: int) -> bool | None:
    try:
        system = platform.system()
        if system == "Linux":
            return Path(f"/proc/{pid}").exists()
        if system == "Darwin":
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "pid="],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
            return result.returncode == 0 and bool(result.stdout.strip())
        if system == "Windows":
            script = (
                f"$p=Get-CimInstance Win32_Process -Filter \"ProcessId={int(pid)}\";"
                "if($p){'yes'}else{'no'}"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            if result.returncode != 0:
                return None
            return result.stdout.strip().lower() == "yes"
    except (OSError, subprocess.SubprocessError):
        return None
    return None


def _reserved_parent_authority_matches(
    expected: BrowserProcessIdentity,
    current: BrowserProcessIdentity,
) -> bool:
    if _browser_parent_identity_matches(expected, current):
        return True
    reparented = (
        current.parent_pid > 0
        and current.parent_pid != expected.parent_pid
        and current.parent_created_at > 0
        and bool(current.parent_executable)
        and bool(current.parent_command)
    )
    return reparented and _original_parent_state(expected) in {"gone", "reused"}


def terminate_verified_process(
    *,
    pid: int,
    created_at: float,
    executable: str,
    final_authority: Callable[[], bool] | None,
    direct_child: subprocess.Popen | None = None,
) -> bool:
    """Terminate only the process instance identified by PID, start time, and image."""
    if pid <= 0 or created_at <= 0 or not executable or final_authority is None:
        return False
    if platform.system() == "Darwin":
        if (
            direct_child is None
            or direct_child.pid != pid
            or direct_child.poll() is not None
        ):
            logger.error("Darwin cleanup refused without live direct-child authority")
            return False
        actual = _process_identity(pid)
        if (
            actual is None
            or abs(actual.created_at - created_at) >= 0.001
            or _normalized_path(actual.executable) != _normalized_path(executable)
        ):
            return False
        try:
            if final_authority() is not True:
                return False
            direct_child.kill()
            return True
        except (OSError, ValueError):
            return False

    if platform.system() != "Windows":
        pidfd_open = getattr(os, "pidfd_open", None)
        pidfd_send_signal = getattr(signal, "pidfd_send_signal", None)
        if pidfd_open is None or pidfd_send_signal is None:
            return False
        try:
            pidfd = pidfd_open(pid, 0)
        except (OSError, ValueError):
            return False
        try:
            actual = _process_identity(pid)
            if (
                actual is None
                or abs(actual.created_at - created_at) >= 0.001
                or _normalized_path(actual.executable) != _normalized_path(executable)
            ):
                return False
            try:
                if final_authority() is not True:
                    return False
            except Exception:
                return False
            pidfd_send_signal(pidfd, getattr(signal, "SIGKILL", 9))
            return True
        except (OSError, ValueError):
            return False
        finally:
            try:
                os.close(pidfd)
            except OSError:
                pass

    import ctypes
    from ctypes import wintypes

    process_terminate = 0x0001
    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(
        process_terminate | process_query_limited_information,
        False,
        pid,
    )
    if not handle:
        return False
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel_time),
            ctypes.byref(user_time),
        ):
            return False
        creation_ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        handle_created_at = (creation_ticks - 116_444_736_000_000_000) / 10_000_000
        image_buffer = ctypes.create_unicode_buffer(32768)
        image_length = wintypes.DWORD(len(image_buffer))
        if not kernel32.QueryFullProcessImageNameW(
            handle, 0, image_buffer, ctypes.byref(image_length)
        ):
            return False
        if (
            abs(handle_created_at - created_at) >= 0.001
            or _normalized_path(image_buffer.value) != _normalized_path(executable)
        ):
            return False
        try:
            if final_authority() is not True:
                return False
        except Exception:
            return False
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def _terminate_reserved_listener(
    reservation: _BrowserReservation,
    listener_pid: int,
) -> bool:
    current = _validated_reserved_listener(reservation, listener_pid)
    if current is None:
        return False
    return terminate_verified_process(
        pid=current.pid,
        created_at=current.created_at,
        executable=current.executable,
        final_authority=lambda: (
            _validated_reserved_listener(reservation, listener_pid) is not None
        ),
    )


def _validated_reserved_listener(
    reservation: _BrowserReservation,
    listener_pid: int,
) -> BrowserProcessIdentity | None:
    expected = reservation.browser_identity
    if (
        reservation._released
        or expected is None
        or expected.pid != listener_pid
        or expected.port != reservation.port
        or _normalized_path(expected.profile_dir) != _normalized_path(reservation.profile_dir)
        or _listener_pids(reservation.port) != [listener_pid]
    ):
        return None
    current = _process_identity(listener_pid)
    if not (
        _browser_identity_matches(expected, current)
        and _reserved_parent_authority_matches(expected, current)
    ):
        return None
    return current


def _cleanup_reserved_listener(reservation: _BrowserReservation) -> bool:
    listeners = _listener_pids(reservation.port)
    if not listeners:
        return True
    if len(listeners) != 1:
        return False
    listener_pid = listeners[0]
    if _validated_reserved_listener(reservation, listener_pid) is None:
        return False
    if not _terminate_reserved_listener(reservation, listener_pid):
        return False
    return not _listener_pids(reservation.port)


def _record_launched_browser_identity(
    reservation: _BrowserReservation,
    process: subprocess.Popen,
    executable: str,
    profile_dir: Path,
    port: int,
) -> None:
    actual = _process_identity(process.pid)
    expected = BrowserProcessIdentity(
        pid=process.pid,
        created_at=actual.created_at if actual is not None else 0.0,
        executable=str(executable),
        command=actual.command if actual is not None else "",
        profile_dir=str(profile_dir),
        port=port,
    )
    if not _browser_identity_matches(expected, actual):
        raise RuntimeError("launched browser identity could not be verified")
    reservation.record_browser_identity(actual)


def _profile_appears_occupied(profile_dir: Path) -> bool:
    return any((profile_dir / name).exists() for name in (
        "SingletonLock",
        "SingletonSocket",
        "SingletonCookie",
    ))


def _reserve_browser_launch(
    worker_id: int,
    port: int,
    profile_dir: Path,
    *,
    kill_existing: bool,
) -> _BrowserReservation:
    """Own slot/profile/port before either refusing or performing legacy cleanup."""
    reservation = _acquire_browser_reservation(worker_id, port, profile_dir)
    try:
        with _chrome_lock:
            tracked = _chrome_procs.get(worker_id)
        if not kill_existing and (
            (tracked is not None and tracked.poll() is None)
            or _port_is_listening(port)
            or _profile_appears_occupied(profile_dir)
        ):
            raise BrowserSlotOccupiedError("browser slot, profile, or port is occupied")
        if kill_existing:
            if not _cleanup_reserved_listener(reservation):
                raise BrowserSlotOccupiedError("reserved port listener identity is foreign or ambiguous")
        return reservation
    except Exception:
        reservation.release()
        raise


class BrowserCleanupOwnership:
    """Exclusive slot/profile/port ownership for coordinated orphan cleanup."""

    def __init__(self, reservation: _BrowserReservation) -> None:
        self._reservation = reservation

    def record_browser_identity(self, identity: BrowserProcessIdentity) -> None:
        self._reservation.record_browser_identity(identity)

    def cleanup_browser(self) -> bool:
        return _cleanup_reserved_listener(self._reservation)

    def release(self, *, clear_identity: bool = False) -> None:
        self._reservation.release(clear_identity=clear_identity)


def reserve_browser_cleanup(
    worker_id: int,
    port: int,
    profile_dir: Path,
) -> BrowserCleanupOwnership | None:
    try:
        reservation = _acquire_browser_reservation(worker_id, port, profile_dir)
    except BrowserSlotOccupiedError:
        return None
    return BrowserCleanupOwnership(reservation)


def cleanup_orphaned_browser(worker_id: int, port: int, profile_dir: Path) -> bool:
    """Clean an unreserved browser orphan while holding slot/profile/port ownership."""
    ownership = reserve_browser_cleanup(worker_id, port, profile_dir)
    if ownership is None:
        return False
    cleaned = False
    try:
        cleaned = ownership.cleanup_browser()
        return cleaned
    except Exception:
        logger.debug("Orphan browser cleanup failed for slot %s", worker_id, exc_info=True)
        return False
    finally:
        ownership.release(clear_identity=cleaned)


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(
    process: subprocess.Popen,
    expected: BrowserProcessIdentity | None,
    reservation: _BrowserReservation | None,
) -> bool:
    """Terminate only the browser instance bound to an owned reservation claim."""
    if (
        expected is None
        or reservation is None
        or reservation._released
        or process.pid != expected.pid
        or reservation.browser_identity != expected
        or expected.parent_pid <= 0
        or expected.parent_created_at <= 0
        or not expected.parent_executable
        or not expected.parent_command
        or expected.port != reservation.port
        or _normalized_path(expected.profile_dir) != _normalized_path(reservation.profile_dir)
    ):
        return False
    if not _owned_browser_authority_is_current(process, expected, reservation):
        return False
    return terminate_verified_process(
        pid=expected.pid,
        created_at=expected.created_at,
        executable=expected.executable,
        final_authority=lambda: _owned_browser_authority_is_current(
            process, expected, reservation
        ),
        direct_child=process,
    )


def _owned_browser_authority_is_current(
    process: subprocess.Popen,
    expected: BrowserProcessIdentity,
    reservation: _BrowserReservation,
) -> bool:
    with _chrome_lock:
        registered = _chrome_procs.get(reservation.worker_id)
        registered_reservation = _browser_reservations.get(id(process))
    if (
        reservation._released
        or process.pid != expected.pid
        or reservation.browser_identity != expected
        or registered is not process
        or registered_reservation is not reservation
        or expected.port != reservation.port
        or _normalized_path(expected.profile_dir) != _normalized_path(reservation.profile_dir)
    ):
        return False
    current = _process_identity(process.pid)
    return bool(
        _browser_identity_matches(expected, current)
        and _browser_parent_identity_matches(expected, current)
    )


def _popen_process_handle(process: subprocess.Popen) -> int:
    try:
        return int(process._handle)
    except (AttributeError, TypeError, ValueError):
        return 0


def _acquire_spawn_guard(process: subprocess.Popen) -> _SpawnGuard | None:
    system = platform.system()
    if system == "Windows":
        handle = _popen_process_handle(process)
        return _SpawnGuard(process, "windows", handle) if handle else None
    if system == "Linux":
        opener = getattr(os, "pidfd_open", None)
        if opener is None:
            return None
        try:
            return _SpawnGuard(process, "pidfd", opener(process.pid, 0))
        except (OSError, ValueError):
            return None
    if system == "Darwin":
        return _SpawnGuard(process, "darwin-child", 0)
    return None


def _terminate_windows_handle(handle: int, expected_pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL
    if int(kernel32.GetProcessId(handle) or 0) != expected_pid:
        return False
    return bool(kernel32.TerminateProcess(handle, 1))


def _resume_windows_process(handle: int) -> bool:
    import ctypes
    from ctypes import wintypes

    ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
    ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    ntdll.NtResumeProcess.restype = ctypes.c_long
    return int(ntdll.NtResumeProcess(handle)) >= 0


def _windows_handle_identity(handle: int) -> tuple[int, float, str] | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetProcessId.argtypes = [wintypes.HANDLE]
    kernel32.GetProcessId.restype = wintypes.DWORD
    kernel32.GetProcessTimes.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    pid = int(kernel32.GetProcessId(handle) or 0)
    creation = wintypes.FILETIME()
    exit_time = wintypes.FILETIME()
    kernel_time = wintypes.FILETIME()
    user_time = wintypes.FILETIME()
    if not pid or not kernel32.GetProcessTimes(
        handle,
        ctypes.byref(creation),
        ctypes.byref(exit_time),
        ctypes.byref(kernel_time),
        ctypes.byref(user_time),
    ):
        return None
    image_buffer = ctypes.create_unicode_buffer(32768)
    image_length = wintypes.DWORD(len(image_buffer))
    if not kernel32.QueryFullProcessImageNameW(
        handle, 0, image_buffer, ctypes.byref(image_length)
    ):
        return None
    creation_ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
    created_at = (creation_ticks - 116_444_736_000_000_000) / 10_000_000
    return pid, created_at, image_buffer.value


def _assign_exact_handle_to_kill_job(
    process_handle: int,
    final_authority: Callable[[], bool],
) -> int | None:
    import ctypes
    from ctypes import wintypes

    job_object_limit_kill_on_close = 0x00002000
    job_object_extended_limit_information = 9
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    class _BASIC(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_void_p),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _IO(ctypes.Structure):
        _fields_ = [
            ("ReadOperationCount", ctypes.c_uint64),
            ("WriteOperationCount", ctypes.c_uint64),
            ("OtherOperationCount", ctypes.c_uint64),
            ("ReadTransferCount", ctypes.c_uint64),
            ("WriteTransferCount", ctypes.c_uint64),
            ("OtherTransferCount", ctypes.c_uint64),
        ]

    class _EXTENDED(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BASIC),
            ("IoInfo", _IO),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = _EXTENDED()
    info.BasicLimitInformation.LimitFlags = job_object_limit_kill_on_close
    if not kernel32.SetInformationJobObject(
        job,
        job_object_extended_limit_information,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        kernel32.CloseHandle(job)
        return None
    try:
        if final_authority() is not True:
            kernel32.CloseHandle(job)
            return None
    except Exception:
        kernel32.CloseHandle(job)
        return None
    if not kernel32.AssignProcessToJobObject(job, process_handle):
        kernel32.CloseHandle(job)
        return None
    return int(job)


def _assign_kill_on_close_job(
    worker_id: int,
    process: subprocess.Popen,
    expected: BrowserProcessIdentity | None,
    reservation: _BrowserReservation | None,
) -> bool:
    """Assign the exact Popen handle to a kill-on-close job after full validation."""
    if platform.system() != "Windows":
        return True
    if expected is None or reservation is None:
        return False
    try:
        process_handle = _popen_process_handle(process)
        handle_identity = _windows_handle_identity(process_handle) if process_handle else None
        if (
            handle_identity is None
            or handle_identity[0] != expected.pid
            or abs(handle_identity[1] - expected.created_at) >= 0.001
            or _normalized_path(handle_identity[2]) != _normalized_path(expected.executable)
            or not _owned_browser_authority_is_current(process, expected, reservation)
        ):
            return False
        job = _assign_exact_handle_to_kill_job(
            process_handle,
            lambda: _owned_browser_authority_is_current(process, expected, reservation),
        )
        if not job:
            return False
        _job_handles[id(process)] = _OwnedJobHandle(worker_id, expected.pid, job)
        return True
    except Exception:
        logger.debug("[worker-%d] job-object assignment failed", worker_id, exc_info=True)
        return False


def _close_windows_job_handle(handle: int) -> bool:
    if platform.system() != "Windows":
        return True
    try:
        import ctypes

        return bool(ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle))
    except Exception:
        logger.debug("Closing browser job handle failed", exc_info=True)
        return False


def _abort_spawn(
    worker_id: int,
    process: subprocess.Popen,
    reservation: _BrowserReservation,
    guard: _SpawnGuard,
) -> bool:
    terminated = guard.terminate_and_reap()
    job_closed = _close_owned_job_handle(worker_id, process)
    with _chrome_lock:
        if _chrome_procs.get(worker_id) is process:
            _chrome_procs.pop(worker_id, None)
        _browser_reservations.pop(id(process), None)
    if terminated and job_closed:
        reservation.release(clear_identity=True)
        return True
    logger.error("[worker-%s] spawn abort could not prove child cleanup", worker_id)
    return False


def _close_owned_job_handle(worker_id: int, process: subprocess.Popen) -> bool:
    key = id(process)
    record = _job_handles.get(key)
    if record is None:
        return True
    if record.worker_id != worker_id or record.pid != process.pid:
        return False
    if not _close_windows_job_handle(record.handle):
        return False
    if _job_handles.get(key) is record:
        _job_handles.pop(key, None)
    return True


# ---------------------------------------------------------------------------
# Worker profile management
# ---------------------------------------------------------------------------

def _browser_profile_suffix(browser: str | None) -> str:
    """Profile-dir suffix per browser so Chrome and Edge workers don't share/corrupt a
    Chromium user-data dir. Chrome/default keep the bare worker-{id} (back-compat)."""
    name = (browser or "chrome").strip().lower()
    return "" if name in ("", "chrome", "google-chrome", "cft", "chromium", "default") else f"-{name}"


def setup_worker_profile(worker_id: int, browser: str | None = "chrome") -> Path:
    """Create an isolated browser profile for a worker.

    On first run, clones from an existing SAME-BROWSER worker profile (preferred,
    since it already has session cookies) or from the user's real profile for that
    browser (Chrome for chrome, Edge for edge -- so the worker inherits the logins/
    saved passwords from the matching real browser). Subsequent runs reuse it.

    Args:
        worker_id: Numeric worker identifier.
        browser: Browser name (chrome/edge/...) -- selects the clone source + dir.

    Returns:
        Path to the worker's user-data directory.
    """
    suffix = _browser_profile_suffix(browser)
    profile_dir = _worker_profile_dir(worker_id, browser)
    if (profile_dir / "Default").exists():
        return profile_dir  # Already initialized

    # Source priority: (1) the LinkedIn seed profile (carries the li_at session captured
    # by `applypilot linkedin-login`) for Chrome workers, (2) an existing SAME-BROWSER
    # worker (already has session cookies), (3) the user's real profile for this browser.
    source: Path | None = None
    seed = config.CHROME_WORKER_DIR / SEED_PROFILE_NAME
    if browser in (None, "", "chrome") and (seed / "Default").exists():
        source = seed
    if source is None:
        for wid in range(10):
            candidate = config.CHROME_WORKER_DIR / f"worker-{wid}{suffix}"
            if candidate != profile_dir and (candidate / "Default").exists():
                source = candidate
                break
    if source is None:
        source = config.get_browser_user_data(browser)

    # Offsite fleet / fresh container: there is no profile to clone (and no LinkedIn cookies
    # are needed for ATS applies). Launch with a clean, empty user-data-dir -- Chrome creates
    # the Default profile itself on first run. Without this guard, a missing source profile
    # (e.g. /root/.config/google-chrome in the container) crashes on source.iterdir().
    if source is None or not source.exists():
        logger.info("[worker-%d] No source profile (%s) -- using a fresh empty profile.",
                    worker_id, source)
        profile_dir.mkdir(parents=True, exist_ok=True)
        return profile_dir

    logger.info("[worker-%d] Copying Chrome profile from %s (first time setup)...",
                worker_id, source.name)
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Copy essential profile dirs -- skip caches and heavy transient data
    skip = {
        "ShaderCache", "GrShaderCache", "Service Worker", "Cache",
        "Code Cache", "GPUCache", "CacheStorage", "Crashpad",
        "BrowserMetrics", "SafeBrowsing", "Crowd Deny",
        "MEIPreload", "SSLErrorAssistant", "recovery", "Temp",
        "SingletonLock", "SingletonSocket", "SingletonCookie",
    }

    for item in source.iterdir():
        if item.name in skip:
            continue
        dst = profile_dir / item.name
        try:
            if item.is_dir():
                shutil.copytree(
                    str(item), str(dst), dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(
                        "Cache", "Code Cache", "GPUCache", "Service Worker",
                    ),
                )
            else:
                shutil.copy2(str(item), str(dst))
        except (PermissionError, OSError):
            pass  # skip locked files

    return profile_dir


def has_linkedin_session(profile_dir: Path) -> bool:
    """True if `profile_dir` holds a LinkedIn auth cookie (li_at) = a logged-in session.

    Reads a TEMP COPY of the Cookies DB to dodge Chrome's file lock, and checks only that
    the li_at row EXISTS -- never reads or decrypts the cookie value."""
    import os
    import sqlite3
    import tempfile
    for ck in (profile_dir / "Default" / "Network" / "Cookies",
               profile_dir / "Default" / "Cookies"):
        if not ck.exists():
            continue
        tmpdir = None
        try:
            # Copy the DB AND its WAL/SHM sidecars into a temp dir under the same base
            # name. Chrome's cookie DB runs in WAL mode, so a freshly-set li_at often
            # still lives in the -wal (not yet checkpointed into the main file) -- copying
            # only the main DB would miss it and falsely report "logged out". With the
            # sidecars present, SQLite replays the WAL and sees the new cookie.
            tmpdir = tempfile.mkdtemp(prefix="li_ck_")
            dest = os.path.join(tmpdir, "Cookies")
            shutil.copy2(str(ck), dest)
            for ext in ("-wal", "-shm"):
                side = Path(str(ck) + ext)
                if side.exists():
                    try:
                        shutil.copy2(str(side), dest + ext)
                    except OSError:
                        pass  # sidecar locked -> SQLite recovers from what's present
            con = sqlite3.connect(dest)
            try:
                n = con.execute(
                    "SELECT COUNT(*) FROM cookies "
                    "WHERE name='li_at' AND host_key LIKE '%linkedin%'"
                ).fetchone()[0]
            finally:
                con.close()
            if n > 0:
                return True
        except Exception:
            logger.debug("li_at check failed for %s", ck, exc_info=True)
        finally:
            if tmpdir and os.path.isdir(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
    return False


def _has_linkedin_session_cdp(port: int) -> bool:
    """True if the LIVE browser on `port` holds a li_at LinkedIn cookie.

    Reads cookies straight from the running Chrome over CDP (Playwright), so login is
    detected WHILE the window is open -- no Cookies-file lock, no need to close the window
    first (the lock was why the old file-only check forced a manual window close). Strictly
    read-only: it never opens a page and never closes the user's browser (the browser was
    launched externally, so we just disconnect on exit). Returns False on any error -- Chrome
    not yet listening, CDP unreachable -- so the caller falls back to the file-copy check."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=5000)
            # Do NOT call browser.close(): this Chrome was launched by us via Popen, not by
            # Playwright; closing it would terminate the user's login window mid-login.
            for ctx in browser.contexts:
                try:
                    for c in ctx.cookies():
                        if (c.get("name") == "li_at"
                                and "linkedin" in str(c.get("domain") or "").lower()):
                            return True
                except Exception:
                    continue
    except Exception:
        logger.debug("CDP li_at check failed on port %d", port, exc_info=True)
    return False


def linkedin_login(browser: str | None = "chrome", timeout_seconds: int = 420,
                   poll_seconds: float = 4.0):
    """Open a VISIBLE Chrome on the dedicated LinkedIn seed profile so the user logs in
    ONCE. Polls for the li_at auth cookie and returns (ok, seed_dir) when it appears (or
    on timeout). NEVER enters credentials -- the user logs in (and clears any 2FA/
    challenge) in the window. Apply workers then clone the seed and inherit the session.

    Returns (bool ok, Path seed_dir)."""
    enforce_no_lifecycle_faults()
    seed = config.CHROME_WORKER_DIR / SEED_PROFILE_NAME
    reservation = _reserve_browser_launch(
        LINKEDIN_LOGIN_SLOT,
        LINKEDIN_LOGIN_CDP_PORT,
        seed,
        kill_existing=False,
    )
    try:
        seed.mkdir(parents=True, exist_ok=True)
        chrome_exe = config.resolve_browser_path(browser)
        cmd = [
            chrome_exe,
            f"--remote-debugging-port={LINKEDIN_LOGIN_CDP_PORT}",
            f"--user-data-dir={seed}",
            "--profile-directory=Default",
            "--no-first-run", "--no-default-browser-check",
            "--window-size=1180,920",
            "--disable-session-crashed-bubble", "--hide-crash-restore-bubble", "--noerrdialogs",
            "https://www.linkedin.com/login",
        ]
        popen_kwargs = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if platform.system() == "Windows":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        proc = subprocess.Popen(cmd, **popen_kwargs)
    except Exception:
        reservation.release()
        raise
    guard = _acquire_spawn_guard(proc)
    if guard is None:
        raise RuntimeError("stable browser spawn guard unavailable; reservation retained")
    try:
        _record_launched_browser_identity(
            reservation,
            proc,
            chrome_exe,
            seed,
            LINKEDIN_LOGIN_CDP_PORT,
        )
    except Exception:
        if guard.terminate_and_reap():
            reservation.release(clear_identity=True)
        raise
    with _chrome_lock:
        _chrome_procs[LINKEDIN_LOGIN_SLOT] = proc
        _browser_reservations[id(proc)] = reservation
    if not _assign_kill_on_close_job(
        LINKEDIN_LOGIN_SLOT,
        proc,
        reservation.browser_identity,
        reservation,
    ):
        _abort_spawn(LINKEDIN_LOGIN_SLOT, proc, reservation, guard)
        raise RuntimeError("browser job assignment could not be verified")
    if platform.system() == "Windows" and not _resume_windows_process(guard.handle):
        _abort_spawn(LINKEDIN_LOGIN_SLOT, proc, reservation, guard)
        raise RuntimeError("suspended browser could not be resumed")
    guard.release()
    deadline = time.monotonic() + timeout_seconds
    ok = False

    def _wait_for_persisted_session(until: float) -> bool:
        while time.monotonic() < until:
            if has_linkedin_session(seed):
                return True
            time.sleep(max(poll_seconds, 0.25))
        return has_linkedin_session(seed)

    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:  # user closed the window -> file check (close flushes)
                ok = has_linkedin_session(seed)
                break
            if has_linkedin_session(seed):
                ok = True
                break
            if _has_linkedin_session_cdp(LINKEDIN_LOGIN_CDP_PORT):
                # CDP can see the in-memory cookie before Chrome has persisted it. Workers
                # clone the disk profile, so require the persisted cookie before success.
                flush_deadline = min(deadline, time.monotonic() + 20.0)
                ok = _wait_for_persisted_session(flush_deadline)
                break
            time.sleep(poll_seconds)
        else:
            ok = has_linkedin_session(seed)
    finally:
        # Cleanup uses only the verified stable process handle; CDP stays read-only.
        require_browser_cleanup(cleanup_worker, LINKEDIN_LOGIN_SLOT, proc)
    return ok, seed


def _suppress_restore_nag(profile_dir: Path) -> None:
    """Clear Chrome's 'restore pages' nag by fixing Preferences.

    Chrome writes exit_type=Crashed when killed, which triggers a
    'Restore pages?' prompt on next launch. This patches it out.
    """
    prefs_file = profile_dir / "Default" / "Preferences"
    if not prefs_file.exists():
        return

    try:
        prefs = json.loads(prefs_file.read_text(encoding="utf-8"))
        prefs.setdefault("profile", {})["exit_type"] = "Normal"
        prefs.setdefault("session", {})["restore_on_startup"] = 5  # 5 = open the New Tab Page (one blank tab)
        prefs["session"]["startup_urls"] = []
        # Pinned tabs reopen on EVERY launch regardless of the startup setting. A worker profile
        # cloned from the user's real Chrome inherits their pinned (e.g. job-search) tabs, so each
        # worker's browser opens with those extra tabs -- clear them for a clean single-tab start.
        prefs["pinned_tabs"] = []
        prefs["credentials_enable_service"] = False
        prefs.setdefault("password_manager", {})["saving_enabled"] = False
        prefs.setdefault("autofill", {})["profile_enabled"] = False
        prefs_file.write_text(json.dumps(prefs), encoding="utf-8")
    except Exception:
        logger.debug("Could not patch Chrome preferences", exc_info=True)


# ---------------------------------------------------------------------------
# Chrome launch / kill
# ---------------------------------------------------------------------------

def launch_chrome(worker_id: int, port: int | None = None,
                  headless: bool = False, browser: str | None = None,
                  kill_existing: bool = True) -> subprocess.Popen:
    """Launch a Chromium-family browser with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run headless (no visible window).
        browser: Browser name (chrome/edge/...) for per-worker browser assignment.
            Edge is Chromium too, so the same CDP flags apply. None -> default
            (get_chrome_path / CHROME_PATH).
        kill_existing: Preserve legacy zombie cleanup when True. When False,
            refuse any occupied slot/profile/port without terminating its owner.

    Returns:
        subprocess.Popen handle for the browser process.
    """
    enforce_no_lifecycle_faults()
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir = _worker_profile_dir(worker_id, browser)
    reservation = _reserve_browser_launch(
        worker_id,
        port,
        profile_dir,
        kill_existing=kill_existing,
    )
    try:
        profile_dir = setup_worker_profile(worker_id, browser)

        # Patch preferences to suppress restore nag
        _suppress_restore_nag(profile_dir)

        chrome_exe = config.resolve_browser_path(browser)

        cmd = [
            chrome_exe,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={profile_dir}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1024,768",
            "--disable-session-crashed-bubble",
            "--disable-features=InfiniteSessionRestore,PasswordManagerOnboarding",
            "--hide-crash-restore-bubble",
            "--noerrdialogs",
            "--password-store=basic",
            "--disable-save-password-bubble",
            "--disable-popup-blocking",
            # Block dangerous permissions at browser level
            "--use-fake-device-for-media-stream",
            "--use-fake-ui-for-media-stream",
            "--deny-permission-prompts",
            "--disable-notifications",
        ]
        if headless:
            cmd.append("--headless=new")
        import os as _os
        _no_sandbox = _os.environ.get("APPLYPILOT_CHROME_NO_SANDBOX", "").strip().lower() in {
            "1", "true", "yes", "on",
        }
        if platform.system() == "Linux" or _no_sandbox:
            # Cloud containers run as root with a tiny /dev/shm -- Chromium refuses to start
            # without these. Also REQUIRED on a Windows worker whose browser lives in a
            # restricted path the Chrome sandbox's locked-down token cannot read (its own
            # exe -> "Sandbox cannot access executable ... Access is denied"); such a box sets
            # APPLYPILOT_CHROME_NO_SANDBOX=1 (e.g. an install under C:\ApplyPilot). The home
            # box leaves it unset and keeps the sandbox.
            cmd += ["--no-sandbox", "--disable-dev-shm-usage"]

        # On Unix, start in a new process group so we can kill the whole tree
        kwargs: dict = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if platform.system() == "Windows":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        else:
            import os
            kwargs["preexec_fn"] = os.setsid

        proc = subprocess.Popen(cmd, **kwargs)
    except Exception:
        reservation.release()
        raise
    guard = _acquire_spawn_guard(proc)
    if guard is None:
        raise RuntimeError("stable browser spawn guard unavailable; reservation retained")
    try:
        _record_launched_browser_identity(reservation, proc, chrome_exe, profile_dir, port)
    except Exception:
        if guard.terminate_and_reap():
            reservation.release(clear_identity=True)
        raise
    with _chrome_lock:
        _chrome_procs[worker_id] = proc
        _browser_reservations[id(proc)] = reservation
    # Assign IMMEDIATELY (before Chrome forks much) so the tree dies with us even on a
    # hard external kill -- no lingering orphan to lock the profile.
    if not _assign_kill_on_close_job(
        worker_id, proc, reservation.browser_identity, reservation
    ):
        _abort_spawn(worker_id, proc, reservation, guard)
        raise RuntimeError("browser job assignment could not be verified")
    if platform.system() == "Windows" and not _resume_windows_process(guard.handle):
        _abort_spawn(worker_id, proc, reservation, guard)
        raise RuntimeError("suspended browser could not be resumed")
    guard.release()

    # Poll the CDP endpoint instead of a blind sleep(3): return as soon as Chrome's
    # debug port actually accepts connections (usually <1s when warm), and cap the
    # wait so a slow first-run profile clone can't false-start the agent (which then
    # connects to a not-yet-ready port and dies as no_result). Read-only probe.
    import os as _os
    import urllib.request as _urlreq
    deadline = time.time() + float(_os.environ.get("APPLYPILOT_CDP_READY_TIMEOUT") or 15)
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break  # Chrome exited; let the caller's run fail with diagnostics
        try:
            with _urlreq.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=1) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.2)
    if ready:
        logger.info("[worker-%d] Chrome CDP ready on port %d (pid %d)",
                    worker_id, port, proc.pid)
    else:
        logger.warning("[worker-%d] Chrome CDP NOT confirmed ready on port %d after wait (pid %d)",
                       worker_id, port, proc.pid)
    return proc


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> bool:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    with _chrome_lock:
        registered = _chrome_procs.get(worker_id)
        reservation = _browser_reservations.get(id(process)) if process is not None else None
        job_record = _job_handles.get(id(process)) if process is not None else None
        job_owned = (
            job_record is None
            or (
                job_record.worker_id == worker_id
                and process is not None
                and job_record.pid == process.pid
            )
        )
        owned = (
            process is not None
            and registered is process
            and reservation is not None
            and reservation.worker_id == worker_id
            and not reservation._released
            and job_owned
        )
    if not owned:
        logger.error("[worker-%s] Refusing cleanup for an unowned browser process", worker_id)
        return False

    if process.poll() is None:
        current_identity = _process_identity(process.pid)
        if not _browser_identity_matches(reservation.browser_identity, current_identity):
            logger.error("[worker-%s] Refusing cleanup after browser identity mismatch", worker_id)
            return False
        if not _kill_process_tree(process, reservation.browser_identity, reservation):
            logger.error("[worker-%s] Refusing cleanup after browser identity mismatch", worker_id)
            return False
    deadline = time.monotonic() + max(
        0.0,
        float(os.environ.get("APPLYPILOT_CHROME_CLEANUP_TIMEOUT") or 10),
    )
    while process and process.poll() is None and time.monotonic() < deadline:
        time.sleep(0.1)

    port = reservation.port
    process_gone = process.poll() is not None
    cleanup_ok = process_gone and not _port_is_listening(port)
    if cleanup_ok:
        cleanup_ok = _close_owned_job_handle(worker_id, process)
    if cleanup_ok:
        with _chrome_lock:
            if _chrome_procs.get(worker_id) is process:
                _chrome_procs.pop(worker_id, None)
            _browser_reservations.pop(id(process), None)
        reservation.release(clear_identity=True)
        logger.info("[worker-%d] Chrome cleaned up", worker_id)
    else:
        logger.error("[worker-%d] Chrome cleanup could not prove process and port release", worker_id)
    return cleanup_ok


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)

    for wid, proc in procs.items():
        cleanup_worker(wid, proc)


def reset_worker_dir(worker_id: int) -> Path:
    """Wipe and recreate a worker's isolated working directory.

    Each job gets a fresh working directory so that file conflicts
    (resume PDFs, MCP configs) don't bleed between jobs.

    Args:
        worker_id: Numeric worker identifier.

    Returns:
        Path to the clean worker directory.
    """
    worker_dir = config.APPLY_WORKER_DIR / f"worker-{worker_id}"
    if worker_dir.exists():
        shutil.rmtree(str(worker_dir), ignore_errors=True)
    worker_dir.mkdir(parents=True, exist_ok=True)
    return worker_dir


def cleanup_on_exit() -> None:
    """Atexit handler: clean only browsers owned and tracked by this process.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)

    for wid, proc in procs.items():
        cleanup_worker(wid, proc)
