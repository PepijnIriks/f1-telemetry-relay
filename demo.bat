@echo off
REM Demo without the game: starts the relay in DEMO MODE plus a fake game (simulator).
REM Everything works on the website, but NOTHING is written to the database.
cd /d "%~dp0"
title F1 Telemetry Relay - DEMO
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
REM The fake game starts in its own window a few seconds after the relay (close that window to stop it)
start "F1 demo - fake game" /min cmd /c "timeout /t 4 /nobreak >nul & python src\simulate.py"
start "" pythonw src\driver_menu.py
python src\relay.py --demo --driver Demo
pause
