@echo off
REM Exports every table of the Azure database to CSV (folder exports\<date_time>).
REM Starts the database first if it is off, and turns it off again afterwards.
cd /d "%~dp0"
title F1 relay - export database
if not exist .venv (
  echo Run start.bat once first, it creates the environment.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python export_csv.py %*
pause
