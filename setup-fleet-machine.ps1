<#
================================================================================
  setup-fleet-machine.ps1
  Set up a SECOND machine to run Codex and talk to the ApplyPilot fleet Postgres,
  over your private home LAN (both machines on the 192.168.1.x router -- NO Tailscale).

  The home box's LAN IP is 192.168.1.187 (gateway 192.168.1.1). Reserve that IP on
  the router (DHCP reservation) so it doesn't change. Both machines must be on the
  SAME router/network for this to work.

  --------------------------------------------------------------------------------
  STEP 0 -- DO THIS ONCE ON THE HOME BOX (the machine that has Postgres), AS ADMIN:
  --------------------------------------------------------------------------------
    # Allow Postgres from the private LAN only (a public source can't match this rule,
    # and TCP can't spoof a 192.168.x source, so this is safe even with the public
    # Ethernet still plugged in):
    Add-Content "C:\Program Files\PostgreSQL\18\data\pg_hba.conf" "host all all 192.168.1.0/24 scram-sha-256"
    New-NetFirewallRule -DisplayName "PostgreSQL LAN" -Direction Inbound -Protocol TCP -LocalPort 5432 -Action Allow -RemoteAddress 192.168.1.0/24
    Restart-Service postgresql-x64-18
    # (listen_addresses is already '*'. pg_hba + firewall limit reach to the LAN.)

  --------------------------------------------------------------------------------
  THEN, ON THIS (SECOND) MACHINE -- also on the 192.168.1.x router -- run:
      powershell -ExecutionPolicy Bypass -File .\setup-fleet-machine.ps1
  --------------------------------------------------------------------------------
#>

$ErrorActionPreference = "Stop"
function Say($m,$c="White"){ Write-Host $m -ForegroundColor $c }

Say "`n=== ApplyPilot fleet -- second-machine setup (LAN) ===" Cyan

# --- Prereqs this script can't automate (interactive sign-ins) ---
Say "`nBefore continuing, make sure:" Yellow
Say "  [ ] This machine is on the SAME router as the home box (a 192.168.1.x address)"
Say "  [ ] The home box STEP 0 above is done (PG opened to the LAN, service restarted)"
Say "  [ ] Codex CLI is installed + signed in here (npm i -g @openai/codex ; codex login)"
if ((Read-Host "All set? (y/n)") -ne 'y') { Say "Finish those, then re-run." Yellow; exit 1 }

# Sanity: are WE on the private LAN?
$gw = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1).IPv4DefaultGateway.NextHop
if ($gw -notlike '192.168.*' -and $gw -notlike '10.*' -and $gw -notlike '172.*') {
  Say "  WARNING: your primary gateway is $gw (not private). Connect this machine to the same router as the home box first." Yellow
  if ((Read-Host "Continue anyway? (y/n)") -ne 'y') { exit 1 }
}

# --- 1. Base tools via winget (skips anything already present) ---
function Ensure($name,$id,$cmd){
  if (Get-Command $cmd -ErrorAction SilentlyContinue){ Say "  [ok] $name" Green }
  else { Say "  installing $name ..." ; winget install -e --id $id --accept-source-agreements --accept-package-agreements }
}
Ensure "Git"         "Git.Git"            "git"
Ensure "Python 3.12" "Python.Python.3.12" "python"
Ensure "Node.js LTS" "OpenJS.NodeJS.LTS"  "node"

# --- 2. Inputs ---
$homeIp = (Read-Host "Home box LAN IP [default 192.168.1.187]").Trim()
if (-not $homeIp){ $homeIp = "192.168.1.187" }
$pgPw   = (Read-Host "Postgres password (the home box's 'postgres' user password)").Trim()
$installDir = (Read-Host "Folder to install the code [default C:\ApplyPilot]").Trim()
if (-not $installDir){ $installDir = "C:\ApplyPilot" }

# --- 3. Reachability check BEFORE the heavy install ---
Say "`n  checking the home box is reachable on the LAN ..." Cyan
if (-not (Test-Connection -ComputerName $homeIp -Count 2 -Quiet)) {
  Say "  Could not ping $homeIp. Confirm both machines are on the same router and the IP is right." Yellow
  if ((Read-Host "Continue anyway? (y/n)") -ne 'y') { exit 1 }
}

# --- 4. Get the code (private repo -- you may be prompted to sign in to GitHub) ---
$repo = "https://github.com/thefulmination/applypilot-private.git"
if (-not (Test-Path (Join-Path $installDir ".git"))){
  Say "  cloning the fleet repo -> $installDir (sign in to GitHub if asked) ..."
  git clone $repo $installDir
} else { Say "  [ok] repo already at $installDir" Green }
Set-Location $installDir

# --- 5. Python env + install (bridge needs psycopg + mcp; pyyaml for config) ---
if (-not (Test-Path ".\.venv")){ python -m venv .venv }
$py = (Resolve-Path ".\.venv\Scripts\python.exe").Path
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -e . --quiet
& $py -m pip install "psycopg[binary]" mcp pyyaml --quiet

# --- 6. Postgres connectivity: pgpass (keeps the DSN passwordless) + env vars ---
$pgpassDir = Join-Path $env:APPDATA "postgresql"
New-Item -ItemType Directory -Force -Path $pgpassDir | Out-Null
Set-Content -Path (Join-Path $pgpassDir "pgpass.conf") -Value "${homeIp}:5432:*:postgres:${pgPw}" -Encoding ascii
$dsn = "host=$homeIp port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
# FLEET_PG_DSN -> the fleet CLIs + Codex bridge; APPLYPILOT_FLEET_DSN -> the low-level connect() fallback.
[Environment]::SetEnvironmentVariable("FLEET_PG_DSN", $dsn, "User")
[Environment]::SetEnvironmentVariable("APPLYPILOT_FLEET_DSN", $dsn, "User")
$env:FLEET_PG_DSN = $dsn
$env:APPLYPILOT_FLEET_DSN = $dsn

# --- 7. Test the actual Postgres connection over the LAN ---
Say "`n  testing Postgres over the LAN ..." Cyan
& $py -c "from applypilot.apply import pgqueue; pgqueue.connect(); print('CONNECTED to the fleet Postgres over the LAN')"

# --- 8. Wire the Codex bridge into ~/.codex/config.toml (single-quoted = literal Windows paths) ---
$codexDir = Join-Path $env:USERPROFILE ".codex"
New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
$block = @"
[mcp_servers.applypilot-fleet]
command = '$py'
args = ["-m", "applypilot.fleet.codex_bridge"]
cwd = '$installDir'
enabled = true

[mcp_servers.applypilot-fleet.env]
FLEET_PG_DSN = '$dsn'
"@
$cfg = Join-Path $codexDir "config.toml"
if (Test-Path $cfg){ Add-Content $cfg "`n$block"; Say "  appended the fleet bridge to existing ~/.codex/config.toml" Green }
else { Set-Content $cfg $block -Encoding utf8; Say "  wrote ~/.codex/config.toml" Green }

Say "`n=== DONE ===" Green
Say "Open Codex on this machine and ask:  'what is the applypilot fleet status?'"
Say "It should call fleet_status and report the live numbers from the home box's Postgres."
Say "(If a tool says 'FLEET_PG_DSN is not set', fully restart Codex so it re-reads config.toml.)"
Say "`nTo also have THIS machine APPLY jobs (not just monitor), that's the heavier worker setup"
Say "(Playwright browser + an authed apply agent) -- ask and I'll extend this."
