"""Chrome lifecycle management for apply workers.

Handles launching an isolated Chrome instance with remote debugging,
worker profile setup/cloning, and cross-platform process cleanup.
"""

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from pathlib import Path

from applypilot import config

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

# Track Chrome processes per worker for cleanup
_chrome_procs: dict[int, subprocess.Popen] = {}
_chrome_lock = threading.Lock()

# Windows Job Object handles per worker. Each launched Chrome is assigned to a job with
# KILL_ON_JOB_CLOSE, so the whole browser tree dies when THIS python process dies for ANY
# reason (graceful exit, crash, OOM, or an external taskkill of the parent) -- not only on
# the graceful cleanup_worker path. This is what stops orphaned Chrome trees from lingering
# and locking worker profiles. Keeping the handle referenced here holds the job open; the
# OS closes it on process exit, which fires the kill.
_job_handles: dict[int, int] = {}


# ---------------------------------------------------------------------------
# Cross-platform process helpers
# ---------------------------------------------------------------------------

def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows, Chrome spawns 10+ child processes (GPU, renderer, etc.),
    so taskkill /T is needed to kill the entire tree. On Unix, os.killpg
    handles the process group.
    """
    import signal as _signal

    try:
        if platform.system() == "Windows":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
            )
        else:
            # Unix: kill entire process group
            import os
            try:
                os.killpg(os.getpgid(pid), _signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone or owned by another user
                try:
                    os.kill(pid, _signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
    except Exception:
        logger.debug("Failed to kill process tree for PID %d", pid, exc_info=True)


def _kill_on_port(port: int) -> None:
    """Kill any process listening on a specific port (zombie cleanup).

    Uses netstat on Windows, lsof on macOS/Linux.
    """
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=10,
            )
            for line in result.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    if pid.isdigit():
                        _kill_process_tree(int(pid))
        else:
            # macOS / Linux
            result = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=10,
            )
            for pid_str in result.stdout.strip().splitlines():
                pid_str = pid_str.strip()
                if pid_str.isdigit():
                    _kill_process_tree(int(pid_str))
    except FileNotFoundError:
        logger.debug("Port-kill tool not found (netstat/lsof) for port %d", port)
    except Exception:
        logger.debug("Failed to kill process on port %d", port, exc_info=True)


def _assign_kill_on_close_job(worker_id: int, pid: int) -> None:
    """Windows: put the launched browser in a Job Object that auto-kills the whole tree
    when this process dies for ANY reason -- crash, OOM, or an external taskkill of the
    parent -- not just the graceful cleanup_worker path. Without this, a parent killed from
    outside leaves an orphaned Chrome tree that lingers and locks the worker profile (the
    exact failure that stranded worker-80). No-op on non-Windows (launch_chrome already
    starts those in their own process group for group-kill).

    Best-effort: any failure is swallowed so a browser launch never breaks over it.
    """
    if platform.system() != "Windows":
        return
    try:
        import ctypes
        from ctypes import wintypes

        JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
        JobObjectExtendedLimitInformation = 9
        PROCESS_TERMINATE = 0x0001
        PROCESS_SET_QUOTA = 0x0100

        # restype/argtypes MUST be set: HANDLE is 64-bit on x64 and ctypes defaults to a
        # 32-bit int return, which truncates the handle and silently breaks everything.
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                                wintypes.LPVOID, wintypes.DWORD]
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]

        class _BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64),
                        ("PerJobUserTimeLimit", ctypes.c_int64),
                        ("LimitFlags", wintypes.DWORD),
                        ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t),
                        ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_void_p),
                        ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class _IO(ctypes.Structure):
            _fields_ = [("ReadOperationCount", ctypes.c_uint64),
                        ("WriteOperationCount", ctypes.c_uint64),
                        ("OtherOperationCount", ctypes.c_uint64),
                        ("ReadTransferCount", ctypes.c_uint64),
                        ("WriteTransferCount", ctypes.c_uint64),
                        ("OtherTransferCount", ctypes.c_uint64)]

        class _EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", _BASIC),
                        ("IoInfo", _IO),
                        ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        job = k32.CreateJobObjectW(None, None)
        if not job:
            return
        info = _EXTENDED()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, JobObjectExtendedLimitInformation,
                                           ctypes.byref(info), ctypes.sizeof(info)):
            k32.CloseHandle(job)
            return
        h_proc = k32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not h_proc:
            k32.CloseHandle(job)
            return
        assigned = k32.AssignProcessToJobObject(job, h_proc)
        k32.CloseHandle(h_proc)
        if not assigned:
            k32.CloseHandle(job)
            return
        # Hold the job handle open for this process's lifetime. Renderers Chrome spawns
        # AFTER assignment inherit the job; the main process (port + profile-lock holder)
        # is covered, which is what prevents the lingering-lock orphan.
        _job_handles[worker_id] = job
    except Exception:
        logger.debug("[worker-%d] job-object assignment failed", worker_id, exc_info=True)


def _close_job(worker_id: int) -> None:
    """Close a worker's job handle (fires KILL_ON_JOB_CLOSE if the tree is still alive)."""
    job = _job_handles.pop(worker_id, None)
    if job and platform.system() == "Windows":
        try:
            import ctypes
            ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(job)
        except Exception:
            logger.debug("[worker-%d] closing job handle failed", worker_id, exc_info=True)


