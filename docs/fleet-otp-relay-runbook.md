# Fleet OTP / Email-Verification Relay - Owner Runbook

The relay lets a remote apply worker clear a supported email-verification wall
without placing Gmail credentials on that worker. The home box reads Gmail and
answers a short-lived Postgres request. Gmail remains read-only and verification
material is consumed once.

Run every home-box command below from the production checkout:

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
```

## Non-disclosure rule

Never output verification codes, email subjects, senders, magic links,
credentials, app passwords, or password-bearing DSNs. Do not add those values to
diagnostic commands, screenshots, tickets, chat, or logs. The commands below emit
only backend names, counts, ages, health booleans, and lifecycle booleans.

The Gmail app-password secret belongs only at
`~/.applypilot/gmail_app_password.json`. Do not use or create a legacy OAuth
credential file for this relay.

## Apply the schema

This migration is idempotent and prints no connection string or row content:

```powershell
$env:PYTHONPATH = (Join-Path $PWD "src")
@'
from applypilot.apply import pgqueue
from applypilot.fleet import schema

dsn = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
with pgqueue.connect(dsn) as conn:
    schema.ensure_schema_v3(conn)
print("schema=ok")
'@ | .\.conda-env\python.exe -
```

Expected: `schema=ok`. This installs the `wait_started_at` and
`matched_message_id` lifecycle fields, uniqueness protection, and active-wait
index.

## Start and restart only the responder

Register persistent startup once:

```powershell
.\register-otp-responder-startup.ps1
```

For a deployment restart, preserve the persistent `-Supervise` launcher. If its
child is running, stop only that child and let the supervisor relaunch it. If no
supervisor exists for this checkout, start the existing launcher in supervised
mode. Never stop the supervisor.

```powershell
$Launcher = (Resolve-Path -LiteralPath ".\run-otp-responder.ps1").Path
$ExpectedResponderExe = (
    Resolve-Path -LiteralPath ".\.conda-env\Scripts\applypilot-fleet-otp-home.exe"
).Path

$Supervisor = @(
    Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.CommandLine -and
        $_.CommandLine.IndexOf($Launcher, [StringComparison]::OrdinalIgnoreCase) -ge 0 -and
        $_.CommandLine -match '(?i)(?:^|\s)-Supervise(?:\s|$)'
    }
)
if ($Supervisor.Count -gt 1) {
    throw "Multiple OTP supervisors exist for this checkout; resolve before restart."
}

