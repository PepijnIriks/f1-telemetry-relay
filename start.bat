@echo off
REM Starts everything in one go: installs what is needed (first time only) and starts the relay + live page.
REM Double-click this file on this PC.
cd /d "%~dp0"
title F1 Telemetry Relay

where python >nul 2>nul
if errorlevel 1 (
  echo Python was not found. Install Python 3 from https://www.python.org/downloads/ and tick "Add to PATH".
  pause
  exit /b 1
)

if not exist .venv (
  echo First run: creating the environment...
  python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install --quiet --upgrade pip
  pip install --quiet -r requirements.txt
) else (
  call .venv\Scripts\activate.bat
)

REM Can the database login be read from Azure Key Vault? If not (first time on a new PC,
REM no permission or no secret yet), run the Key Vault setup first.
python src\secrets_store.py --check
if errorlevel 1 python src\setup_keyvault.py

REM Optional: "start.bat simulate" sends fake data that IS stored (for testing). For a demo use demo.bat.
if /i "%1"=="simulate" start "F1 simulation" cmd /k python src\simulate.py

REM The small "F1 driver" window (click a name to change driver). Turn off with DRIVER_MENU=0
findstr /r /c:"^DRIVER_MENU=0" config\config.env >nul 2>nul
if errorlevel 1 start "" pythonw src\driver_menu.py

python src\relay.py
pause
