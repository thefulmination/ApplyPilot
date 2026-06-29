# run-fleet-discovery.ps1 [-Label m2] [-ResultsPerSite 50] [-HoursOld 72]
#   Run ONE discovery worker: leases search_tasks from Postgres, scrapes JobSpy, and stages
#   postings to discovered_postings. PURE SCRAPE -- no login, no apply agent (no Codex/Claude),
#   no brain write. Best run on a residential machine (machine 2), NOT the home box, so scraping
#   stays off the IP that holds the LinkedIn apply session.
#
#   FULL LOOP:
#     1. SEED tasks (home box): the console "Expand searches" button, or
#        .\.conda-env\Scripts\applypilot-fleet-discovery-home.exe expand --config .applypilot\searches.yaml
#     2. SCRAPE (this script, on machine 2):  .\run-fleet-discovery.ps1
#     3. INGEST to the brain (home box):
#        .\.conda-env\Scripts\applypilot-fleet-discovery-home.exe pull
#
#   Without step 1 there are no tasks to lease and this just idles.
param([string]$Label = "m2", [int]$ResultsPerSite = 50, [int]$HoursOld = 72)
$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ProjectRoot

# .conda-env on the home box, .venv on a bootstrapped machine -- use whichever has the exe.
$exe = $null
foreach ($d in @(".\.conda-env\Scripts", ".\.venv\Scripts")) {
  $cand = Join-Path $d "applypilot-fleet-discovery.exe"
  if (Test-Path $cand) { $exe = (Resolve-Path $cand).Path; break }
}
if (-not $exe) { throw "applypilot-fleet-discovery not found in .conda-env or .venv -- run the setup script first." }

# DSN: inherit a persisted FLEET_PG_DSN (machine 2 sets it at User scope -> the home box), else
# default to the home box's local Postgres.
if (-not $env:FLEET_PG_DSN) { $env:FLEET_PG_DSN = "host=localhost port=5432 dbname=applypilot_fleet user=postgres connect_timeout=5" }
$env:APPLYPILOT_FLEET_DSN = $env:FLEET_PG_DSN
$env:PYTHONUTF8 = "1"; $env:PYTHONIOENCODING = "utf-8"

$wid = "$Label-disc"
Write-Host "[fleet-discovery] worker $wid  results/site=$ResultsPerSite  hours-old=$HoursOld  (pure scrape, no agent)"
& $exe --dsn $env:FLEET_PG_DSN --worker-id "$wid" --results-per-site $ResultsPerSite --hours-old $HoursOld
