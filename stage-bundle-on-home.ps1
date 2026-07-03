#requires -Version 5.1
<#
  stage-bundle-on-home.ps1   (RUNS ON THE HOME BOX)
  ---------------------------------------------------------------------------
  Gathers everything machine 2 needs to run `applypilot run discover`+`score`
  into ONE local bundle folder: a consistent brain snapshot + every config file
  the discover/score stages read (which your live config scatters across
  .applypilot\, MasterResume\, and New project 9\). Then drops in the machine-2
  setup script. Copy the bundle to machine 2 and run that script there.

  The bundle contains your .env (API keys) -> it is written to a LOCAL,
  non-OneDrive folder; transport it off OneDrive and delete it after deploy.

  Usage:  .\stage-bundle-on-home.ps1            # -> C:\Users\<you>\m2bundle
          .\stage-bundle-on-home.ps1 -OutDir D:\m2bundle
#>
param([string]$OutDir = (Join-Path $env:USERPROFILE 'm2bundle'))
$ErrorActionPreference = 'Stop'

$Repo    = Split-Path -Parent $MyInvocation.MyCommand.Path
$AppCfg  = Join-Path $Repo '.applypilot'
$Py      = Join-Path $Repo '.conda-env\python.exe'
$Brain   = Join-Path $env:LOCALAPPDATA 'ApplyPilot\applypilot.db'
$BundleCfg = Join-Path $OutDir '.applypilot'

if (-not (Test-Path $Py))    { throw "home python not found: $Py" }
if (-not (Test-Path $Brain)) { throw "live brain not found: $Brain" }

Write-Host "=== staging machine-2 bundle -> $OutDir ===" -ForegroundColor Cyan
# Clean slate: a bundle from a prior run can leave stale (e.g. old-named) files behind.
if (Test-Path $OutDir) { Remove-Item -LiteralPath $OutDir -Recurse -Force }
New-Item -ItemType Directory -Force -Path $BundleCfg | Out-Null

# 1. Consistent brain snapshot via the sqlite backup API (reads a clean view of
#    the live DB incl. any WAL; never writes the source).
Write-Host '[..] snapshotting the brain (~1 GB, a moment) ...' -ForegroundColor Yellow
& $Py -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" $Brain (Join-Path $OutDir 'applypilot.db')
if ($LASTEXITCODE -ne 0) { throw 'brain snapshot failed' }
Write-Host "[ok] brain -> $(Join-Path $OutDir 'applypilot.db')" -ForegroundColor Green

# 2. Config files: source path -> bundle filename, required flag.
#    'applypilot-preference-profile.json' and 'Company_List.txt' keep their names
#    because the machine-2 script rewrites the .env/YAML dir-prefixes (not filenames).
$items = @(
  @{ src = (Join-Path $AppCfg '.env');                          dst = '.env';                              req = $true  }
  @{ src = (Join-Path $AppCfg 'profile.json');                  dst = 'profile.json';                      req = $true  }
  @{ src = (Join-Path $AppCfg 'resume.txt');                    dst = 'resume.txt';                        req = $true  }
  @{ src = (Join-Path $AppCfg 'searches_tuned.yaml');           dst = 'searches_tuned.yaml';               req = $true  }
  @{ src = (Join-Path $AppCfg 'job_knowledge_graph_prompt.md'); dst = 'job_knowledge_graph_prompt.md';     req = $true  }
  @{ src = 'C:\Users\JStal\OneDrive\Documents\MasterResume\Company_List.txt';                              dst = 'Company_List.txt'; req = $true }
  @{ src = 'C:\Users\JStal\OneDrive\Documents\New project 9\data\review\applypilot-preference-profile.json'; dst = 'applypilot-preference-profile.json'; req = $false }
  @{ src = (Join-Path $AppCfg 'searches.yaml');                 dst = 'searches.yaml';                     req = $false }
  @{ src = (Join-Path $AppCfg 'corporate_ats.yaml');            dst = 'corporate_ats.yaml';                req = $false }
  @{ src = (Join-Path $AppCfg 'corporate_ats_cache.json');      dst = 'corporate_ats_cache.json';          req = $false }
  @{ src = (Join-Path $AppCfg 'workday_employers.yaml');        dst = 'workday_employers.yaml';            req = $false }
  @{ src = (Join-Path $AppCfg 'resume.pdf');                    dst = 'resume.pdf';                        req = $false }
)
foreach ($i in $items) {
  if (Test-Path $i.src) {
    Copy-Item -LiteralPath $i.src -Destination (Join-Path $BundleCfg $i.dst) -Force
    Write-Host ("  [ok] {0}" -f $i.dst) -ForegroundColor Green
  } elseif ($i.req) {
    throw "REQUIRED file missing: $($i.src)"
  } else {
    Write-Host ("  [skip] {0} (optional, not found)" -f $i.dst) -ForegroundColor DarkYellow
  }
}

# 3. Drop in the machine-2 setup script.
$setup = Join-Path $Repo 'run-discover-score-on-machine2.ps1'
if (-not (Test-Path $setup)) { throw "run-discover-score-on-machine2.ps1 not found next to this script" }
Copy-Item -LiteralPath $setup -Destination (Join-Path $OutDir 'run-discover-score-on-machine2.ps1') -Force

# 4. Zip a clean transfer artifact next to the folder (overwrite any prior).
$Zip = "$OutDir.zip"
if (Test-Path $Zip) { Remove-Item -LiteralPath $Zip -Force }
Write-Host '[..] zipping bundle for transfer ...' -ForegroundColor Yellow
Compress-Archive -Path (Join-Path $OutDir '*') -DestinationPath $Zip
Write-Host ("[ok] {0}  ({1:N0} MB)" -f $Zip, ((Get-Item $Zip).Length/1MB)) -ForegroundColor Green

Write-Host "`n=== BUNDLE READY ===" -ForegroundColor Cyan
Write-Host "Next:" -ForegroundColor White
Write-Host "  1. Copy '$Zip' to machine 2 and extract it (NOT via OneDrive -- it has API keys)."
Write-Host "  2. On machine 2, from inside the copied folder:  powershell -ExecutionPolicy Bypass -File .\run-discover-score-on-machine2.ps1"
Write-Host "  3. Run the two launchers it creates: discover.ps1, then score.ps1 (under %LOCALAPPDATA%\ApplyPilot-DiscoverScore)."
Write-Host "  4. Delete the bundle on both machines afterward (it contains your .env keys)." -ForegroundColor DarkGray
