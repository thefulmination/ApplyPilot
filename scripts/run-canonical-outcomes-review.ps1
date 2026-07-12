param(
    [int]$Port = 8766,
    [string]$DbPath = "$env:LOCALAPPDATA\ApplyPilot\applypilot.db"
)

$ErrorActionPreference = "Stop"
$worktreeRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\..\.."))
$python = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "ApplyPilot virtualenv Python not found: $python"
}
if (-not (Test-Path -LiteralPath $DbPath)) {
    throw "ApplyPilot brain not found: $DbPath"
}

$env:PYTHONPATH = Join-Path $worktreeRoot "src"
$env:APPLYPILOT_DB_PATH = $DbPath
$code = @"
import os
from applypilot.outcome_dashboard import serve
serve(host="127.0.0.1", port=$Port, db_path=os.environ["APPLYPILOT_DB_PATH"])
"@

& $python -c $code