def _close_all_jobs() -> None:
    """Close every tracked job handle (shutdown sweep)."""
    for wid in list(_job_handles.keys()):
        _close_job(wid)


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
    profile_dir = config.CHROME_WORKER_DIR / f"worker-{worker_id}{suffix}"
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


def _close_browser_cdp(port: int) -> bool:
    """Ask the login Chrome to close cleanly so profile data is flushed to disk."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return False
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=5000)
            browser.close()
            return True
    except Exception:
        logger.debug("CDP browser close failed on port %d", port, exc_info=True)
        return False


def linkedin_login(browser: str | None = "chrome", timeout_seconds: int = 420,
                   poll_seconds: float = 4.0):
    """Open a VISIBLE Chrome on the dedicated LinkedIn seed profile so the user logs in
    ONCE. Polls for the li_at auth cookie and returns (ok, seed_dir) when it appears (or
    on timeout). NEVER enters credentials -- the user logs in (and clears any 2FA/
    challenge) in the window. Apply workers then clone the seed and inherit the session.

    Returns (bool ok, Path seed_dir)."""
    seed = config.CHROME_WORKER_DIR / SEED_PROFILE_NAME
    seed.mkdir(parents=True, exist_ok=True)
    _kill_on_port(LINKEDIN_LOGIN_CDP_PORT)
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
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + timeout_seconds
    ok = False

    def _wait_for_persisted_session(until: float) -> bool:
        while time.time() < until:
            if has_linkedin_session(seed):
                return True
            time.sleep(max(poll_seconds, 0.25))
        return has_linkedin_session(seed)

    try:
        while time.time() < deadline:
            if proc.poll() is not None:  # user closed the window -> file check (close flushes)
                ok = has_linkedin_session(seed)
                break
            if has_linkedin_session(seed):
                ok = True
                break
            if _has_linkedin_session_cdp(LINKEDIN_LOGIN_CDP_PORT):
                # CDP can see the in-memory cookie before Chrome has persisted it. Workers
                # clone the disk profile, so require the persisted cookie before success.
                flush_deadline = min(deadline, time.time() + 20.0)
                ok = _wait_for_persisted_session(flush_deadline)
                if not ok:
                    _close_browser_cdp(LINKEDIN_LOGIN_CDP_PORT)
                    try:
                        proc.wait(timeout=15)
                    except (AttributeError, subprocess.TimeoutExpired, OSError):
                        pass
                    ok = _wait_for_persisted_session(min(deadline, time.time() + 5.0))
                break
            time.sleep(poll_seconds)
        else:
            ok = has_linkedin_session(seed)
    finally:
        # Close the seed Chrome so the profile is unlocked for workers to clone.
        if proc.poll() is None:
            _close_browser_cdp(LINKEDIN_LOGIN_CDP_PORT)
            try:
                proc.wait(timeout=15)
            except (AttributeError, subprocess.TimeoutExpired, OSError):
                pass
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
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
                  headless: bool = False, browser: str | None = None) -> subprocess.Popen:
    """Launch a Chromium-family browser with remote debugging for a worker.

    Args:
        worker_id: Numeric worker identifier.
        port: CDP port. Defaults to BASE_CDP_PORT + worker_id.
        headless: Run headless (no visible window).
        browser: Browser name (chrome/edge/...) for per-worker browser assignment.
            Edge is Chromium too, so the same CDP flags apply. None -> default
            (get_chrome_path / CHROME_PATH).

    Returns:
        subprocess.Popen handle for the browser process.
    """
    if port is None:
        port = BASE_CDP_PORT + worker_id

    profile_dir = setup_worker_profile(worker_id, browser)

    # Kill any zombie browser from a previous run on this port
    _kill_on_port(port)

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
    if platform.system() != "Windows":
        import os
        kwargs["preexec_fn"] = os.setsid

    proc = subprocess.Popen(cmd, **kwargs)
    with _chrome_lock:
        _chrome_procs[worker_id] = proc
    # Assign IMMEDIATELY (before Chrome forks much) so the tree dies with us even on a
    # hard external kill -- no lingering orphan to lock the profile.
    _assign_kill_on_close_job(worker_id, proc.pid)

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


def cleanup_worker(worker_id: int, process: subprocess.Popen | None) -> None:
    """Kill a worker's Chrome instance and remove it from tracking.

    Args:
        worker_id: Numeric worker identifier.
        process: The Popen handle returned by launch_chrome.
    """
    if process and process.poll() is None:
        _kill_process_tree(process.pid)
    _close_job(worker_id)
    with _chrome_lock:
        _chrome_procs.pop(worker_id, None)
    logger.info("[worker-%d] Chrome cleaned up", worker_id)


def kill_all_chrome() -> None:
    """Kill all Chrome instances and any port zombies.

    Called during graceful shutdown to ensure no orphan Chrome processes.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port in case of zombies
    _kill_on_port(BASE_CDP_PORT)
    _close_all_jobs()


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
    """Atexit handler: kill all Chrome processes and sweep CDP ports.

    Register this with atexit.register() at application startup.
    """
    with _chrome_lock:
        procs = dict(_chrome_procs)
        _chrome_procs.clear()

    for wid, proc in procs.items():
        if proc.poll() is None:
            _kill_process_tree(proc.pid)
        _kill_on_port(BASE_CDP_PORT + wid)

    # Sweep base port for any orphan
    _kill_on_port(BASE_CDP_PORT)
    _close_all_jobs()
