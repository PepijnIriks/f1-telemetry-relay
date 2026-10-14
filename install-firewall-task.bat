@echo off
REM Adds a Windows scheduled task that keeps this computer's IP in the Azure database firewall
REM (checks every 15 minutes while you are logged in). Remove it again with: remove-firewall-task.bat
cd /d "%~dp0"
if not exist .venv (
  echo Run start.bat once first, it creates the environment.
  pause
  exit /b 1
)
schtasks /Create /F /TN "F1 relay - Azure firewall" /SC MINUTE /MO 15 /TR "\"%~dp0.venv\Scripts\pythonw.exe\" \"%~dp0src\azure_firewall.py\" --quiet"
if errorlevel 1 (
  echo Creating the scheduled task failed.
  pause
  exit /b 1
)
echo.
echo Scheduled task created. Checking the firewall right now...
.venv\Scripts\python.exe src\azure_firewall.py
pause
