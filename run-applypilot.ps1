$ErrorActionPreference = "Stop"

$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$ApplyPilotDir = Join-Path $ProjectRoot ".applypilot"
$PythonScripts = Join-Path $ProjectRoot ".conda-env\Scripts"
$ClaudeBin = Join-Path $ProjectRoot ".tools\claude\node_modules\.bin"
$ClaudeExeBin = Join-Path $ProjectRoot ".tools\claude\node_modules\@anthropic-ai\claude-code\bin"
$NodeBin = "C:\Program Files\nodejs"

New-Item -ItemType Directory -Force -Path $ApplyPilotDir | Out-Null

$env:APPLYPILOT_DIR = $ApplyPilotDir
# DB lives on LOCAL disk (NOT OneDrive): OneDrive holds OS file locks on the ~750MB
# db during sync -> "database is locked" crashes that busy_timeout cannot wait out
# (a foreign lock). The .applypilot copy is kept as a BACKUP, refreshed after write
# commands below. APPLYPILOT_DB_PATH overrides config's default of $APPLYPILOT_DIR/applypilot.db.
$LocalDbDir = Join-Path $env:LOCALAPPDATA "ApplyPilot"
New-Item -ItemType Directory -Force -Path $LocalDbDir | Out-Null
$env:APPLYPILOT_DB_PATH = Join-Path $LocalDbDir "applypilot.db"
$OneDriveDbBackup = Join-Path $ApplyPilotDir "applypilot.db"
if (Test-Path -LiteralPath $OneDriveDbBackup) {
    $BackupDb = Get-Item -LiteralPath $OneDriveDbBackup
    $LiveDb = Get-Item -LiteralPath $env:APPLYPILOT_DB_PATH -ErrorAction SilentlyContinue
    if (-not $LiveDb -or ($BackupDb.Length -gt 1048576 -and $LiveDb.Length -lt ($BackupDb.Length / 2))) {
        Copy-Item -LiteralPath $OneDriveDbBackup -Destination $env:APPLYPILOT_DB_PATH -Force
        Write-Host "[run-applypilot] Seeded local DB from OneDrive backup."
    }
}
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

# The apply agent must use the AUTHENTICATED global claude (npm -g install), NOT the
# .tools\claude copy that the PATH prepend above resolves to -- that copy isn't logged
# in, so the auth canary aborts. config.get_claude_path() honors CLAUDE_PATH first, so
# pin it. (Codex resolves via config.get_codex_path -> .tools\codex, unaffected.)
$GlobalClaude = Join-Path $env:APPDATA "npm\claude.cmd"
if (Test-Path -LiteralPath $GlobalClaude) { $env:CLAUDE_PATH = $GlobalClaude }

[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()

& (Join-Path $PythonScripts "applypilot.exe") @args
$rc = $LASTEXITCODE

# Back up the live LOCAL db to OneDrive after commands that WRITE it (OneDrive copy =
# backup, not the live db). Read-only commands are skipped so we don't copy ~750MB
# after every `status`. Failure to back up is non-fatal.
$WriteCmds = @("run","apply","discover","enrich","score","audit","diagnose","tailor","cover","pdf",
               "verify-live","resolve-ats-boards","resolve-company-apply-urls","boost-output","dedupe-jobs","rescore-jobs","scan-gmail")
if ($args.Count -gt 0 -and $WriteCmds -contains $args[0]) {
    try {
        $LiveDb = Get-Item -LiteralPath $env:APPLYPILOT_DB_PATH -ErrorAction Stop
        $BackupDb = Get-Item -LiteralPath $OneDriveDbBackup -ErrorAction SilentlyContinue
        if ($BackupDb -and $BackupDb.Length -gt 1048576 -and $LiveDb.Length -lt ($BackupDb.Length / 2)) {
            throw "Refusing to overwrite larger OneDrive DB backup ($($BackupDb.Length) bytes) with much smaller live DB ($($LiveDb.Length) bytes)."
        }
        Copy-Item -LiteralPath $env:APPLYPILOT_DB_PATH -Destination $OneDriveDbBackup -Force -ErrorAction Stop
        Write-Host "[run-applypilot] DB backed up to OneDrive."
    } catch { Write-Warning "[run-applypilot] DB backup to OneDrive failed: $_" }
}
exit $rc
