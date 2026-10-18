@echo off
REM Runs the automated tests (takes a few seconds, needs no game and no Azure).
REM Database tests only run when TEST_DATABASE_URL points to a local, empty test database.
cd /d "%~dp0"
title F1 relay - tests
if not exist .venv (
  echo Run start.bat once first, it creates the environment.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python -m pytest --version >nul 2>nul
if errorlevel 1 pip install --quiet -r requirements-dev.txt
python -m pytest -q tests
pause