if ($Supervisor.Count -eq 1) {
    Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.ExecutablePath -and
        $_.ExecutablePath -eq $ExpectedResponderExe
    } | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -ErrorAction Stop
    }
} else {
    $LauncherArguments = @(
        "-NoProfile",
        "-ExecutionPolicy", "Bypass",
        "-WindowStyle", "Hidden",
        "-File", ('"{0}"' -f $Launcher),
        "-Supervise"
    )
    Start-Process -FilePath "powershell.exe" -WindowStyle Hidden `
        -WorkingDirectory $PWD.Path -ArgumentList $LauncherArguments | Out-Null
}

$RestartDeadline = (Get-Date).AddSeconds(45)
do {
    Start-Sleep -Seconds 1
    $ResponderCount = @(
        Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.ExecutablePath -and
            $_.ExecutablePath -eq $ExpectedResponderExe
        }
    ).Count
} while ($ResponderCount -ne 1 -and (Get-Date) -lt $RestartDeadline)

if ($ResponderCount -ne 1) {
    throw "Expected exactly one production-checkout OTP responder after restart."
}
Write-Output "responder_process_count=$ResponderCount"
```

Verify exactly one responder executable and a fresh heartbeat without printing
command lines or connection details:

```powershell
$ExpectedResponderExe = (
    Resolve-Path -LiteralPath ".\.conda-env\Scripts\applypilot-fleet-otp-home.exe"
).Path
$ResponderCount = @(
    Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
        $_.ExecutablePath -and
        $_.ExecutablePath -eq $ExpectedResponderExe
    }
).Count
Write-Output "responder_process_count=$ResponderCount"

@'
from applypilot.apply import pgqueue

dsn = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
with pgqueue.connect(dsn) as conn, conn.cursor() as cur:
    cur.execute(
        "SELECT COALESCE(MAX(last_beat) >= now() - interval '30 seconds', false) "
        "AS healthy FROM worker_heartbeat WHERE worker_id='otp_responder'"
    )
    print(f"responder_heartbeat_healthy={str(bool(cur.fetchone()['healthy'])).lower()}")
'@ | .\.conda-env\python.exe -
```

Required: `responder_process_count=1` and
`responder_heartbeat_healthy=true`.

## Privacy-safe Gmail canary

This executes the same Gmail `X-GM-RAW` verification-mail filter used by the
responder. It confirms the active backend and query path but never prints message
content or metadata.

```powershell
@'
from applypilot import inbox_auth
from applypilot.mail_source import get_mail_source

source = get_mail_source()
if type(source).__name__ != "ImapMailSource":
    raise SystemExit("backend_not_imap")
messages = source.fetch(
    since_days=1,
    max_messages=1,
    gmail_raw_query=inbox_auth.AUTH_GMAIL_RAW_QUERY,
)
print("backend=ImapMailSource")
print("x_gm_raw=ok")
print(f"filtered_count={len(messages)}")
'@ | .\.conda-env\python.exe -
```

Required: `backend=ImapMailSource` and `x_gm_raw=ok`. A zero filtered
count is healthy when no recent verification mail exists.

## Aggregate relay status

This query prints only the active pending count, oldest active-wait age, responder
heartbeat health, and whether any matched-message audit marker exists:

```powershell
@'
from applypilot.apply import pgqueue

dsn = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
with pgqueue.connect(dsn) as conn, conn.cursor() as cur:
    cur.execute("""
        SELECT
          COUNT(*) FILTER (
            WHERE code IS NULL AND consumed_at IS NULL
              AND wait_started_at IS NOT NULL
              AND requested_at <= now() AND expires_at > now()
          ) AS pending_count,
          EXTRACT(EPOCH FROM now() - MIN(wait_started_at) FILTER (
            WHERE code IS NULL AND consumed_at IS NULL
              AND wait_started_at IS NOT NULL
              AND requested_at <= now() AND expires_at > now()
          ))::bigint AS oldest_wait_age_seconds,
          EXISTS (
            SELECT 1 FROM otp_request WHERE matched_message_id IS NOT NULL
          ) AS matched_message_present
        FROM otp_request
    """)
    status = cur.fetchone()
    cur.execute(
        "SELECT COALESCE(MAX(last_beat) >= now() - interval '30 minutes', false) "
        "AS healthy FROM worker_heartbeat WHERE worker_id='otp_responder'"
    )
    heartbeat = bool(cur.fetchone()["healthy"])
print(f"pending_count={status['pending_count']}")
print(f"oldest_wait_age_seconds={status['oldest_wait_age_seconds'] or 0}")
print(f"responder_heartbeat_healthy={str(heartbeat).lower()}")
print(f"matched_message_present={str(bool(status['matched_message_present'])).lower()}")
'@ | .\.conda-env\python.exe -
```

`matched_message_present=false` is normal before the first successful cycle.

## Alert interpretation

- `otp_relay_down` is critical. During active demand it means the responder
  heartbeat is absent/stale, or mail-source authentication failed. Restore the
  responder or IMAP access before retrying. With no active wait, an absent
  never-started responder does not create noise; mail authentication failure still
  alerts.
- `otp_delivery_stalled` is critical. Active demand has exceeded
  `APPLYPILOT_OTP_STALL_SECONDS` (default 120, minimum 30) while both the responder
  heartbeat and mail source are healthy. Investigate provider matching, delivery
  latency, and responder errors. Do not inspect or print message content.
- A prearmed row with `wait_started_at IS NULL` is not active demand and must not
  produce a delivery-stalled alert.

## Controlled end-to-end acceptance

Use a real application URL that is known to send an email challenge, or an
owner-approved test message that follows the same provider and timestamp rules.
Do not synthesize a database answer: the controlled end-to-end check must prove
the mailbox path.

1. Confirm the schema, process count, heartbeat, and `X-GM-RAW` canary above.
2. Choose an unused Chrome slot from 0 through 9. The command defaults to slot 9
   and fails before launch if its debugging port is already in use; set
   `APPLYPILOT_OTP_E2E_SLOT` beforehand to select another dedicated slot.
3. Generate a GUID-suffixed non-secret worker label and capture the start time.
4. Run one headed application through the production fleet worker implementation
   with relay auth enabled. The worker
   creates one request, waits for the responder answer, atomically consumes it,
   clears the secret value, and performs at most one assisted retry.

```powershell
& {
    $EnvironmentNames = @(
        "FLEET_WORKER_ID",
        "APPLYPILOT_INBOX_AUTH",
        "APPLYPILOT_INBOX_AUTH_MODE",
        "FLEET_PG_DSN",
        "OTP_E2E_STARTED_AT",
        "APPLYPILOT_OTP_E2E_SLOT",
        "APPLYPILOT_OTP_E2E_URL"
    )
    $PreviousEnvironment = @{}
    foreach ($Name in $EnvironmentNames) {
        $Existing = Get-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
        $PreviousEnvironment[$Name] = [pscustomobject]@{
            Existed = $null -ne $Existing
            Value = if ($null -ne $Existing) { $Existing.Value } else { $null }
        }
    }

    try {
        $ControlledUrl = Read-Host "Controlled application URL"
        $TestWorkerId = "otp-e2e-home-$([guid]::NewGuid().ToString('N'))"
        $TestSlotText = if ($env:APPLYPILOT_OTP_E2E_SLOT) {
            $env:APPLYPILOT_OTP_E2E_SLOT
        } else {
            "9"
        }
        $TestSlot = 0
        if (-not [int]::TryParse($TestSlotText, [ref]$TestSlot) -or
            $TestSlot -lt 0 -or $TestSlot -gt 9) {
            throw "APPLYPILOT_OTP_E2E_SLOT must be an integer from 0 through 9."
        }
        $BasePortText = if ($env:APPLYPILOT_BASE_CDP_PORT) {
            $env:APPLYPILOT_BASE_CDP_PORT
        } else {
            "9400"
        }
        $BasePort = 0
        if (-not [int]::TryParse($BasePortText, [ref]$BasePort) -or $BasePort -lt 1) {
            throw "APPLYPILOT_BASE_CDP_PORT must be a positive integer."
        }
        $TestPort = $BasePort + $TestSlot
        if ($TestPort -gt 65535) {
            throw "The controlled-cycle Chrome port is outside the valid TCP range."
        }
        if (Get-NetTCPConnection -State Listen -LocalPort $TestPort -ErrorAction SilentlyContinue) {
            throw "The controlled-cycle Chrome slot is already in use; choose another slot."
        }
        $env:FLEET_WORKER_ID = $TestWorkerId
        $env:APPLYPILOT_INBOX_AUTH = "1"
        $env:APPLYPILOT_INBOX_AUTH_MODE = "relay"
        $env:FLEET_PG_DSN = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
        $env:OTP_E2E_STARTED_AT = (Get-Date).ToUniversalTime().ToString("o")
        $env:APPLYPILOT_OTP_E2E_SLOT = [string]$TestSlot
        $env:APPLYPILOT_OTP_E2E_URL = $ControlledUrl

        @'
import datetime as dt
import contextlib
import os

from applypilot.fleet import apply_worker_main

apply_worker_main._setup_apply_env()

from applypilot.apply import pgqueue
from applypilot.fleet import deadman

started = dt.datetime.fromisoformat(os.environ["OTP_E2E_STARTED_AT"].replace("Z", "+00:00"))
worker_id = os.environ["FLEET_WORKER_ID"]
slot = int(os.environ["APPLYPILOT_OTP_E2E_SLOT"])
job = {
    "url": os.environ["APPLYPILOT_OTP_E2E_URL"],
    "application_url": os.environ["APPLYPILOT_OTP_E2E_URL"],
    "title": "Controlled email-auth acceptance",
    "company": "Owner-approved controlled target",
    "site": "Owner-approved controlled target",
    "score": 10,
    "fit_score": 10,
    "description": "Owner-approved controlled acceptance cycle.",
    "source": "controlled_acceptance",
    "tailored_resume_path": None,
}

apply_fn = apply_worker_main.make_apply_fn(
    "sonnet",
    "codex",
    slot=slot,
    fleet_worker_id=worker_id,
)
try:
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            result = apply_fn(job)

            dsn = os.environ["FLEET_PG_DSN"]
            with pgqueue.connect(dsn) as conn, conn.cursor() as cur:
                cur.execute("""
                    SELECT
                      COUNT(*) = 1 AS request_created,
                      COALESCE(BOOL_OR(answered_at IS NOT NULL), false) AS responder_answered,
                      COALESCE(BOOL_OR(consumed_at IS NOT NULL), false) AS worker_consumed,
                      COALESCE(BOOL_OR(consumed_at IS NOT NULL AND code IS NULL), false) AS code_cleared,
                      COALESCE(BOOL_OR(
                        consumed_at IS NOT NULL AND matched_message_id IS NOT NULL
                      ), false) AS matched_message_id_retained
                    FROM otp_request
                    WHERE worker_id = %s AND requested_at >= %s
                """, (worker_id, started))
                facts = cur.fetchone()

            with pgqueue.connect(dsn) as conn:
                mail_source_ok = deadman.mail_source_alive()
                alerts, _ = deadman.deadman_check(
                    conn,
                    now=dt.datetime.now(dt.timezone.utc),
                    gmail_token_ok=mail_source_ok,
                )
            otp_alerts = [
                alert for alert in alerts
                if alert.kind in {"otp_relay_down", "otp_delivery_stalled"}
            ]
            otp_alert_count = len(otp_alerts)
            if mail_source_ok is not True:
                otp_alert_count = max(1, otp_alert_count)
except Exception:
    raise SystemExit(1) from None

for key in (
    "request_created", "responder_answered", "worker_consumed",
    "code_cleared", "matched_message_id_retained",
):
    print(f"{key}={'yes' if bool(facts[key]) else 'no'}")
print(f"inbox_auth_prearmed={'yes' if result['inbox_auth_prearmed'] else 'no'}")
print(f"assisted_retry_count={result['assisted_retry_count']}")
print(f"assisted_retry_terminal={'yes' if result['assisted_retry_terminal'] else 'no'}")
print(f"deadman_otp_alerts={otp_alert_count}")

accepted = (
    all(bool(facts[key]) for key in (
        "request_created", "responder_answered", "worker_consumed",
        "code_cleared", "matched_message_id_retained",
    ))
    and result["inbox_auth_prearmed"] is True
    and result["assisted_retry_count"] == 1
    and result["assisted_retry_terminal"] is True
    and mail_source_ok is True
    and otp_alert_count == 0
)
if not accepted:
    raise SystemExit(1)
'@ | .\.conda-env\python.exe -
    }
    finally {
        foreach ($Name in $EnvironmentNames) {
            $Prior = $PreviousEnvironment[$Name]
            if ($Prior.Existed) {
                [Environment]::SetEnvironmentVariable($Name, $Prior.Value, "Process")
            } else {
                Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
            }
        }
    }
}
```

The outer script block keeps the URL, GUID worker ID, slot, and timestamp local. Its
`finally` restores prior environment values or removes newly introduced values,
even if the apply or verification fails. Python runs as a child process, so
`_setup_apply_env()` cannot leave its environment changes in the operator shell.
The result metadata automatically proves whether exactly one assisted retry
reached a terminal result: submitted, or the expected bounded controlled failure.
Do not infer this from a mailbox match alone and do not paste agent output if it
contains prohibited material.

Acceptance requires these exact non-secret facts:

```text
request_created=yes
responder_answered=yes
worker_consumed=yes
code_cleared=yes
matched_message_id_retained=yes
inbox_auth_prearmed=yes
assisted_retry_count=1
assisted_retry_terminal=yes
deadman_otp_alerts=0
```

If a controlled message cannot be delivered, that is an external blocker. Report
it exactly and do not call the live cycle passed, even when automated tests, the
IMAP canary, process count, and heartbeat are healthy.

## Remote workers

The Mac installer sets `APPLYPILOT_INBOX_AUTH=1` and
`APPLYPILOT_INBOX_AUTH_MODE=relay`. For an existing installation, add those two
lines to `~/applypilot-fleet/.applypilot/fleet-worker.env`, then restart only that
worker with:

```bash
launchctl kickstart -k gui/$(id -u)/com.applypilot.fleetworker
```
