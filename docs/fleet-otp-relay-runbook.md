# Fleet OTP / Email-Verification Relay — Owner Runbook

Lets remote workers (the Mac, any offsite box) clear email-verification walls using
codes read from the home box's Gmail — Gmail credentials never leave the home box.
Design: `docs/superpowers/specs/2026-07-03-fleet-otp-relay-design.md`.

## Home box (one-time + keep running)

The responder must run on the box that has Gmail (`~/.applypilot/gmail_credentials.json`).
Register it once on the home box:

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
.\register-otp-responder-startup.ps1
```

The script first tries to create `ApplyPilotFleet-OtpResponder` in Task Scheduler. If
Windows denies that registration, it installs a current-user Startup shortcut named
`ApplyPilotFleet-OtpResponder.lnk` instead. Both paths run `run-otp-responder.ps1
-Supervise`, so the hidden launcher restarts the responder if the child process exits.

For an immediate manual start without registering persistence:

```powershell
cd "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot"
.\run-otp-responder.ps1
```

It scans Gmail only when a request is actually pending, so it is cheap when idle.

## Remote workers

The Mac installer now sets `APPLYPILOT_INBOX_AUTH=1` and `APPLYPILOT_INBOX_AUTH_MODE=relay`
automatically, so a freshly set-up worker uses the relay. For an already-installed Mac,
add those two lines to `~/applypilot-fleet/.applypilot/fleet-worker.env` and restart:
`launchctl kickstart -k gui/$(id -u)/com.applypilot.fleetworker`.

## Verify end to end

With the responder running and a worker applying to a job that needs email verification,
watch a request appear and get answered (run on the home box):

```powershell
.\.conda-env\python.exe -c 'from applypilot.apply import pgqueue; c=pgqueue.connect("host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"); cur=c.cursor(); cur.execute("SELECT id, worker_id, sender_hint, requested_at, answered_at, consumed_at, (code IS NOT NULL) AS has_code FROM otp_request ORDER BY id DESC LIMIT 5"); [print(r) for r in cur.fetchall()]; c.close()'
```

A healthy cycle shows a row go `requested_at` set → `answered_at` set (`has_code` briefly true)
→ `consumed_at` set (`has_code` false). The code value is never displayed or logged.

## Notes

- If the home box or responder is down, remote workers time out and the job parks/fails
  gracefully exactly as before — the relay never makes things worse.
- Matching is time-based (the code email must arrive AFTER the request); concurrent
  applies on the same ATS are assigned nearest-in-time, one email per request.
