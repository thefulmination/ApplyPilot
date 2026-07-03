#requires -Version 5.1
<#
  run-discover-score-on-machine2.ps1   (RUNS ON MACHINE 2)
  ---------------------------------------------------------------------------
  Deploys a staged bundle into a DEDICATED, ISOLATED data dir and writes two
  launchers to run discover + score against your real brain + config.

  WHY isolated: the bundle's .applypilot files (profile.json, resume.txt,
  searches.yaml, .env, ...) have the SAME NAMES as machine 2's existing apply
  worker config in C:\ApplyPilot\.applypilot. We do NOT copy over that folder --
  it would clobber the running apply worker's config. Everything instead lands
  under %LOCALAPPDATA%\ApplyPilot-DiscoverScore\, and the launchers point
  ApplyPilot there for the discover/score run ONLY (process-scoped env). The
  apply worker's config + environment are left completely untouched.

  PRE-EXISTING on machine 2: git repo + venv at C:\ApplyPilot (the apply worker).

  RUN FROM the bundle folder produced by stage-bundle-on-home.ps1, which holds:
     .\applypilot.db   and   .\.applypilot\  (config files)
#>
$ErrorActionPreference = 'Stop'

$RepoDir = 'C:\ApplyPilot'
$VenvPy  = Join-Path $RepoDir '.venv\Scripts\python.exe'
$AppExe  = Join-Path $RepoDir '.venv\Scripts\applypilot.exe'

# Dedicated, isolated data root: NOT the apply config dir, NOT OneDrive, no admin.
$DataRoot  = Join-Path $env:LOCALAPPDATA 'ApplyPilot-DiscoverScore'
$ConfigDir = Join-Path $DataRoot '.applypilot'
$BrainPath = Join-Path $DataRoot 'applypilot.db'
$ConfigFwd = ($ConfigDir -replace '\\','/')

$BundleDir       = (Get-Location).Path
$StagedBrain     = Join-Path $BundleDir 'applypilot.db'
$StagedConfigDir = Join-Path $BundleDir '.applypilot'

Write-Host '=== ApplyPilot machine-2 discover+score setup (isolated) ===' -ForegroundColor Cyan

# 1. venv + entrypoint -------------------------------------------------------
if (-not (Test-Path $VenvPy)) { throw "venv python not found: $VenvPy" }
if (-not (Test-Path $AppExe)) { throw "applypilot.exe not found: $AppExe (is the package installed in the venv?)" }
Write-Host '[ok] venv + applypilot.exe present' -ForegroundColor Green

# 2. jobspy ------------------------------------------------------------------
& $VenvPy -c 'import jobspy' 2>$null
if ($LASTEXITCODE -ne 0) {
  Write-Host '[..] installing python-jobspy (--no-deps)' -ForegroundColor Yellow
  & $VenvPy -m pip install --no-deps python-jobspy
  & $VenvPy -c 'import jobspy' 2>$null
  if ($LASTEXITCODE -ne 0) { throw 'python-jobspy installed but still not importable (pandas/numpy?)' }
}
Write-Host '[ok] jobspy importable' -ForegroundColor Green

# 3. deploy brain + config into the ISOLATED data root -----------------------
# -Force only ever overwrites OUR own dir (re-runs refresh from the latest bundle);
# machine 2's C:\ApplyPilot\.applypilot is never read or written here.
if (-not (Test-Path $StagedBrain))     { throw "staged brain not found: $StagedBrain (run from the bundle folder)" }
if (-not (Test-Path $StagedConfigDir)) { throw "staged .applypilot folder not found: $StagedConfigDir" }
New-Item -ItemType Directory -Force -Path $DataRoot, $ConfigDir | Out-Null
Copy-Item -Path $StagedBrain -Destination $BrainPath -Force
foreach ($sfx in '-wal','-shm') { $s = "$BrainPath$sfx"; if (Test-Path $s) { Remove-Item $s -Force } }
Copy-Item -Path (Join-Path $StagedConfigDir '*') -Destination $ConfigDir -Recurse -Force
Write-Host "[ok] deployed -> $DataRoot" -ForegroundColor Green
Write-Host "     (machine 2's C:\ApplyPilot\.applypilot apply config left untouched)" -ForegroundColor DarkGray
foreach ($req in 'profile.json','resume.txt') {
  if (-not (Test-Path (Join-Path $ConfigDir $req))) { throw "$req missing in the bundle -- required; re-stage it." }
}

