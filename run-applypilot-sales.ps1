$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ApplyPilotDir = Join-Path $ProjectRoot ".applypilot"

New-Item -ItemType Directory -Force -Path $ApplyPilotDir | Out-Null

$env:APPLYPILOT_DB_PATH = Join-Path $ApplyPilotDir "applypilot_sales.db"
$env:APPLYPILOT_RESUME_PATH = Join-Path $ApplyPilotDir "resume_sales.txt"
$env:APPLYPILOT_RESUME_STRATEGY_PATH = Join-Path $ApplyPilotDir "resume_strategy_sales.yaml"
$env:APPLYPILOT_SEARCH_CONFIG_PATH = Join-Path $ApplyPilotDir "searches_sales.yaml"

Write-Host "ApplyPilot sales lane"
Write-Host "  Resume:  $env:APPLYPILOT_RESUME_PATH"
Write-Host "  Search:  $env:APPLYPILOT_SEARCH_CONFIG_PATH"
Write-Host "  DB:      $env:APPLYPILOT_DB_PATH"
Write-Host ""

& (Join-Path $ProjectRoot "run-applypilot.ps1") @args
exit $LASTEXITCODE
