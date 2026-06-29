# run-fleet-worker.ps1 [-Slot N] [-Agent claude|codex] [-Model name]
#   Launch ONE offsite apply worker. Use a DISTINCT -Slot per worker on the same machine
#   (slot keys the browser profile + CDP port + logs so they don't collide).
#   Run each in its own window:  .\run-fleet-worker.ps1 -Slot 0 -Agent codex
#   Requires FLEET_PG_DSN already set (the setup script persists it). LinkedIn never runs here.
param([int]$Slot = 0, [string]$Agent = "claude", [string]$Model = "")
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# Python env: the home box uses .conda-env; a bootstrapped machine uses .venv. Find whichever exists.
$exe = $null
foreach ($d in @(".\.conda-env\Scripts", ".\.venv\Scripts")) {
  $cand = Join-Path $d "applypilot-fleet-apply.exe"
  if (Test-Path $cand) { $exe = (Resolve-Path $cand).Path; break }
}
if (-not $exe) { throw "applypilot-fleet-apply not found in .conda-env or .venv -- run the setup script first." }

$env:APPLYPILOT_DIR = Join-Path $ProjectRoot ".applypilot"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $ProjectRoot ".playwright-browsers"
$chromium = Get-ChildItem -Path $env:PLAYWRIGHT_BROWSERS_PATH -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending | Select-Object -First 1
if ($chromium) { $env:CHROME_PATH = Join-Path $chromium.FullName "chrome-win64\chrome.exe" }
$env:Path = "C:\Program Files\nodejs;$env:Path"

# Apply-agent CLI path (must be the AUTHENTICATED one): claude or codex
if ($Agent -eq "claude") {
  $a = Join-Path $env:APPDATA "npm\claude.cmd"; if (Test-Path $a) { $env:CLAUDE_PATH = $a }
  if (-not $Model) { $Model = "sonnet" }
} elseif ($Agent -eq "codex") {
  $a = Join-Path $env:APPDATA "npm\codex.cmd"; if (Test-Path $a) { $env:CODEX_PATH = $a }
  # leave $Model empty -> codex uses its default model
} else { throw "unknown -Agent '$Agent' (use claude or codex)" }

# Per-slot throwaway cost-DB (never the real brain), Gmail verification, 10-min timeout
$env:APPLYPILOT_DB_PATH = Join-Path $env:TEMP "fleet_apply_throwaway_$Slot.db"
$env:APPLYPILOT_ENABLE_GMAIL_MCP = "1"
$env:APPLYPILOT_AGENT_TIMEOUT = "600"
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"

if (-not $env:FLEET_PG_DSN) { throw "FLEET_PG_DSN is not set (the setup script persists it; open a fresh window)." }

$margs = @(); if ($Model) { $margs = @("--model", $Model) }
Write-Host "[fleet-worker] worker home-$Slot  agent=$Agent  model=$(if($Model){$Model}else{'default'})  -> logs .applypilot\logs\worker-$Slot.log"
& $exe --dsn $env:FLEET_PG_DSN --worker-id "home-$Slot" --chrome-slot $Slot --agent $Agent @margs
