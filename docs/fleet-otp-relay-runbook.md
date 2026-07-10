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

For a deployment restart, use the existing responder launcher. It removes stale
responder instances scoped to this checkout before starting the replacement; it
does not restart apply, discovery, doctor, or watchdog processes.

```powershell
.\run-otp-responder.ps1
Start-Sleep -Seconds 8
```

Verify exactly one responder executable and a fresh heartbeat without printing
command lines or connection details:

```powershell
$ResponderCount = @(
    Get-CimInstance Win32_Process | Where-Object {
        $_.Name -eq "applypilot-fleet-otp-home.exe"
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
2. Set a unique non-secret worker label and capture the start time.
3. Run one headed, supervised application with relay auth enabled. The worker
   creates one request, waits for the responder answer, atomically consumes it,
   clears the secret value, and performs at most one assisted retry.

```powershell
$ControlledUrl = Read-Host "Controlled application URL"
$env:FLEET_WORKER_ID = "otp-e2e-home"
$env:APPLYPILOT_INBOX_AUTH = "1"
$env:APPLYPILOT_INBOX_AUTH_MODE = "relay"
$env:APPLYPILOT_FLEET_DSN = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
$env:OTP_E2E_STARTED_AT = (Get-Date).ToUniversalTime().ToString("o")
.\run-applypilot.ps1 apply --url $ControlledUrl --inbox-auth --workers 1
```

Observe that the one assisted retry reaches a terminal result: submitted, or the
expected bounded controlled failure. Do not infer this from a mailbox match alone
and do not paste agent output if it contains prohibited material.

Run the lifecycle proof immediately afterward. It emits only required booleans:

```powershell
@'
import datetime as dt
import os

from applypilot.apply import pgqueue

started = dt.datetime.fromisoformat(os.environ["OTP_E2E_STARTED_AT"].replace("Z", "+00:00"))
worker_id = os.environ["FLEET_WORKER_ID"]
dsn = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
with pgqueue.connect(dsn) as conn, conn.cursor() as cur:
    cur.execute("""
        SELECT
          COUNT(*) = 1 AS request_created,
          COALESCE(BOOL_OR(answered_at IS NOT NULL), false) AS responder_answered,
          COALESCE(BOOL_OR(consumed_at IS NOT NULL), false) AS worker_consumed,
          COALESCE(BOOL_OR(consumed_at IS NOT NULL AND code IS NULL), false) AS code_cleared,
          COALESCE(BOOL_OR(consumed_at IS NOT NULL AND matched_message_id IS NOT NULL), false)
            AS matched_message_id_retained
        FROM otp_request
        WHERE worker_id = %s AND requested_at >= %s
    """, (worker_id, started))
    facts = cur.fetchone()
for key in (
    "request_created", "responder_answered", "worker_consumed",
    "code_cleared", "matched_message_id_retained",
):
    print(f"{key}={str(bool(facts[key])).lower()}")
'@ | .\.conda-env\python.exe -
```

After confirming the worker's terminal result, record only:

```text
assisted_retry_terminal=yes
```

Finally, check for OTP-specific DeadMan alerts without printing alert details:

```powershell
@'
import datetime as dt

from applypilot.apply import pgqueue
from applypilot.fleet import deadman

dsn = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
with pgqueue.connect(dsn) as conn:
    alerts, _ = deadman.deadman_check(
        conn,
        now=dt.datetime.now(dt.timezone.utc),
        gmail_token_ok=True,
    )
otp_alerts = [a for a in alerts if a.kind in {"otp_relay_down", "otp_delivery_stalled"}]
print(f"deadman_otp_alerts={len(otp_alerts)}")
'@ | .\.conda-env\python.exe -
```

Acceptance requires these exact non-secret facts:

```text
request_created=true
responder_answered=true
worker_consumed=true
code_cleared=true
matched_message_id_retained=true
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
