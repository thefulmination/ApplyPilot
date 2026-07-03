@echo off
REM bootstrap-m2.bat -- double-click this in File Explorer on machine 2. No typing needed.
REM Pulls the latest code, then registers the m2 fleet tasks (FleetAgent with -AutoUpdate
REM + DiscoveryScrape). Safe to re-run any time; registration is idempotent.
cd /d "%~dp0"
echo === [1/2] git pull ===
git pull
echo.
echo === [2/2] register fleet tasks for m2 ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0register-fleet-tasks.ps1" -Machine m2
echo.
echo Done. Review any errors above.
pause
