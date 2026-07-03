@echo off
REM bootstrap-m4.bat -- ONE double-click sets up m4 (GGGTOWER) end to end.
REM Self-elevates (UAC prompt), updates code, registers the fleet tasks, starts them.
REM No key file needed: the DeepSeek key is pulled from the fleet Postgres automatically.

REM --- self-elevate: relaunch as Administrator if not already ---
net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator rights...
  powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  exit /b
)

cd /d C:\ApplyPilot
if errorlevel 1 (
  echo ERROR: C:\ApplyPilot not found. Is the repo cloned there?
  pause & exit /b 1
)

echo === [1/3] updating code (git stash + pull) ===
git stash
git pull

echo.
echo === [2a/3] hydrating Gmail MCP creds (email-verification) from Postgres ===
for %%P in ("C:\ApplyPilot\.venv\Scripts\python.exe" "C:\ApplyPilot\.conda-env\python.exe") do if exist %%P %%P "C:\ApplyPilot\hydrate-gmail.py"

echo === [2/3] registering m4 fleet tasks (FleetAgent + ComputeScore) ===
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\ApplyPilot\register-fleet-tasks.ps1" -Machine m4

echo.
echo === [3/3] starting the tasks now ===
schtasks /run /tn "ApplyPilotFleet-ComputeScore" 2>nul
schtasks /run /tn "ApplyPilotFleet-FleetAgent" 2>nul

echo.
echo === done. check status: ===
powershell -NoProfile -ExecutionPolicy Bypass -File "C:\ApplyPilot\status-m4.ps1"
echo.
echo (Compute scores immediately; apply workers stay idle until the fleet is unpaused.)
pause
