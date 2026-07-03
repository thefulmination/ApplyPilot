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
