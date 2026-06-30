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
# Backups go to a LOCAL versioned folder (NOT OneDrive: syncing the ~1GB db held OS file
# locks -> "database is locked" crashes, and a single corrupt run could clobber the only
# backup). The legacy OneDrive copy is kept read-only as a seed fallback during transition.
$BackupDir = Join-Path $LocalDbDir "db-backups"
$KeepBackups = 8
$OneDriveDbBackup = Join-Path $ApplyPilotDir "applypilot.db"   # legacy; seed fallback only

# Seed the live DB if missing/implausibly small: prefer newest local backup, else legacy OneDrive copy.
$NewestLocalBackup = Get-ChildItem -Path $BackupDir -Filter "applypilot-*.db" -ErrorAction SilentlyContinue |
    Sort-Object LastWriteTime -Descending | Select-Object -First 1
$SeedSource = if ($NewestLocalBackup) { $NewestLocalBackup.FullName }
              elseif (Test-Path -LiteralPath $OneDriveDbBackup) { $OneDriveDbBackup }
              else { $null }
if ($SeedSource) {
    $SeedItem = Get-Item -LiteralPath $SeedSource
    $LiveDb = Get-Item -LiteralPath $env:APPLYPILOT_DB_PATH -ErrorAction SilentlyContinue
    if (-not $LiveDb -or ($SeedItem.Length -gt 1048576 -and $LiveDb.Length -lt ($SeedItem.Length / 2))) {
        Copy-Item -LiteralPath $SeedSource -Destination $env:APPLYPILOT_DB_PATH -Force
        Write-Host "[run-applypilot] Seeded local DB from $($SeedItem.Name)."
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

# Back up the live LOCAL db to a LOCAL versioned folder after WRITE commands. NOT OneDrive
# (syncing a ~1GB db locked the file and a single corrupt run could clobber the only backup).
# Integrity gate: refuse to back up a corrupt db. Versioned: keep newest $KeepBackups so a bad
# run can't wipe prior good copies. Read-only commands are skipped. Non-fatal.
$WriteCmds = @("run","apply","discover","enrich","score","audit","diagnose","tailor","cover","pdf",
               "verify-live","resolve-ats-boards","resolve-company-apply-urls","boost-output","dedupe-jobs","rescore-jobs","scan-gmail")
if ($args.Count -gt 0 -and $WriteCmds -contains $args[0]) {
    try {
        $LiveDb = Get-Item -LiteralPath $env:APPLYPILOT_DB_PATH -ErrorAction Stop
        # Integrity gate: never save corruption over good backups (a corrupt clobber is what bit us).
        $DbOk = $true
        $Sqlite3 = Join-Path $ProjectRoot ".conda-env\Library\bin\sqlite3.exe"
        if (Test-Path -LiteralPath $Sqlite3) {
            try {
                $chk = (& $Sqlite3 -readonly $env:APPLYPILOT_DB_PATH "PRAGMA quick_check(1);" 2>$null | Out-String).Trim()
                if ($chk -and $chk -ne "ok") {
                    $DbOk = $false
                    Write-Warning "[run-applypilot] DB failed quick_check -- SKIPPING backup to avoid saving corruption ($chk)."
                }
            } catch { }  # if the check itself can't run, fall through and back up anyway
        }
        if ($DbOk) {
            New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
            # Checkpoint WAL into the main db so a plain file copy is complete + consistent.
            if (Test-Path -LiteralPath $Sqlite3) { & $Sqlite3 $env:APPLYPILOT_DB_PATH "PRAGMA wal_checkpoint(TRUNCATE);" 2>$null | Out-Null }
            $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
            $dest = Join-Path $BackupDir "applypilot-$stamp.db"
            Copy-Item -LiteralPath $env:APPLYPILOT_DB_PATH -Destination $dest -Force -ErrorAction Stop
            Get-ChildItem -Path $BackupDir -Filter "applypilot-*.db" -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending | Select-Object -Skip $KeepBackups |
                Remove-Item -Force -ErrorAction SilentlyContinue
            Write-Host "[run-applypilot] DB backed up locally -> $dest"
        }
    } catch { Write-Warning "[run-applypilot] local DB backup failed: $_" }
}
exit $rc
