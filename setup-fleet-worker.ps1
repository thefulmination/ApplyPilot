<#
================================================================================
  setup-fleet-worker.ps1
  Bootstrap a FRESH Windows machine into a full ApplyPilot APPLY WORKER (Codex
  agent), talking to the home box's Postgres over Tailscale or your private LAN.

  PREREQS:
    - This machine can reach the home box's Tailscale IP, or both boxes are on the same private LAN.
    - The home box already opened Postgres to the LAN (see setup-fleet-machine.ps1).

  MANUAL bits the script can't do (interactive logins / personal files):
    1. GitHub sign-in when it clones the private repo.
    2. 'codex login' once at the end (the script installs the Codex CLI).
    3. A copy of the home box's .applypilot folder (profile.json + resume + searches.yaml)
       -- personal + gitignored, so not in the repo. Copy it here; give the path when asked.

  RUN (normal PowerShell, NOT admin):
      powershell -ExecutionPolicy Bypass -File .\setup-fleet-worker.ps1
================================================================================
#>
param([string]$HomeIp = "100.90.104.99", [string]$InstallDir = "C:\ApplyPilot")
$ErrorActionPreference = "Stop"
function Say($m,$c="White"){ Write-Host $m -ForegroundColor $c }
function Refresh-Path { $env:Path = [Environment]::GetEnvironmentVariable("Path","Machine") + ";" + [Environment]::GetEnvironmentVariable("Path","User") }
function Set-CapSolverKey([string]$InstallDir) {
  $capKey = (Read-Host "  CapSolver API key (Enter to skip)").Trim()
  if (-not $capKey) { Say "  SKIPPED -- set CAPSOLVER_API_KEY before launching apply workers." Yellow; return }

  [Environment]::SetEnvironmentVariable("CAPSOLVER_API_KEY", $capKey, "User")
  $env:CAPSOLVER_API_KEY = $capKey
  $appDir = Join-Path $InstallDir ".applypilot"
  New-Item -ItemType Directory -Force -Path $appDir | Out-Null
  $envFile = Join-Path $appDir ".env"
  $lines = @()
  if (Test-Path $envFile) { $lines = @(Get-Content $envFile | Where-Object { $_ -notmatch '^CAPSOLVER_API_KEY=' }) }
  $lines += "CAPSOLVER_API_KEY=$capKey"
  Set-Content -Path $envFile -Value $lines -Encoding ascii
  Say "  [ok] CapSolver key saved for this Windows user and $envFile" Green
}

Say "`n=== ApplyPilot fleet -- WORKER bootstrap (Codex agent) ===" Cyan

# --- 0. On the private LAN? ---
$gw = (Get-NetIPConfiguration | Where-Object { $_.IPv4DefaultGateway } | Select-Object -First 1).IPv4DefaultGateway.NextHop
Say "primary gateway: $gw" Gray
if ($gw -notlike '192.168.*' -and $gw -notlike '10.*' -and $gw -notlike '172.*') {
  Say "WARNING: not on a private LAN -- connect to the same router as the home box first." Yellow
}

# --- 1. Reach the home box's Postgres ---
Say "`n[1/8] checking $HomeIp:5432 is reachable ..." Cyan
if (-not (Test-NetConnection $HomeIp -Port 5432 -WarningAction SilentlyContinue).TcpTestSucceeded) {
  Say "  cannot reach $HomeIp:5432 -- confirm same router + the home box opened PG to the LAN." Yellow
  if ((Read-Host "  continue anyway? (y/n)") -ne 'y') { exit 1 }
} else { Say "  reachable." Green }

# --- 2. Base tools via winget ---
Say "`n[2/8] installing Git / Python / Node (winget; skips present) ..." Cyan
function Ensure($name,$id,$cmd){ if (Get-Command $cmd -ErrorAction SilentlyContinue){ Say "  [ok] $name" Green } else { Say "  installing $name ..."; winget install -e --id $id --accept-source-agreements --accept-package-agreements } }
Ensure "Git"         "Git.Git"            "git"
Ensure "Python 3.12" "Python.Python.3.12" "py"
Ensure "Node.js LTS" "OpenJS.NodeJS.LTS"  "node"
Refresh-Path
foreach ($c in @("git","py","node","npm")) {
  if (-not (Get-Command $c -ErrorAction SilentlyContinue)) {
    Say "  '$c' still not on PATH. CLOSE this window, open a NEW PowerShell, and re-run this script (winget PATH changes need a fresh session)." Yellow
    exit 1
  }
}

# --- 3. Codex CLI (the apply agent) ---
Say "`n[3/8] installing the Codex CLI ..." Cyan
if (Get-Command codex -ErrorAction SilentlyContinue){ Say "  [ok] codex present" Green } else { npm install -g @openai/codex; Refresh-Path }

