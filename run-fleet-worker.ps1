# run-fleet-worker.ps1 [-Slot N] [-Agent claude|codex] [-Model name] [-Label name]
#   Launch ONE offsite apply worker. Use a DISTINCT -Slot per worker on the same machine
#   (slot keys the browser profile + CDP port + logs so they don't collide).
#   Use a DISTINCT -Label per MACHINE (home box = "home", second box = e.g. "m2") so the
#   worker-id (= "<Label>-<Slot>") is unique fleet-wide -- otherwise two machines both stamp
#   "home-0" and lease attribution / monitoring can't tell them apart.
#   Run each in its own window:  .\run-fleet-worker.ps1 -Slot 0 -Agent codex -Label m2
#   Requires FLEET_PG_DSN already set (the setup script persists it). LinkedIn never runs here.
param([int]$Slot = 0, [string]$Agent = "claude", [string]$Model = "", [string]$Label = "home")
$WorkerId = "$Label-$Slot"
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# Guard against the multi-tab collision: refuse a SECOND worker on the SAME worker-id/slot.
# Multiple processes on one slot share ONE Chrome profile + CDP debug port and fight over it
# (the "3-4 tabs, stuck/crash" symptom). For parallelism use DISTINCT -Slot values (each gets
# its own isolated browser), or run-fleet-workers.ps1 -Count N.
$dupe = Get-CimInstance Win32_Process -Filter "Name='applypilot-fleet-apply.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -match ('--worker-id\s+"?' + [regex]::Escape($WorkerId) + '"?(\s|$)') }
if ($dupe) {
  throw "Worker '$WorkerId' (slot $Slot) is ALREADY running (PID $($dupe.ProcessId -join ', ')). Another worker on the SAME slot collides on one Chrome. Use a different -Slot for parallelism, or stop the existing one first."
}

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
Write-Host "[fleet-worker] worker $WorkerId  agent=$Agent  model=$(if($Model){$Model}else{'default'})  -> logs .applypilot\logs\worker-$Slot.log"
& $exe --dsn $env:FLEET_PG_DSN --worker-id "$WorkerId" --chrome-slot $Slot --agent $Agent @margs
