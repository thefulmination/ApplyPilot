# swap-recovered-brain.ps1
# RUN THIS IN YOUR OWN POWERSHELL (real environment) -- not through Claude.
# Replaces the corrupt live brain with the verified-clean recovered copy.
# The corrupt original is snapshotted first (never deleted). Safe to re-run.

$ErrorActionPreference = "Stop"
$brain  = "C:\Users\JStal\AppData\Local\ApplyPilot\applypilot.db"
$final  = "C:\Users\JStal\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Local\ApplyPilot\recovery-20260626\applypilot.recovered-final.db"
$sqlite = "C:\Users\JStal\OneDrive\Documents\New project\ApplyPilot\.conda-env\Library\bin\sqlite3.exe"

Write-Host "== 1. Safety checks =="
$busy = Get-CimInstance Win32_Process -Filter "Name='applypilot.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'applypilot' }
if ($busy) { throw "An applypilot process is running (PID $($busy.ProcessId)). Stop it first, then re-run." }
if (-not (Test-Path $final)) { throw "Recovered DB not found at:`n  $final" }
$chk = (& $sqlite $final "PRAGMA integrity_check(2);" | Out-String).Trim()
if ($chk -ne "ok") { throw "Recovered DB failed integrity_check ('$chk'). Aborting." }
$rjobs = (& $sqlite $final "SELECT COUNT(*) FROM jobs;").Trim()
$rappl = (& $sqlite $final "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL;").Trim()
Write-Host "   recovered DB OK: $rjobs jobs, $rappl applied"

Write-Host "== 2. Snapshot the corrupt brain (kept as insurance) =="
$stamp = Get-Date -Format "yyyyMMdd-HHmmss"
$snap  = "C:\Users\JStal\AppData\Local\ApplyPilot\applypilot.corrupt-$stamp.db"
if (Test-Path $brain) { Copy-Item $brain $snap -Force; Write-Host "   saved $snap" }
else { Write-Host "   (no live brain present to snapshot)" }

Write-Host "== 3. Swap in the recovered brain =="
Remove-Item "$brain-wal","$brain-shm" -Force -ErrorAction SilentlyContinue   # clear stale corrupt WAL
Copy-Item $final $brain -Force
Write-Host "   installed recovered brain -> $brain"

Write-Host "== 4. Verify the live brain =="
$chk2  = (& $sqlite $brain "PRAGMA integrity_check(3);" | Out-String).Trim()
$jobs2 = (& $sqlite $brain "SELECT COUNT(*) FROM jobs;").Trim()
$appl2 = (& $sqlite $brain "SELECT COUNT(*) FROM jobs WHERE applied_at IS NOT NULL;").Trim()
Write-Host "   integrity: $chk2 | jobs: $jobs2 | applied: $appl2"
if ($chk2 -eq "ok") {
    Write-Host "`n[OK] SWAP COMPLETE - the brain is clean ($jobs2 jobs, $appl2 applied)."
    Write-Host "Corrupt original kept at: $snap"
    Write-Host "Delete it once you're confident, and you can delete this script too."
} else {
    Write-Host "`n[WARN] integrity not 'ok'. Restore the original with:"
    Write-Host "   Copy-Item '$snap' '$brain' -Force"
}
