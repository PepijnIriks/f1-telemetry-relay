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

REM First time on a new PC: no Key Vault set yet -> run the Key Vault setup first
findstr /r /c:"^KEYVAULT_NAME=." config\config.env >nul 2>nul
if errorlevel 1 python src\setup_keyvault.py

REM Optional: "start.bat simulate" sends fake data so you can show everything works without the game
if /i "%1"=="simulate" start "F1 simulation" cmd /k python src\simulate.py
if /i "%1"=="simulatie" start "F1 simulation" cmd /k python src\simulate.py

python src\relay.py
pause
