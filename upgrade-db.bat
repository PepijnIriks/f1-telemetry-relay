@echo off
REM One time: stores the raw packets compressed (all data is kept, it only takes much less space).
REM Close the relay first. Can be stopped and started again: it continues where it was.
REM   upgrade-db.bat --drop-old   removes the old uncompressed copy after checking it once more
REM   upgrade-db.bat --cancel     stops an unfinished upgrade (removes only the half-made copy)
cd /d "%~dp0"
title F1 relay - database upgrade
if not exist .venv (
  echo Run start.bat once first, it creates the environment.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python src\db_upgrade.py %*
pause
