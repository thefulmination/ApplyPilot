# status-m4.ps1 [-Label m4]
#   Show live fleet status for one machine (default m4). Double-click or run in PowerShell.
#   Works on any box: inherits FLEET_PG_DSN (set by setup), else the home box's local PG.
param([string]$Label = "m4")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root
$py = @(".\.conda-env\python.exe", ".\.venv\Scripts\python.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $py) { throw "python not found (.conda-env or .venv)." }
$env:PYTHONUTF8 = "1"
& $py (Join-Path $root "fleet-status.py") $Label
