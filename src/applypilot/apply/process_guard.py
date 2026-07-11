"""Stable ownership for subprocesses spawned directly by ApplyPilot."""

from __future__ import annotations

import os
import platform
import signal
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime


def parse_ps_lstart_local(value: str) -> float:
    """Convert ps(1) local wall time to epoch using host timezone and DST rules."""
    parsed = datetime.strptime(value, "%a %b %d %H:%M:%S %Y")
    return float(time.mktime(parsed.timetuple()))


def darwin_process_executable(pid: int) -> str:
    """Resolve a Darwin executable path without relying on whitespace parsing."""
    try:
        import ctypes

        libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
        libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
        libproc.proc_pidpath.restype = ctypes.c_int
        buffer = ctypes.create_string_buffer(4096)
        length = int(libproc.proc_pidpath(int(pid), buffer, len(buffer)))
        if length <= 0:
            return ""
        return buffer.value.decode("utf-8", errors="strict")
    except (AttributeError, OSError, TypeError, UnicodeError, ValueError):
        return ""


def emergency_cleanup_direct_child(
    process: subprocess.Popen,
    timeout: float = 20,
) -> bool:
    """Terminate and reap this parent's unreaped direct child after guard failure."""
    try:
        if platform.system() == "Windows":
            try:
                handle = int(process._handle)
            except (AttributeError, TypeError, ValueError):
                return False
            terminated = bool(handle) and _terminate_windows_handle(handle, process.pid)
            if not terminated:
                # The stable Popen handle still proves which direct child wait() observes.
                process.wait(timeout=timeout)
                return True
        else:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        process.wait(timeout=timeout)
        return True
    except Exception:
        return False


@dataclass
class SpawnedChildGuard:
    process: subprocess.Popen
    kind: str
    handle: int
    released: bool = False

    @classmethod
    def acquire(cls, process: subprocess.Popen) -> "SpawnedChildGuard | None":
        if process.poll() is not None:
            return cls(process, "completed", 0)
        system = platform.system()
        if system == "Windows":
            try:
                handle = int(process._handle)
            except (AttributeError, TypeError, ValueError):
                return None
            return cls(process, "windows", handle) if handle else None
        if system == "Linux":
            opener = getattr(os, "pidfd_open", None)
            if opener is None:
                return None
            try:
                return cls(process, "pidfd", opener(process.pid, 0))
            except (OSError, ValueError):
                return None
        if system == "Darwin":
            return cls(process, "darwin-child", 0)
        return None

    def release(self) -> None:
        if self.released:
            return
        self.released = True
        if self.kind == "pidfd":
            try:
                os.close(self.handle)
            except OSError:
                pass

    def terminate_and_reap(self, timeout: float = 20) -> bool:
        if self.released:
            return self.process.poll() is not None
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
                elif self.kind == "completed":
                    pass
                else:
                    return False
            self.process.wait(timeout=timeout)
            return self.process.poll() is not None
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        finally:
            self.release()


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
