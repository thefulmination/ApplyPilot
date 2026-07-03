# Mac Offsite-Apply Fleet Worker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a macOS machine (different LAN, variable uptime) as an opportunistic offsite-ATS apply worker: launchd-supervised, Tailscale to the home fleet Postgres, self-updating from the private repo, graceful SIGTERM drain so updates never interrupt a live apply.

**Architecture:** The existing `applypilot-fleet-apply` Python entrypoint is already cross-platform; we add (1) a SIGTERM finish-current-job handler, (2) a platform guard for one Windows-only cleanup call, (3) a least-privilege `fleet_worker` PG role + home-box hardening script, (4) macOS setup/wrapper/launchd scripts, (5) an owner runbook. Spec: `docs/superpowers/specs/2026-07-03-mac-fleet-worker-design.md`.

**Tech Stack:** Python 3.11+ (psycopg3, pytest with the repo's `fleet_db` disposable-Postgres fixture), bash (macOS /bin/bash 3.2-compatible), launchd, PowerShell (home box), Tailscale, GitHub deploy keys.

## Global Constraints

- **Repo:** all work in `C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot`, on the CURRENT branch `applypilot-hardening-and-brainstorm-integration`. The working tree has UNRELATED uncommitted changes (resbuild-bridge files) — **never `git add -A` / `git add .`**; stage only files this plan names.
- **Fleet DB:** name `applypilot_fleet`; remote role `fleet_worker`; DSN keyword format exactly `host=<ip> port=5432 dbname=applypilot_fleet user=fleet_worker connect_timeout=5` (password via pgpass, never in the DSN). PostgreSQL 15+ assumed (home box runs PG 18).
- **Worker identity:** worker-id = `<Label>-<Slot>` → `mac-0` (trailing digit auto-derives the Chrome slot).
- **LinkedIn exclusion:** no Mac artifact may reference `applypilot-fleet-linkedin`, `linkedin_queue`, or lane-B code. The Mac runs ONLY `applypilot-fleet-apply`.
- **Secrets:** API keys and the PG password are prompted interactively (`read -rs` / `Read-Host -AsSecureString`), land only in `chmod 600` files on the target machine, and are NEVER committed, echoed, or written to any file in this repo.
- **macOS bash compat:** macOS ships bash 3.2 — no `wait -n`, no `declare -A`, no `${var,,}`.
- **Python tests:** run with `& .\.conda-env\python.exe -m pytest <file> -v` from the repo root (PowerShell). PG-backed tests use the `fleet_db` fixture (tests/conftest.py:97) which needs the `applypilot-pgtest` conda env; if a PG test errors with "pg binaries not found", report it — do not delete the test.
- **Commit style:** one commit per task, message prefix `feat(mac-worker):` / `fix(mac-worker):` / `docs(mac-worker):`, ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: Graceful SIGTERM stop in the apply worker

The wrapper (Task 5) and launchd send SIGTERM to restart the worker for updates. Today the worker has NO signal handling — a mid-apply kill produces a `crash_unconfirmed` "may-have-submitted" row (the double-apply vector). Add a stop flag: SIGTERM lets the CURRENT job finish, then the loop exits before the next lease. SIGINT (Ctrl+C) keeps its default abort behavior.

**Files:**
- Modify: `src/applypilot/fleet/apply_worker_main.py` (imports block; `run_apply` at line 210; `main` at line 251)
- Test: `tests/test_fleet_apply_graceful_stop.py` (new)

**Interfaces:**
- Produces: `apply_worker_main.request_stop(signum=None, frame=None)`, `apply_worker_main.stop_requested() -> bool`, `apply_worker_main.install_stop_handler() -> None`, module event `apply_worker_main._STOP_REQUESTED` (threading.Event). Task 5's wrapper relies on `kill -TERM <worker-pid>` = drain-then-exit.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fleet_apply_graceful_stop.py`:

```python
"""SIGTERM graceful stop: the launchd/wrapper update path sends SIGTERM; the worker must
finish the CURRENT job and exit before the next lease (a mid-apply kill parks the job
crash_unconfirmed = the 'may-have-submitted' double-apply vector)."""
from unittest.mock import MagicMock

from applypilot.fleet import apply_worker_main as awm


class _StubCtx:
    def __enter__(self):
        return MagicMock()

    def __exit__(self, *a):
        return False


def _conn_factory():
    return _StubCtx()


def test_stop_flag_set_by_handler():
    awm._STOP_REQUESTED.clear()
    assert not awm.stop_requested()
    awm.request_stop()  # exactly what the signal handler invokes
    assert awm.stop_requested()
    awm._STOP_REQUESTED.clear()


def test_run_apply_finishes_current_job_then_exits(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)
    calls = {"n": 0}
    loop = MagicMock()

    def run_once():
        calls["n"] += 1
        awm.request_stop()  # SIGTERM lands mid-job
        return {"action": "applied"}

    loop.run_once = run_once
    counts = awm.run_apply(_conn_factory, loop, max_iterations=None, idle_sleep=0)
    assert calls["n"] == 1  # current job completed; NO second lease
    assert counts["applied"] == 1
    awm._STOP_REQUESTED.clear()


def test_run_apply_exits_immediately_if_stop_already_requested(monkeypatch):
    awm._STOP_REQUESTED.clear()
    monkeypatch.setattr("applypilot.apply.pgqueue.ats_should_halt", lambda conn: False)
    loop = MagicMock()
    awm.request_stop()
    counts = awm.run_apply(_conn_factory, loop, max_iterations=None, idle_sleep=0)
    loop.run_once.assert_not_called()
    assert counts == {"applied": 0, "halted": 0, "idle": 0, "error": 0}
    awm._STOP_REQUESTED.clear()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& .\.conda-env\python.exe -m pytest tests\test_fleet_apply_graceful_stop.py -v`
Expected: FAIL — all three tests error with `AttributeError: module 'applypilot.fleet.apply_worker_main' has no attribute '_STOP_REQUESTED'` (the AttributeError fires before `run_apply` is entered, so nothing hangs).

- [ ] **Step 3: Implement the stop flag**

In `src/applypilot/fleet/apply_worker_main.py`:

(a) Add to the imports block at the top of the file (keep existing imports untouched):

```python
import signal
import threading
```

(b) Below the module logger definition, add:

```python
# --- graceful stop (SIGTERM) -------------------------------------------------
# The macOS launchd wrapper (run-worker-mac.sh) and `launchctl unload` send SIGTERM to
# restart the worker for a code update. Mid-apply death parks the job crash_unconfirmed
# ("may-have-submitted"), so instead: SIGTERM sets a flag, the CURRENT job finishes, and
# run_apply exits before the next lease. SIGINT (Ctrl+C) keeps default abort behavior.
_STOP_REQUESTED = threading.Event()


def request_stop(signum=None, frame=None) -> None:
    _STOP_REQUESTED.set()


def stop_requested() -> bool:
    return _STOP_REQUESTED.is_set()


def install_stop_handler() -> None:
    try:
        signal.signal(signal.SIGTERM, request_stop)
    except (ValueError, OSError):  # pragma: no cover - non-main thread / exotic platform
        pass
```

(c) In `run_apply` (line 210), change the while condition from:

```python
    while max_iterations is None or it < max_iterations:
```

to:

```python
    while not _STOP_REQUESTED.is_set() and (max_iterations is None or it < max_iterations):
```

and immediately after the loop (before `return counts`) add:

```python
    if _STOP_REQUESTED.is_set():
        logger.info("stop requested (SIGTERM); exiting after current job, before next lease")
```

(d) In `main` (line 251), after `args = p.parse_args(argv)` and the `if not args.dsn:` guard, add:

```python
    install_stop_handler()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& .\.conda-env\python.exe -m pytest tests\test_fleet_apply_graceful_stop.py -v`
Expected: 3 passed.

- [ ] **Step 5: Regression-check the apply lane tests**

Run: `& .\.conda-env\python.exe -m pytest tests\test_fleet_apply_lane.py tests\test_fleet_apply_e2e.py -q`
Expected: all pass (skips are fine if the pgtest env is unavailable — report if so).

- [ ] **Step 6: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add src/applypilot/fleet/apply_worker_main.py tests/test_fleet_apply_graceful_stop.py
git commit -m "feat(mac-worker): SIGTERM graceful stop - finish current job, exit before next lease

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Platform guard for the supervisor's orphan cleanup

`src/applypilot/apply/supervisor.py:77-85` invokes `powershell` unconditionally to kill orphaned Playwright-MCP node processes — DOA on macOS. Factor the command into a per-platform helper (`pkill -f` on POSIX).

**Files:**
- Modify: `src/applypilot/apply/supervisor.py:66-85` (`_cleanup_orphans`)
- Test: `tests/test_supervisor_orphan_cleanup.py` (new)

**Interfaces:**
- Produces: `supervisor._orphan_kill_cmd() -> list[str]` (pure function; platform-switched on `sys.platform`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_supervisor_orphan_cleanup.py`:

```python
"""The orphan Playwright-MCP cleanup must never invoke PowerShell on POSIX (macOS fleet
worker) and must keep the existing PowerShell path on Windows."""
import sys

from applypilot.apply import supervisor


def test_orphan_kill_cmd_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    cmd = supervisor._orphan_kill_cmd()
    assert cmd[0] == "powershell"
    assert "_npx|playwright|modelcontextprotocol|@playwright" in " ".join(cmd)


def test_orphan_kill_cmd_posix_uses_pkill(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    cmd = supervisor._orphan_kill_cmd()
    assert cmd == ["pkill", "-f", "_npx|playwright|modelcontextprotocol|@playwright"]
    assert "powershell" not in " ".join(cmd)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& .\.conda-env\python.exe -m pytest tests\test_supervisor_orphan_cleanup.py -v`
Expected: FAIL with `AttributeError: module 'applypilot.apply.supervisor' has no attribute '_orphan_kill_cmd'`.

- [ ] **Step 3: Implement the helper and use it**

In `src/applypilot/apply/supervisor.py`, add above `_cleanup_orphans` (line 66):

```python
_ORPHAN_PATTERN = "_npx|playwright|modelcontextprotocol|@playwright"


def _orphan_kill_cmd() -> list[str]:
    """Platform command to kill orphaned Playwright-MCP node servers. Matched by command
    line so the desktop app / unrelated node processes are never touched."""
    if sys.platform == "win32":
        return ["powershell", "-NoProfile", "-Command",
                "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
                f"Where-Object {{ $_.CommandLine -match '{_ORPHAN_PATTERN}' }} | "
                "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"]
    return ["pkill", "-f", _ORPHAN_PATTERN]
```

Then replace the body of the second `try:` block in `_cleanup_orphans` (lines 76-85) with:

```python
    try:
        subprocess.run(
            _orphan_kill_cmd(),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
    except Exception:
        pass
```

(`sys` is already imported at supervisor.py:25; keep the existing comment above the block.)

- [ ] **Step 4: Run test to verify it passes**

Run: `& .\.conda-env\python.exe -m pytest tests\test_supervisor_orphan_cleanup.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add src/applypilot/apply/supervisor.py tests/test_supervisor_orphan_cleanup.py
git commit -m "fix(mac-worker): platform-guard orphan MCP cleanup (pkill on POSIX, powershell on win32)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Least-privilege `fleet_worker` PG role helper

Remote machines currently connect as `postgres` (superuser). The Mac gets a dedicated role: LOGIN + DML on the fleet tables, nothing else. Implemented as a tested Python helper so the home-box script (Task 4) is a thin caller.

**Files:**
- Create: `src/applypilot/fleet/pg_roles.py`
- Test: `tests/test_fleet_pg_roles.py` (new; uses the `fleet_db` fixture from tests/conftest.py:97)

**Interfaces:**
- Consumes: `applypilot.apply.pgqueue.connect(dsn)` (existing).
- Produces: `pg_roles.ensure_fleet_worker_role(conn, password: str, *, role: str = "fleet_worker") -> None` — idempotent; re-run rotates the password. Task 4's script calls it via `python -c`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_fleet_pg_roles.py`:

```python
"""fleet_worker role: remote (Tailscale) workers get DML on the fleet tables and nothing
else — no superuser, no DDL. Exercises the EXACT tables the apply worker writes."""
import psycopg
import pytest

from applypilot.apply import pgqueue
from applypilot.fleet import pg_roles


def _worker_dsn(fleet_db: str) -> str:
    # fleet_db is postgresql://postgres@127.0.0.1:<port>/postgres (trust auth locally)
    return fleet_db.replace("postgres@", "fleet_worker@", 1)


def test_role_can_dml_fleet_tables(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        pg_roles.ensure_fleet_worker_role(conn, "test-pw-1")
    with psycopg.connect(_worker_dsn(fleet_db)) as wconn:
        with wconn.cursor() as cur:
            cur.execute("SELECT paused FROM fleet_config WHERE id = 1")
            assert cur.fetchone() is not None
            cur.execute("UPDATE fleet_config SET paused = paused WHERE id = 1")
            cur.execute(
                "INSERT INTO worker_heartbeat (worker_id, machine_owner, home_ip, role, state, last_beat) "
                "VALUES ('mac-test', 't', '0.0.0.0', 'apply', 'idle', now())")
        wconn.commit()


def test_role_cannot_ddl(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        pg_roles.ensure_fleet_worker_role(conn, "test-pw-1")
    with psycopg.connect(_worker_dsn(fleet_db)) as wconn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with wconn.cursor() as cur:
                cur.execute("CREATE TABLE mac_worker_should_fail (id int)")


def test_rerun_is_idempotent_password_rotation(fleet_db):
    with pgqueue.connect(fleet_db) as conn:
        pg_roles.ensure_fleet_worker_role(conn, "pw-old")
        pg_roles.ensure_fleet_worker_role(conn, "pw-new")  # must not raise
    with psycopg.connect(_worker_dsn(fleet_db)) as wconn:  # role still connects + works
        with wconn.cursor() as cur:
            cur.execute("SELECT 1 FROM fleet_config WHERE id = 1")
            assert cur.fetchone() == (1,)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& .\.conda-env\python.exe -m pytest tests\test_fleet_pg_roles.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'applypilot.fleet.pg_roles'`.

- [ ] **Step 3: Implement the helper**

Create `src/applypilot/fleet/pg_roles.py`:

```python
"""Least-privilege PG role for REMOTE fleet workers (the Mac / any offsite box).

The home box connects as `postgres` (superuser, local pgpass). Remote workers connect
as `fleet_worker` instead: LOGIN + DML on the fleet tables in the CURRENT database —
no superuser, no DDL, no CREATEROLE, no other databases. Applied idempotently by the
home-box hardening script (setup-fleet-pg-tailscale.ps1); re-running with a new
password rotates the credential (the remote kill switch)."""
from __future__ import annotations

from psycopg import sql

DEFAULT_ROLE = "fleet_worker"

_GRANTS = (
    "GRANT CONNECT ON DATABASE {db} TO {role}",
    "GRANT USAGE ON SCHEMA public TO {role}",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {role}",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {role}",
    # Tables the superuser creates LATER (schema migrations) stay usable without re-running:
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {role}",
    "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO {role}",
)


def ensure_fleet_worker_role(conn, password: str, *, role: str = DEFAULT_ROLE) -> None:
    """Idempotently create/refresh the remote-worker role on conn's CURRENT database."""
    r = sql.Identifier(role)
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        verb = "ALTER" if cur.fetchone() else "CREATE"
        # CREATE/ALTER ROLE are utility statements: no server-side params -> sql.Literal.
        cur.execute(sql.SQL(
            f"{verb} ROLE {{}} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {{}}"
        ).format(r, sql.Literal(password)))
        cur.execute("SELECT current_database()")
        db = sql.Identifier(cur.fetchone()[0])
        for stmt in _GRANTS:
            cur.execute(sql.SQL(stmt.replace("{db}", "{0}").replace("{role}", "{1}")).format(db, r))
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `& .\.conda-env\python.exe -m pytest tests\test_fleet_pg_roles.py -v`
Expected: 3 passed. (If `worker_heartbeat` INSERT fails on a NOT NULL column, add that column with a dummy value to the INSERT in the test — mirror the column list of `_heartbeat` at src/applypilot/fleet/worker.py:142.)

- [ ] **Step 5: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add src/applypilot/fleet/pg_roles.py tests/test_fleet_pg_roles.py
git commit -m "feat(mac-worker): least-privilege fleet_worker PG role (DML-only, idempotent, rotatable)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Home-box hardening script `setup-fleet-pg-tailscale.ps1`

One-time, owner-run (elevated) on the home box: create the role, open pg_hba for the tailnet only, verify listen_addresses, firewall 5432 to the tailnet CIDR, print the Mac-side DSN + pgpass line. Follows the repo-root `.ps1` conventions (param block, `$ErrorActionPreference = "Stop"`, python resolution like run-fleet-worker.ps1:26-31).

**Files:**
- Create: `setup-fleet-pg-tailscale.ps1` (repo root)

**Interfaces:**
- Consumes: `pg_roles.ensure_fleet_worker_role` (Task 3), local superuser DSN `host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5`.
- Produces: a running-config change only; prints the remote DSN for Task 6's setup prompts.

- [ ] **Step 1: Write the script**

Create `setup-fleet-pg-tailscale.ps1`:

```powershell
# setup-fleet-pg-tailscale.ps1 [-Role fleet_worker] [-TailnetCidr 100.64.0.0/10] [-Db applypilot_fleet]
#   HOME BOX one-time hardening so a REMOTE (Tailscale) machine can join the apply fleet
#   WITHOUT the postgres superuser credential:
#     1. create/refresh the least-privilege role (prompts for its password; re-run = rotate)
#     2. pg_hba.conf: allow ONLY the tailnet range, this db, this role, scram-sha-256
#     3. verify listen_addresses covers the Tailscale interface
#     4. Windows Firewall: TCP 5432 inbound from the tailnet CIDR only
#     5. print the DSN + ~/.pgpass line to enter on the Mac during setup-mac-worker.sh
#   RUN ELEVATED (firewall rule). Requires Tailscale up and local pgpass superuser access.
param(
  [string]$Role = "fleet_worker",
  [string]$TailnetCidr = "100.64.0.0/10",
  [string]$Db = "applypilot_fleet"
)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# Python env: home box uses .conda-env; a bootstrapped machine uses .venv (same as run-fleet-worker.ps1).
$py = $null
foreach ($d in @(".\.conda-env", ".\.venv\Scripts")) {
  $cand = Join-Path $d "python.exe"
  if (Test-Path $cand) { $py = (Resolve-Path $cand).Path; break }
}
if (-not $py) { throw "python not found in .conda-env or .venv" }
$SuperDsn = "host=localhost port=5432 dbname=$Db user=postgres connect_timeout=5"

# 0. This box's tailnet address (the host the Mac will dial).
$tsIp = (& tailscale ip -4 2>$null | Select-Object -First 1)
if (-not $tsIp) { throw "Tailscale is not running. Install + sign in first: https://tailscale.com/download" }
Write-Host "[pg-tailscale] home box tailnet address: $tsIp"

# 1. Role (password prompted; passed to python via env so it never appears in argv).
$sec = Read-Host -AsSecureString "New password for PG role '$Role' (re-running rotates it)"
$env:APPLYPILOT_PG_ROLE_PW = [Runtime.InteropServices.Marshal]::PtrToStringUni(
  [Runtime.InteropServices.Marshal]::SecureStringToGlobalAllocUnicode($sec))
$env:APPLYPILOT_PG_ROLE = $Role
$env:APPLYPILOT_SUPER_DSN = $SuperDsn
& $py -c "import os; from applypilot.apply import pgqueue; from applypilot.fleet import pg_roles; conn = pgqueue.connect(os.environ['APPLYPILOT_SUPER_DSN']); pg_roles.ensure_fleet_worker_role(conn, os.environ['APPLYPILOT_PG_ROLE_PW'], role=os.environ['APPLYPILOT_PG_ROLE']); conn.close(); print('[pg-tailscale] role ensured')"
if ($LASTEXITCODE -ne 0) { throw "role creation failed" }

# 2. pg_hba.conf: tailnet-only rule (idempotent append).
$hba = (& $py -c "import os; from applypilot.apply import pgqueue; conn = pgqueue.connect(os.environ['APPLYPILOT_SUPER_DSN']); cur = conn.cursor(); cur.execute('SHOW hba_file'); print(cur.fetchone()[0]); conn.close()").Trim()
$rule = "host    $Db    $Role    $TailnetCidr    scram-sha-256"
$hbaText = Get-Content $hba -Raw
if ($hbaText -notmatch [regex]::Escape($TailnetCidr)) {
  Add-Content -Path $hba -Value "`n# ApplyPilot remote fleet workers (Tailscale only)`n$rule"
  Write-Host "[pg-tailscale] pg_hba rule appended: $rule"
} else {
  Write-Host "[pg-tailscale] pg_hba already has a $TailnetCidr rule; left as-is"
}
& $py -c "import os; from applypilot.apply import pgqueue; conn = pgqueue.connect(os.environ['APPLYPILOT_SUPER_DSN']); cur = conn.cursor(); cur.execute('SELECT pg_reload_conf()'); conn.close(); print('[pg-tailscale] config reloaded')"

# 3. listen_addresses must cover the tailnet interface ('*' does).
$listen = (& $py -c "import os; from applypilot.apply import pgqueue; conn = pgqueue.connect(os.environ['APPLYPILOT_SUPER_DSN']); cur = conn.cursor(); cur.execute('SHOW listen_addresses'); print(cur.fetchone()[0]); conn.close()").Trim()
if ($listen -ne "*" -and $listen -notmatch [regex]::Escape($tsIp)) {
  Write-Warning "listen_addresses='$listen' does not cover $tsIp. Edit postgresql.conf to 'listen_addresses = ''*''' (or add $tsIp) and RESTART the PostgreSQL service."
} else {
  Write-Host "[pg-tailscale] listen_addresses='$listen' OK"
}

# 4. Firewall: 5432 from the tailnet only (idempotent).
if (-not (Get-NetFirewallRule -DisplayName "ApplyPilot PG (tailnet)" -ErrorAction SilentlyContinue)) {
  New-NetFirewallRule -DisplayName "ApplyPilot PG (tailnet)" -Direction Inbound -Protocol TCP `
    -LocalPort 5432 -RemoteAddress $TailnetCidr -Action Allow | Out-Null
  Write-Host "[pg-tailscale] firewall rule added (TCP 5432 from $TailnetCidr)"
} else {
  Write-Host "[pg-tailscale] firewall rule already present"
}

# 5. What to enter on the Mac.
$env:APPLYPILOT_PG_ROLE_PW = ""
Write-Host ""
Write-Host "=== Mac setup values (setup-mac-worker.sh will prompt for these) ==="
Write-Host "  Home Tailscale IP : $tsIp"
Write-Host "  DSN               : host=$tsIp port=5432 dbname=$Db user=$Role connect_timeout=5"
Write-Host "  ~/.pgpass line    : ${tsIp}:5432:${Db}:${Role}:<the password you just typed>"
```

- [ ] **Step 2: Verify the script parses**

Run:
```powershell
$t=$null;$e=$null;[System.Management.Automation.Language.Parser]::ParseFile("C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot\setup-fleet-pg-tailscale.ps1",[ref]$t,[ref]$e)|Out-Null;"parse errors: $($e.Count)"
```
Expected: `parse errors: 0`

- [ ] **Step 3: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add setup-fleet-pg-tailscale.ps1
git commit -m "feat(mac-worker): home-box PG hardening script for Tailscale remote workers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

Do NOT run the script itself — it mutates the live PG/firewall and is an owner action (runbook step).

---

### Task 5: launchd wrapper `run-worker-mac.sh` + plist template

The LaunchAgent runs the wrapper forever (KeepAlive). The wrapper: sources the env file, resolves Chrome/egress IP, pulls updates from the pinned branch, starts `applypilot-fleet-apply`, and every `UPDATE_CHECK_SECONDS` drains the worker with SIGTERM (Task 1) when origin has advanced. `ExitTimeOut 900` gives the drain 15 minutes before launchd SIGKILLs (agent timeout is 600s).

**Files:**
- Create: `run-worker-mac.sh` (repo root)
- Create: `com.applypilot.fleetworker.plist.template` (repo root)
- Test: `tests/test_mac_shell_scripts.py` (new)

**Interfaces:**
- Consumes: env file `$INSTALL_DIR/.applypilot/fleet-worker.env` (written by Task 6) with: `FLEET_PG_DSN`, `APPLYPILOT_FLEET_DSN`, `APPLYPILOT_DIR`, `PLAYWRIGHT_BROWSERS_PATH`, `APPLYPILOT_DB_PATH`, `APPLYPILOT_ENABLE_GMAIL_MCP`, `APPLYPILOT_AGENT_TIMEOUT`, `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `CLAUDE_PATH`, `WORKER_LABEL`, `WORKER_SLOT`, `WORKER_AGENT`, `WORKER_MODEL`, `FLEET_MACHINE_OWNER`, `APPLYPILOT_BRANCH`, `UPDATE_CHECK_SECONDS`, `RESTART_BACKOFF_SECONDS`, `GIT_SSH_COMMAND`. Consumes SIGTERM drain semantics from Task 1.
- Produces: sourceable bash functions `updates_available`, `apply_update`, `resolve_chrome_path`, `detect_egress_ip` (guarded `main` — the functional check below relies on sourcing).

- [ ] **Step 1: Write the failing parse test**

Create `tests/test_mac_shell_scripts.py`:

```python
"""bash -n parse gate for the macOS worker scripts (they can't run on Windows CI, but a
syntax error must not reach the Mac's self-update path — a broken wrapper bricks the
auto-update loop)."""
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = ["run-worker-mac.sh"]  # Task 6 appends "setup-mac-worker.sh"


@pytest.mark.parametrize("name", SCRIPTS)
def test_mac_shell_script_parses(name):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not on PATH")
    r = subprocess.run([bash, "-n", str(REPO / name)], capture_output=True, text=True)
    assert r.returncode == 0, f"{name} failed bash -n:\n{r.stderr}"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `& .\.conda-env\python.exe -m pytest tests\test_mac_shell_scripts.py -v`
Expected: FAIL (bash -n on a missing file exits non-zero). If it SKIPs because bash is missing, use the Git-Bash bash at `C:\Program Files\Git\bin\bash.exe` — confirm it exists and re-run with it on PATH.

- [ ] **Step 3: Write the wrapper**

Create `run-worker-mac.sh`:

```bash
#!/usr/bin/env bash
# run-worker-mac.sh -- launchd-supervised offsite-ATS apply worker for macOS.
#
# launchd (com.applypilot.fleetworker, KeepAlive) runs THIS script whenever the Mac is
# on; the script supervises ONE applypilot-fleet-apply worker:
#   * on start and every UPDATE_CHECK_SECONDS: fetch the pinned branch via the read-only
#     deploy key; if origin advanced, SIGTERM the worker (it finishes the CURRENT job --
#     see install_stop_handler in apply_worker_main.py -- then exits), update, restart.
#     pip re-installs only when pyproject.toml changed (editable install).
#   * if the worker crashes, restart after RESTART_BACKOFF_SECONDS.
#   * on SIGTERM to the wrapper (launchctl unload / shutdown): drain the worker, exit 0.
# All state lives in the fleet Postgres; killing this Mac at any time is safe (leases
# expire and the watchdog reclaims). LinkedIn NEVER runs here (separate entrypoint,
# home box only). macOS ships bash 3.2 -- keep this file 3.2-compatible.
set -u

INSTALL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$INSTALL_DIR/.applypilot/fleet-worker.env"

log() { printf '%s [wrapper] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

load_env() {
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
}

resolve_chrome_path() {
  # Newest Playwright chromium build's mac app-bundle binary.
  local d bin
  d=$(ls -d "$PLAYWRIGHT_BROWSERS_PATH"/chromium-* 2>/dev/null | sort | tail -1)
  [ -n "$d" ] || return 1
  bin=$(find "$d" -type f -path "*/Contents/MacOS/*" 2>/dev/null | head -1)
  [ -n "$bin" ] || return 1
  printf '%s' "$bin"
}

detect_egress_ip() {
  # Residential egress IP = the per-IP rate-governor key (FLEET_HOME_IP).
  curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || printf '0.0.0.0'
}

updates_available() {
  git -C "$INSTALL_DIR" fetch --quiet origin "$APPLYPILOT_BRANCH" || return 1
  [ "$(git -C "$INSTALL_DIR" rev-parse HEAD)" != "$(git -C "$INSTALL_DIR" rev-parse "origin/$APPLYPILOT_BRANCH")" ]
}

apply_update() {
  local old_sha
  old_sha=$(git -C "$INSTALL_DIR" rev-parse HEAD)
  git -C "$INSTALL_DIR" reset --hard "origin/$APPLYPILOT_BRANCH" || return 1
  if ! git -C "$INSTALL_DIR" diff --quiet "$old_sha" HEAD -- pyproject.toml; then
    log "pyproject.toml changed; re-installing package"
    "$INSTALL_DIR/.venv/bin/pip" install -q -e "$INSTALL_DIR" || return 1
  fi
  log "updated $old_sha -> $(git -C "$INSTALL_DIR" rev-parse --short HEAD)"
}

main() {
  load_env
  mkdir -p "$INSTALL_DIR/logs"
  CHROME_PATH="$(resolve_chrome_path)" || { log "FATAL: no Playwright chromium under $PLAYWRIGHT_BROWSERS_PATH"; exit 1; }
  export CHROME_PATH
  FLEET_HOME_IP="$(detect_egress_ip)"
  export FLEET_HOME_IP
  log "egress=$FLEET_HOME_IP chrome=$CHROME_PATH branch=$APPLYPILOT_BRANCH"

  child=0
  trap 'log "SIGTERM: draining worker"; [ "$child" -gt 0 ] && kill -TERM "$child" 2>/dev/null; wait "$child" 2>/dev/null; exit 0' TERM INT

  while true; do
    if updates_available; then apply_update || log "WARN: update failed; running current code"; fi
    "$INSTALL_DIR/.venv/bin/applypilot-fleet-apply" \
      --worker-id "${WORKER_LABEL:-mac}-${WORKER_SLOT:-0}" \
      --agent "${WORKER_AGENT:-claude}" \
      --model "${WORKER_MODEL:-sonnet}" \
      --machine-owner "${FLEET_MACHINE_OWNER:-mac}" &
    child=$!
    log "worker started pid=$child id=${WORKER_LABEL:-mac}-${WORKER_SLOT:-0}"
    waited=0
    while kill -0 "$child" 2>/dev/null; do
      sleep 60
      waited=$((waited + 60))
      if [ "$waited" -ge "${UPDATE_CHECK_SECONDS:-21600}" ]; then
        waited=0
        if updates_available; then
          log "update available: draining worker (finishes current job first)"
          kill -TERM "$child" 2>/dev/null
          wait "$child" 2>/dev/null
          break
        fi
      fi
    done
    wait "$child" 2>/dev/null
    child=0
    log "worker exited; restart in ${RESTART_BACKOFF_SECONDS:-30}s"
    sleep "${RESTART_BACKOFF_SECONDS:-30}"
  done
}

if [ "${BASH_SOURCE[0]}" = "${0}" ]; then main "$@"; fi
```

- [ ] **Step 4: Write the plist template**

Create `com.applypilot.fleetworker.plist.template`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.applypilot.fleetworker</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>__INSTALL_DIR__/run-worker-mac.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <!-- Drain window: SIGTERM -> worker finishes the CURRENT job (agent timeout 600s)
       before launchd escalates to SIGKILL. -->
  <key>ExitTimeOut</key><integer>900</integer>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>__INSTALL_DIR__/logs/wrapper.log</string>
  <key>StandardErrorPath</key><string>__INSTALL_DIR__/logs/wrapper.err.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
```

- [ ] **Step 5: Run the parse test to verify it passes**

Run: `& .\.conda-env\python.exe -m pytest tests\test_mac_shell_scripts.py -v`
Expected: 1 passed.

- [ ] **Step 6: Functional check of the update loop (Git Bash, temp repos)**

Run with the Bash tool (uses Git Bash on Windows; `$TMPDIR` = any scratch dir):

```bash
set -e
T=$(mktemp -d)
git init -q --bare "$T/origin.git"
git clone -q "$T/origin.git" "$T/clone"
cd "$T/clone" && git commit -q --allow-empty -m one && git push -q origin HEAD:main
# source the wrapper's functions against the temp clone
source "/c/Users/JStal/OneDrive/Documents/New project/ApplyPilot/run-worker-mac.sh"
INSTALL_DIR="$T/clone"; APPLYPILOT_BRANCH="main"
if updates_available; then echo "FAIL: no update expected"; else echo "OK: up to date"; fi
git -C "$T/clone" -c advice.detachedHead=false checkout -q HEAD  # keep on main
cd "$T" && git clone -q "$T/origin.git" pusher && cd pusher && git commit -q --allow-empty -m two && git push -q origin HEAD:main
if updates_available; then echo "OK: update detected"; else echo "FAIL: update not detected"; fi
apply_update && echo "OK: applied $(git -C "$T/clone" rev-parse --short HEAD)"
rm -rf "$T"
```

Expected output contains: `OK: up to date`, `OK: update detected`, `OK: applied <sha>` (the pyproject branch is not exercised — no pyproject.toml in the temp repo, `git diff --quiet` passes).

- [ ] **Step 7: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add run-worker-mac.sh com.applypilot.fleetworker.plist.template tests/test_mac_shell_scripts.py
git commit -m "feat(mac-worker): launchd wrapper with drain-then-update loop + plist template

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: One-time installer `setup-mac-worker.sh`

Interactive bootstrap run once on the Mac: toolchain, deploy-key clone, venv + editable install, Playwright chromium, pgpass + `chmod 600` env file (keys prompted, never persisted anywhere else), asset hydration from the `fleet_assets` PG table, launchd registration, connectivity check.

**Files:**
- Create: `setup-mac-worker.sh` (repo root)
- Modify: `tests/test_mac_shell_scripts.py` (append the new script to `SCRIPTS`)

**Interfaces:**
- Consumes: DSN/pgpass values printed by Task 4; plist template + wrapper from Task 5; `pgqueue.get_asset` (existing, src/applypilot/apply/pgqueue.py:446).
- Produces: `$INSTALL_DIR/.applypilot/fleet-worker.env` with the exact variable names Task 5's wrapper consumes.

- [ ] **Step 1: Extend the parse test (failing first)**

In `tests/test_mac_shell_scripts.py` change:

```python
SCRIPTS = ["run-worker-mac.sh"]  # Task 6 appends "setup-mac-worker.sh"
```

to:

```python
SCRIPTS = ["run-worker-mac.sh", "setup-mac-worker.sh"]
```

Run: `& .\.conda-env\python.exe -m pytest tests\test_mac_shell_scripts.py -v`
Expected: 1 passed (wrapper), 1 FAILED (setup script missing).

- [ ] **Step 2: Write the installer**

Create `setup-mac-worker.sh`:

```bash
#!/usr/bin/env bash
# setup-mac-worker.sh -- ONE-TIME interactive bootstrap of a Mac as an ApplyPilot
# offsite-ATS apply worker (worker-id mac-0). Copy JUST THIS FILE to the Mac and run:
#   bash setup-mac-worker.sh
# It clones the private repo via a READ-ONLY deploy key (your GitHub credentials never
# touch this machine), prompts for the PG password + API keys (stored ONLY in chmod-600
# files here), hydrates profile/resume from the fleet_assets PG table, and registers the
# launchd agent so the worker runs whenever this Mac is on. LinkedIn is NEVER installed
# here. Prereqs done by the owner first: Tailscale installed+joined on this Mac, and
# setup-fleet-pg-tailscale.ps1 run on the home box (it prints the values prompted below).
# macOS ships bash 3.2 -- keep this file 3.2-compatible.
set -eu

REPO_SSH="git@github.com:thefulmination/applypilot-private.git"
INSTALL_DIR="${INSTALL_DIR:-$HOME/applypilot-fleet}"
KEY="$HOME/.ssh/applypilot_deploy"
say() { printf '\n[setup] %s\n' "$*"; }

# --- 0. sanity: macOS + Tailscale up -----------------------------------------
[ "$(uname)" = "Darwin" ] || { echo "This script is for macOS."; exit 1; }
if ! /Applications/Tailscale.app/Contents/MacOS/Tailscale status >/dev/null 2>&1 \
   && ! command -v tailscale >/dev/null 2>&1; then
  say "Tailscale not found. Install from https://tailscale.com/download, sign in, then re-run."
  exit 1
fi

# --- 1. toolchain (Homebrew, python, node/npx, git, claude CLI) --------------
if ! command -v brew >/dev/null 2>&1; then
  say "Installing Homebrew (you may be prompted for the Mac's password)..."
  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
  eval "$(/opt/homebrew/bin/brew shellenv 2>/dev/null || /usr/local/bin/brew shellenv)"
fi
say "Installing python, node, git via Homebrew..."
brew install python@3.12 node git >/dev/null
say "Installing Claude Code CLI..."
npm install -g @anthropic-ai/claude-code >/dev/null

# --- 2. read-only deploy key + clone ------------------------------------------
if [ ! -f "$KEY" ]; then
  ssh-keygen -t ed25519 -f "$KEY" -N "" -C "applypilot-mac-worker" >/dev/null
fi
say "Add this READ-ONLY deploy key to the private repo, then press Enter:"
echo "  GitHub -> thefulmination/applypilot-private -> Settings -> Deploy keys -> Add (leave 'write access' UNCHECKED)"
echo ""
cat "$KEY.pub"
read -r _
GIT_SSH="ssh -i $KEY -o IdentitiesOnly=yes"
printf 'Branch to run [main]: '; read -r BRANCH; BRANCH="${BRANCH:-main}"
if [ ! -d "$INSTALL_DIR/.git" ]; then
  say "Cloning $REPO_SSH ($BRANCH) -> $INSTALL_DIR"
  GIT_SSH_COMMAND="$GIT_SSH" git clone --branch "$BRANCH" "$REPO_SSH" "$INSTALL_DIR"
fi

# --- 3. venv + package + playwright chromium ----------------------------------
cd "$INSTALL_DIR"
say "Creating venv + installing applypilot (editable)..."
"$(brew --prefix python@3.12)/bin/python3.12" -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -e . && ./.venv/bin/pip install -q "psycopg[binary]" mcp pyyaml
say "Installing Playwright chromium (project-local)..."
PLAYWRIGHT_BROWSERS_PATH="$INSTALL_DIR/.playwright-browsers" ./.venv/bin/python -m playwright install chromium

# --- 4. prompts (values printed by setup-fleet-pg-tailscale.ps1 on the home box)
printf 'Home box Tailscale IP (100.x.x.x): '; read -r HOME_TS_IP
printf 'PG password for role fleet_worker: '; read -rs PG_PW; echo
printf 'ANTHROPIC_API_KEY: '; read -rs ANTHROPIC_KEY; echo
printf 'DEEPSEEK_API_KEY: '; read -rs DEEPSEEK_KEY; echo
DSN="host=$HOME_TS_IP port=5432 dbname=applypilot_fleet user=fleet_worker connect_timeout=5"

# --- 5. pgpass (password lives HERE, chmod 600 -- never in the DSN/env file) ---
touch "$HOME/.pgpass" && chmod 600 "$HOME/.pgpass"
grep -q "^$HOME_TS_IP:5432:applypilot_fleet:fleet_worker:" "$HOME/.pgpass" 2>/dev/null || \
  printf '%s:5432:applypilot_fleet:fleet_worker:%s\n' "$HOME_TS_IP" "$PG_PW" >> "$HOME/.pgpass"
unset PG_PW

# --- 6. env file (everything run-worker-mac.sh needs) --------------------------
mkdir -p "$INSTALL_DIR/.applypilot" "$INSTALL_DIR/logs"
ENV_FILE="$INSTALL_DIR/.applypilot/fleet-worker.env"
CLAUDE_BIN="$(command -v claude || true)"
# Values are QUOTED: this file is sourced by bash (set -a; . file), and unquoted
# spaces (the DSN, GIT_SSH_COMMAND) would be parsed as commands.
cat > "$ENV_FILE" <<EOF
FLEET_PG_DSN="$DSN"
APPLYPILOT_FLEET_DSN="$DSN"
APPLYPILOT_DIR="$INSTALL_DIR/.applypilot"
PLAYWRIGHT_BROWSERS_PATH="$INSTALL_DIR/.playwright-browsers"
APPLYPILOT_DB_PATH="/tmp/fleet_apply_throwaway_0.db"
APPLYPILOT_ENABLE_GMAIL_MCP="0"
APPLYPILOT_AGENT_TIMEOUT="600"
ANTHROPIC_API_KEY="$ANTHROPIC_KEY"
DEEPSEEK_API_KEY="$DEEPSEEK_KEY"
CLAUDE_PATH="$CLAUDE_BIN"
WORKER_LABEL="mac"
WORKER_SLOT="0"
WORKER_AGENT="claude"
WORKER_MODEL="sonnet"
FLEET_MACHINE_OWNER="mac-$(hostname -s)"
APPLYPILOT_BRANCH="$BRANCH"
UPDATE_CHECK_SECONDS="21600"
RESTART_BACKOFF_SECONDS="30"
GIT_SSH_COMMAND="$GIT_SSH"
EOF
chmod 600 "$ENV_FILE"
unset ANTHROPIC_KEY DEEPSEEK_KEY
say "Env file written (chmod 600): $ENV_FILE"

# --- 7. connectivity + asset hydration from PG ---------------------------------
say "Testing Postgres over Tailscale..."
APPLYPILOT_TEST_DSN="$DSN" APPLYPILOT_DIR="$INSTALL_DIR/.applypilot" ./.venv/bin/python - <<'PY'
import os, pathlib
from applypilot.apply import pgqueue
dsn = os.environ["APPLYPILOT_TEST_DSN"]
conn = pgqueue.connect(dsn)
print("[setup] PG connection OK")
appdir = pathlib.Path(os.environ.get("APPLYPILOT_DIR", "")) or pathlib.Path.cwd() / ".applypilot"
appdir.mkdir(parents=True, exist_ok=True)
for fname in ("profile.json", "resume.pdf"):
    data = pgqueue.get_asset(conn, fname)
    if data:
        (appdir / fname).write_bytes(data)
        print(f"[setup] hydrated {fname} ({len(data)} bytes) from fleet_assets")
    elif not (appdir / fname).exists():
        print(f"[setup] WARNING: {fname} missing -- push it from the home box "
              f"(see docs/fleet-mac-worker-runbook.md) or copy it here manually")
conn.close()
PY

# --- 8. launchd -----------------------------------------------------------------
PLIST="$HOME/Library/LaunchAgents/com.applypilot.fleetworker.plist"
mkdir -p "$HOME/Library/LaunchAgents"
sed "s|__INSTALL_DIR__|$INSTALL_DIR|g" "$INSTALL_DIR/com.applypilot.fleetworker.plist.template" > "$PLIST"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"
say "launchd agent loaded. The worker now runs whenever this Mac is on."
say "Status : launchctl list | grep applypilot"
say "Logs   : tail -f $INSTALL_DIR/logs/wrapper.log $INSTALL_DIR/.applypilot/logs/worker-0.log"
```

- [ ] **Step 3: Run the parse test to verify it passes**

Run: `& .\.conda-env\python.exe -m pytest tests\test_mac_shell_scripts.py -v`
Expected: 2 passed.

- [ ] **Step 4: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add setup-mac-worker.sh tests/test_mac_shell_scripts.py
git commit -m "feat(mac-worker): one-time interactive macOS installer (deploy key, venv, pgpass, env, launchd)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Owner runbook `docs/fleet-mac-worker-runbook.md`

Follows the `docs/fleet-<feature>-runbook.md` naming convention. Every command the OWNER runs, end to end, including the asset push and the canary.

**Files:**
- Create: `docs/fleet-mac-worker-runbook.md`

**Interfaces:**
- Consumes: everything from Tasks 4-6 (script names, prompts, env values).

- [ ] **Step 1: Write the runbook**

Create `docs/fleet-mac-worker-runbook.md` with exactly these sections and commands:

````markdown
# Mac Offsite-Apply Worker — Owner Runbook

Adds a macOS machine (different LAN, intermittently on) as an offsite-ATS apply worker
(`mac-0`). Design: `docs/superpowers/specs/2026-07-03-mac-fleet-worker-design.md`.
LinkedIn NEVER runs on this machine (lane B stays on the home IP).

## A. Home box (one-time, ~15 min)

1. **Tailscale**: install + sign in on the home box (skip if `tailscale ip -4` already
   prints a 100.x address).
2. **Decide the branch the Mac runs.** The Mac pulls `applypilot-private` — push the
   branch you want live (currently `applypilot-hardening-and-brainstorm-integration`;
   use `main` only if the fleet code you run is merged there):
   ```powershell
   git push private applypilot-hardening-and-brainstorm-integration
   ```
3. **Harden PG for the tailnet** (elevated PowerShell, repo root). Prompts for the NEW
   `fleet_worker` password — save it for step C:
   ```powershell
   .\setup-fleet-pg-tailscale.ps1
   ```
   It prints the Home Tailscale IP + DSN + pgpass line the Mac setup will ask for.
4. **Push profile + resume into the fleet_assets table** (so the Mac hydrates them from
   PG — nothing PII transits OneDrive/AirDrop):
   ```powershell
   .\.conda-env\python.exe -c "from applypilot.apply import pgqueue; import pathlib; h=pathlib.Path.home()/'.applypilot'; c=pgqueue.connect('host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5'); pgqueue.put_asset(c,'profile.json',(h/'profile.json').read_bytes()); pgqueue.put_asset(c,'resume.pdf',(h/'resume.pdf').read_bytes()); c.close(); print('assets pushed')"
   ```
   (Adjust the two paths if your profile/resume live elsewhere.)

## B. GitHub (one-time, ~2 min)

The Mac authenticates with a READ-ONLY deploy key it generates during setup. When the
setup script pauses and shows a public key: GitHub → `thefulmination/applypilot-private`
→ Settings → Deploy keys → *Add deploy key* → paste → leave **Allow write access
UNCHECKED** → Add.

## C. The Mac (one-time, ~20 min)

1. Install Tailscale (App Store or https://tailscale.com/download) and sign in to YOUR
   tailnet.
2. Copy the single file `setup-mac-worker.sh` to the Mac (AirDrop/USB is fine — it
   contains no secrets) and run:
   ```bash
   bash setup-mac-worker.sh
   ```
   Prompts, in order: deploy-key pause (step B) → branch → home Tailscale IP →
   `fleet_worker` PG password → `ANTHROPIC_API_KEY` → `DEEPSEEK_API_KEY`.
   Keys land ONLY in `~/applypilot-fleet/.applypilot/fleet-worker.env` and
   `~/.pgpass`, both `chmod 600`.
3. Sanity: `launchctl list | grep applypilot` shows the agent;
   `tail -f ~/applypilot-fleet/logs/wrapper.log` shows `worker started`.

## D. Canary go-live (home box)

1. Fleet must be alive with approved work queued (if dormant: `.\register-fleet-tasks.ps1`
   and confirm `apply_queue` has `status='queued'` rows with `approved_batch` set).
2. Arm a small ATS canary so the fleet auto-pauses after 5 applies (PowerShell
   SINGLE-quoted so the inner python double quotes pass through verbatim):
   ```powershell
   .\.conda-env\python.exe -c 'from applypilot.apply import pgqueue; c=pgqueue.connect("host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"); cur=c.cursor(); cur.execute("UPDATE fleet_config SET canary_enabled=TRUE, canary_remaining=5 WHERE id=1"); c.commit(); c.close(); print("canary armed: 5")'
   ```
3. Watch live — console `http://<home-tailscale-ip>:8787` (reachable tailnet-wide), or
   (the doubled `''` is a literal single quote inside a PS single-quoted string):
   ```powershell
   .\.conda-env\python.exe -c 'from applypilot.apply import pgqueue; c=pgqueue.connect("host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"); cur=c.cursor(); cur.execute("SELECT worker_id, state, current_job, last_beat FROM worker_heartbeat WHERE worker_id LIKE ''mac-%''"); [print(r) for r in cur.fetchall()]; c.close()'
   ```
4. Verify the canary applies landed (`apply_queue.status='applied'` with
   `worker_id='mac-0'`), then disarm the canary / raise caps as usual.

## E. How updates reach the Mac

Push to the pinned branch on `private`; within `UPDATE_CHECK_SECONDS` (6 h, or next Mac
boot) the wrapper fetches, SIGTERMs the worker (it FINISHES the current job — Task-1
handler — then exits), hard-resets to origin, and restarts. `pip install -e .` re-runs
only when `pyproject.toml` changed. Force it immediately: on the Mac,
`launchctl kickstart -k gui/$(id -u)/com.applypilot.fleetworker`.

## F. Kill switch (all owner-side, no access to the Mac needed)

1. Rotate the API keys (Anthropic + DeepSeek consoles).
2. Rotate the PG credential: re-run `.\setup-fleet-pg-tailscale.ps1` with a new password.
3. Remove the Mac from the tailnet (Tailscale admin console).

## G. Troubleshooting

| Symptom | Check |
|---|---|
| No heartbeat row for `mac-0` | Mac on? `launchctl list \| grep applypilot`; `logs/wrapper.log` |
| `FATAL: no Playwright chromium` in wrapper.log | Re-run `PLAYWRIGHT_BROWSERS_PATH=~/applypilot-fleet/.playwright-browsers ~/applypilot-fleet/.venv/bin/python -m playwright install chromium` |
| PG connect fails | `tailscale ping <home-ts-ip>` from the Mac; pg_hba rule + firewall rule on home box (`setup-fleet-pg-tailscale.ps1` re-run is safe) |
| Applies fail with missing profile/resume | Re-run runbook A.4 (asset push), then on the Mac delete `~/applypilot-fleet/.applypilot/profile.json` and re-run `setup-mac-worker.sh` step or copy manually |
| Worker never updates | `git -C ~/applypilot-fleet fetch origin <branch>` by hand — deploy key revoked? branch deleted? |
| Gmail/OTP challenges park as auth_challenge | Expected: `APPLYPILOT_ENABLE_GMAIL_MCP=0` on the Mac v1 (no Gmail OAuth creds there). Resolve challenges from the console, or copy Gmail creds + set the flag to 1 later. |
````

- [ ] **Step 2: Verify internal consistency**

Check (read-only): every script name, env var, and prompt mentioned in the runbook matches Tasks 4-6 exactly (`setup-fleet-pg-tailscale.ps1`, `setup-mac-worker.sh`, prompt order, `fleet-worker.env` path, `UPDATE_CHECK_SECONDS=21600`). Fix any drift in the runbook, not the scripts.

- [ ] **Step 3: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add docs/fleet-mac-worker-runbook.md
git commit -m "docs(mac-worker): owner runbook (home-box hardening, Mac install, canary, kill switch)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Full regression + wrap-up

**Files:**
- Modify (status line only): `docs/superpowers/specs/2026-07-03-mac-fleet-worker-design.md`

- [ ] **Step 1: Run the fleet + apply test suite**

Run: `& .\.conda-env\python.exe -m pytest tests -q -k "fleet or supervisor or apply or mac"`
Expected: all pass (report any pre-existing failures unrelated to this plan separately; do not fix them here).

- [ ] **Step 2: Confirm no secrets or unrelated files are staged**

Run: `git status --short` and `git diff --cached --stat`
Expected: clean except plan-named files; grep the new files for accidental literal keys: `git grep -n "sk-ant\|sk-" -- setup-mac-worker.sh run-worker-mac.sh setup-fleet-pg-tailscale.ps1` → no matches.

- [ ] **Step 3: Mark the spec implemented**

In `docs/superpowers/specs/2026-07-03-mac-fleet-worker-design.md` change the line
`**Status:** Approved by owner (brainstorming session)` to
`**Status:** Implemented (see docs/superpowers/plans/2026-07-03-mac-fleet-worker.md); owner go-live via docs/fleet-mac-worker-runbook.md`.

- [ ] **Step 4: Commit**

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
git add docs/superpowers/specs/2026-07-03-mac-fleet-worker-design.md
git commit -m "docs(mac-worker): mark spec implemented

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```
