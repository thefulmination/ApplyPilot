# run-fleet-worker.ps1 <slot>  --  launch ONE offsite apply worker.
# Each worker needs a distinct slot (0,1,2,...) so its browser/port/logs don't collide.
# Run each in its OWN PowerShell window:
#     .\run-fleet-worker.ps1 0
#     .\run-fleet-worker.ps1 1
#     .\run-fleet-worker.ps1 2
# Requires FLEET_PG_DSN already persisted (setx). LinkedIn does NOT run here -- offsite only.
param([int]$Slot = 0)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# Profile / resume / config (the bare worker doesn't replicate run-applypilot.ps1's env)
$env:APPLYPILOT_DIR = Join-Path $ProjectRoot ".applypilot"
# Bundled Playwright Chromium
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $ProjectRoot ".playwright-browsers"
$chromium = Get-ChildItem -Path $env:PLAYWRIGHT_BROWSERS_PATH -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
if ($chromium) { $env:CHROME_PATH = Join-Path $chromium.FullName "chrome-win64\chrome.exe" }
# node (for the agent's Playwright MCP) + the AUTHENTICATED global claude
$env:Path = "C:\Program Files\nodejs;$env:Path"
$globalClaude = Join-Path $env:APPDATA "npm\claude.cmd"
if (Test-Path $globalClaude) { $env:CLAUDE_PATH = $globalClaude }
# Per-slot throwaway cost-DB (NEVER the real brain; distinct per worker to avoid SQLite contention)
$env:APPLYPILOT_DB_PATH = Join-Path $env:TEMP "fleet_apply_throwaway_$Slot.db"
$env:APPLYPILOT_ENABLE_GMAIL_MCP = "1"      # read verification codes from Gmail
$env:APPLYPILOT_AGENT_TIMEOUT = "600"       # 10 min -- verify-heavy forms need it
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"

if (-not $env:FLEET_PG_DSN) { throw "FLEET_PG_DSN is not set (persist it once with setx, then open a new window)." }

Write-Host "[fleet-worker] launching worker 'home-$Slot' (browser slot $Slot, logs -> .applypilot\logs\worker-$Slot.log)"
& ".\.conda-env\Scripts\applypilot-fleet-apply.exe" --dsn $env:FLEET_PG_DSN --worker-id "home-$Slot" --chrome-slot $Slot --agent claude --model sonnet
