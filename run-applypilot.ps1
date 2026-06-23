$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ApplyPilotDir = Join-Path $ProjectRoot ".applypilot"
$PythonScripts = Join-Path $ProjectRoot ".conda-env\Scripts"
$ClaudeBin = Join-Path $ProjectRoot ".tools\claude\node_modules\.bin"
$ClaudeExeBin = Join-Path $ProjectRoot ".tools\claude\node_modules\@anthropic-ai\claude-code\bin"
$NodeBin = "C:\Program Files\nodejs"

New-Item -ItemType Directory -Force -Path $ApplyPilotDir | Out-Null

$env:APPLYPILOT_DIR = $ApplyPilotDir
# Activate the tuned search config. This MUST be set in the process environment here:
# config.py freezes SEARCH_CONFIG_PATH at import, before .applypilot/.env is loaded,
# so the .env override alone is ignored. Mirrors run-applypilot-sales.ps1.
$env:APPLYPILOT_SEARCH_CONFIG_PATH = Join-Path $ApplyPilotDir "searches_tuned.yaml"
# Floor title-certain Chief-of-Staff / Strategy-&-Ops roles (role_fit>=90) above the
# apply gate so the LLM's pivot-penalized base_score can't bury them. See audit.py.
$env:APPLYPILOT_COS_RESCUE = "1"
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $ProjectRoot ".playwright-browsers"
$Chromium = Get-ChildItem -Path $env:PLAYWRIGHT_BROWSERS_PATH -Directory -Filter "chromium-*" -ErrorAction SilentlyContinue |
    Sort-Object Name -Descending |
    Select-Object -First 1
if ($Chromium) {
    $env:CHROME_PATH = Join-Path $Chromium.FullName "chrome-win64\chrome.exe"
}
$env:npm_config_cache = Join-Path $ProjectRoot ".npm-cache"
$env:npm_config_ignore_scripts = "true"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
$env:Path = "$PythonScripts;$ClaudeExeBin;$ClaudeBin;$NodeBin;$env:Path"

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

& (Join-Path $PythonScripts "applypilot.exe") @args
exit $LASTEXITCODE