# 4. rewrite home-box absolute paths -> this isolated ConfigDir --------------
$rewriteMap = [ordered]@{
  'C:[\\/]Users[\\/]JStal[\\/]OneDrive[\\/]Documents[\\/]New project[\\/]ApplyPilot[\\/]\.applypilot' = $ConfigFwd
  'C:[\\/]Users[\\/]JStal[\\/]OneDrive[\\/]Documents[\\/]New project 9[\\/]data[\\/]review'           = $ConfigFwd
  'C:[\\/]Users[\\/]JStal[\\/]OneDrive[\\/]Documents[\\/]MasterResume'                                 = $ConfigFwd
}
function Repair-AbsolutePaths([string]$FilePath) {
  if (-not (Test-Path $FilePath)) { return }
  $text = Get-Content -LiteralPath $FilePath -Raw
  foreach ($pat in $rewriteMap.Keys) { $text = [regex]::Replace($text, $pat, $rewriteMap[$pat], 'IgnoreCase') }
  Set-Content -LiteralPath $FilePath -Value $text -NoNewline -Encoding UTF8
}
Repair-AbsolutePaths (Join-Path $ConfigDir '.env')
foreach ($y in 'searches.yaml','searches_tuned.yaml') { Repair-AbsolutePaths (Join-Path $ConfigDir $y) }
Write-Host '[ok] home-box absolute paths rewritten -> isolated ConfigDir' -ForegroundColor Green

# 4b. sanity: overrides resolve to staged files ------------------------------
$envText = Get-Content -LiteralPath (Join-Path $ConfigDir '.env') -Raw
$mS = [regex]::Match($envText, '(?im)^\s*APPLYPILOT_SEARCH_CONFIG_PATH\s*=\s*(.+?)\s*$')
if ($mS.Success -and -not (Test-Path $mS.Groups[1].Value.Trim())) { throw "APPLYPILOT_SEARCH_CONFIG_PATH -> '$($mS.Groups[1].Value.Trim())' missing after rewrite; stage that YAML." }
$mP = [regex]::Match($envText, '(?im)^\s*APPLYPILOT_PREFERENCE_PROFILE_PATH\s*=\s*(.+?)\s*$')
if ($mP.Success -and -not (Test-Path $mP.Groups[1].Value.Trim())) { Write-Host "[warn] preference profile missing after rewrite; scoring UNCALIBRATED." -ForegroundColor Yellow }
if (-not (Test-Path (Join-Path $ConfigDir 'Company_List.txt'))) { Write-Host "[warn] Company_List.txt absent; corporate_ats / Workday watchlist contributes 0 jobs." -ForegroundColor Yellow }

# 5. tier preflight (process-scoped APP dir so get_tier reads the isolated .env)
$env:APPLYPILOT_DIR = $ConfigDir
$tier = (& $VenvPy -c 'from applypilot import config; print(config.get_tier())' | Out-String).Trim()
if ($tier -notmatch '^\d+$' -or [int]$tier -lt 2) {
  Write-Host "`n[STOP] config.get_tier() = '$tier' (need >= 2). Put an LLM key in $ConfigDir\.env (DEEPSEEK_API_KEY=...)." -ForegroundColor Red
  exit 1
}
Write-Host "[ok] tier preflight passed (tier $tier)" -ForegroundColor Green

# 6. write two launchers -- env is PROCESS-scoped, so the apply worker's
#    environment is never changed. Discover and score are SEPARATE on purpose:
#    `run discover score --workers N` would also score at N concurrent LLM calls.
$discover = @"
# discover into the isolated brain. --workers is RAM/network-bound -> push it.
`$env:APPLYPILOT_DIR     = '$ConfigDir'
`$env:APPLYPILOT_DB_PATH = '$BrainPath'
& '$AppExe' run discover --workers 16 --discover-mode full
"@
$score = @"
# score the discovered jobs. --workers is DeepSeek-rate-limited -> keep it low (4).
`$env:APPLYPILOT_DIR     = '$ConfigDir'
`$env:APPLYPILOT_DB_PATH = '$BrainPath'
& '$AppExe' run score --workers 4
"@
Set-Content -LiteralPath (Join-Path $DataRoot 'discover.ps1') -Value $discover -Encoding UTF8
Set-Content -LiteralPath (Join-Path $DataRoot 'score.ps1')    -Value $score    -Encoding UTF8
Write-Host '[ok] launchers written' -ForegroundColor Green

# 7. next steps --------------------------------------------------------------
Write-Host "`n=== READY. Run these two, in order (separate commands): ===" -ForegroundColor Cyan
Write-Host "  & '$(Join-Path $DataRoot 'discover.ps1')'" -ForegroundColor White
Write-Host "  & '$(Join-Path $DataRoot 'score.ps1')'" -ForegroundColor White
Write-Host "`n  Brain:  $BrainPath" -ForegroundColor DarkGray
Write-Host "  Config: $ConfigDir   (isolated -- apply worker untouched)" -ForegroundColor DarkGray
