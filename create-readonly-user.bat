@echo off
REM Makes (or renews) a read-only database account for the other group. Its login goes to Azure Key Vault.
cd /d "%~dp0"
title F1 relay - read-only account
if not exist .venv (
  echo Run start.bat once first, it creates the environment.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python src\create_readonly.py
pause
