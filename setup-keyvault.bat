@echo off
REM One time: puts the database login in Azure Key Vault (no passwords in files).
cd /d "%~dp0"
title F1 relay - Key Vault setup
if not exist .venv (
  echo Run start.bat once first, it creates the environment.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python src\setup_keyvault.py
pause
