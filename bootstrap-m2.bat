@echo off
REM bootstrap-m2.bat -- double-click this in File Explorer on machine 2. No typing needed.
REM Pulls the latest code, then registers the m2 fleet tasks (FleetAgent with -AutoUpdate
REM + DiscoveryScrape). Safe to re-run any time; registration is idempotent.
cd /d "%~dp0"
echo === [1/2] git pull ===
git pull
echo.
echo === [2/2] register fleet tasks for m2 ===
REM -AllowZero: m2 is intentionally idle-armed (0 apply workers) when not actively
REM applying. DiscoveryScrape still runs on its own 6h schedule. Scale apply workers
REM up from the HOME box (fleet.ps1) when you want m2 applying.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register-fleet-tasks.ps1" -Machine m2 -AllowZero
echo.
echo Done. Review any errors above.
pause
