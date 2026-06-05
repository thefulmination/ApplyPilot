$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ApplyPilotDir = Join-Path $ProjectRoot ".applypilot"
$PythonScripts = Join-Path $ProjectRoot ".conda-env\Scripts"
$ClaudeBin = Join-Path $ProjectRoot ".tools\claude\node_modules\.bin"
$ClaudeExeBin = Join-Path $ProjectRoot ".tools\claude\node_modules\@anthropic-ai\claude-code\bin"
$NodeBin = "C:\Program Files\nodejs"

New-Item -ItemType Directory -Force -Path $ApplyPilotDir | Out-Null

$env:APPLYPILOT_DIR = $ApplyPilotDir
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