# --- 4. Clone the fleet repo (GitHub sign-in if prompted) ---
Say "`n[4/8] cloning the fleet repo -> $InstallDir ..." Cyan
$repo = "https://github.com/thefulmination/applypilot-private.git"
if (-not (Test-Path (Join-Path $InstallDir ".git"))) { git clone $repo $InstallDir } else { Say "  [ok] already cloned" Green }
Set-Location $InstallDir

# --- 5. Python venv + deps + Playwright Chromium ---
Say "`n[5/8] Python env + dependencies + browser (a few minutes) ..." Cyan
if (-not (Test-Path ".\.venv")) { py -3 -m venv .venv }
$py = (Resolve-Path ".\.venv\Scripts\python.exe").Path
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -e . --quiet
& $py -m pip install "psycopg[binary]" mcp pyyaml --quiet
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $InstallDir ".playwright-browsers"
& $py -m playwright install chromium

# --- 6. Personal config (profile / resume / searches) from the home box ---
Say "`n[6/8] your apply profile + resume + search config ..." Cyan
$dest = Join-Path $InstallDir ".applypilot"; New-Item -ItemType Directory -Force -Path $dest | Out-Null
if (-not (Test-Path (Join-Path $dest "profile.json"))) {
  $src = (Read-Host "  Path to a copy of the home box's .applypilot folder (Enter to skip + copy later)").Trim()
  if ($src -and (Test-Path $src)) {
    foreach ($f in @("profile.json","resume.pdf","resume.txt","searches.yaml","job_preference_profile.json","resume_strategy.yaml")) {
      $p = Join-Path $src $f; if (Test-Path $p) { Copy-Item $p $dest -Force; Say "    copied $f" Green }
    }
  } else { Say "  SKIPPED -- copy profile.json + resume.pdf + resume.txt + searches.yaml into $dest before launching a worker." Yellow }
} else { Say "  [ok] profile already present" Green }

Say "`n[6b/8] CapSolver CAPTCHA service ..." Cyan
Set-CapSolverKey $InstallDir

# --- 7. Postgres connectivity (LAN, passwordless via pgpass) ---
Say "`n[7/8] Postgres connection ..." Cyan
$pgPw = (Read-Host "  Postgres password (home box's 'postgres' user)").Trim()
$pgpassDir = Join-Path $env:APPDATA "postgresql"; New-Item -ItemType Directory -Force -Path $pgpassDir | Out-Null
Set-Content -Path (Join-Path $pgpassDir "pgpass.conf") -Value "${HomeIp}:5432:*:postgres:${pgPw}" -Encoding ascii
$dsn = "host=$HomeIp port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5"
[Environment]::SetEnvironmentVariable("FLEET_PG_DSN", $dsn, "User")
[Environment]::SetEnvironmentVariable("APPLYPILOT_FLEET_DSN", $dsn, "User")
$env:FLEET_PG_DSN = $dsn; $env:APPLYPILOT_FLEET_DSN = $dsn
& $py -c "from applypilot.apply import pgqueue; pgqueue.connect(); print('  CONNECTED to the fleet Postgres')"

# --- 8. Codex fleet bridge (so this machine can also MONITOR the fleet from Codex) ---
Say "`n[8/8] wiring the Codex fleet bridge into ~/.codex/config.toml ..." Cyan
$codexDir = Join-Path $env:USERPROFILE ".codex"; New-Item -ItemType Directory -Force -Path $codexDir | Out-Null
$block = @"
[mcp_servers.applypilot-fleet]
command = '$py'
args = ["-m", "applypilot.fleet.codex_bridge"]
cwd = '$InstallDir'
enabled = true

[mcp_servers.applypilot-fleet.env]
FLEET_PG_DSN = '$dsn'
"@
$cfg = Join-Path $codexDir "config.toml"
if (Test-Path $cfg){ Add-Content $cfg "`n$block" } else { Set-Content $cfg $block -Encoding utf8 }
Say "  done." Green

Say "`n=== BOOTSTRAP COMPLETE ===" Green
Say "Two steps left, then you're a live worker:" Yellow
Say "  1. Sign into the apply agent:   codex login"
Say "  2. (only if step 6 was skipped) put profile.json + resume.pdf + resume.txt + searches.yaml into $InstallDir\.applypilot"
Say "`nThen launch a worker -- open a NEW window so it picks up FLEET_PG_DSN:" Cyan
Say "  cd $InstallDir"
Say "  .\run-fleet-worker.ps1 -Slot 0 -Agent codex"
Say "`nIt leases jobs from the home box's Postgres and applies, all from this machine."
Say "Run multiple workers with different slots (-Slot 0, -Slot 1, ...) for more throughput."
