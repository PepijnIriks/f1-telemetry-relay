@echo off
REM Shows which tables are in the Azure database and how much data they hold.
cd /d "%~dp0"
if not exist .venv (
  echo Run start.bat once first, it creates the environment.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python src\inspect_db.py
pause
