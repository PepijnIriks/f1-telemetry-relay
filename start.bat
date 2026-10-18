@echo off
REM F1 telemetry relay - double-click to start.
REM The first time on a computer it installs what is needed (Python, Azure CLI, the Python packages)
REM and sets up the database login. After that it just starts the relay, the website and the driver window.
REM   start.bat            normal: game data is shown live and stored in Azure
REM   start.bat demo       demo with a fake game, NOTHING is stored (demo.bat does this)
REM   start.bat simulate   fake game whose data IS stored (for testing)
setlocal
cd /d "%~dp0"
set MODE=%~1
title F1 Telemetry Relay %MODE%
set PYTHONPYCACHEPREFIX=%~dp0data\pycache

REM ---- 1. Python 3.10 or newer (the Microsoft Store placeholder "python" does not count)
python -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if not errorlevel 1 goto have_python
echo Python 3 is not installed on this computer (or it is too old).
set ANSWER=
set /p ANSWER=Install Python now? (y/n):
if /i not "%ANSWER%"=="y" goto no_python
winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
echo.
echo Python is installed. Close this window and double-click start.bat again.
pause
exit /b 0
:have_python

REM ---- 2. Azure CLI (database login from Key Vault, database on/off, firewall). Not needed for a demo.
if /i "%MODE%"=="demo" goto environment
where az >nul 2>nul
if not errorlevel 1 goto environment
echo The Azure CLI is not installed. It is needed to store data in the Azure database.
set ANSWER=
set /p ANSWER=Install the Azure CLI now? (y/n):
if /i not "%ANSWER%"=="y" goto environment
winget install -e --id Microsoft.AzureCLI --accept-package-agreements --accept-source-agreements
echo.
echo The Azure CLI is installed. Close this window and double-click start.bat again;
echo a browser then opens once to log in to Azure (use your Fontys account).
pause
exit /b 0

REM ---- 3. The Python environment (.venv). Made again when it is missing or was copied from another computer.
:environment
if not exist .venv\Scripts\python.exe goto make_venv
.venv\Scripts\python.exe -c "import sys" >nul 2>nul
if errorlevel 1 goto make_venv
goto packages
:make_venv
if exist .venv rmdir /s /q .venv
echo Creating the Python environment (first time on this computer, takes a minute)...
python -m venv .venv
if errorlevel 1 goto venv_failed
.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check --upgrade pip
:packages
.venv\Scripts\python.exe -m pip install --quiet --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto packages_failed
call .venv\Scripts\activate.bat

REM ---- 4. Files of older versions (spool, exports, zips, logs) move into the folder data
python src\paths.py

REM ---- 5. Demo: fake game + relay that stores nothing
if /i not "%MODE%"=="demo" goto login
start "F1 demo - fake game" /min cmd /c "timeout /t 4 /nobreak >nul & python src\simulate.py"
start "" pythonw src\driver_menu.py
python src\relay.py --demo --driver Demo
pause
exit /b 0

REM ---- 6. Database login from Azure Key Vault (the first time on a computer: set it up)
:login
python src\secrets_store.py --check
if errorlevel 1 python src\setup_keyvault.py

if /i "%MODE%"=="simulate" start "F1 simulation" cmd /k python src\simulate.py

REM The small "F1 driver" window (click a name to change driver). Turn off with DRIVER_MENU=0 in config\config.env
findstr /r /c:"^DRIVER_MENU=0" config\config.env >nul 2>nul
if errorlevel 1 start "" pythonw src\driver_menu.py

python src\relay.py
pause
exit /b 0

:no_python
echo Install Python 3 yourself from https://www.python.org/downloads/ (tick "Add python.exe to PATH"),
echo then double-click start.bat again.
pause
exit /b 1

:venv_failed
echo Creating the Python environment failed. Re-install Python (tick "Add python.exe to PATH") and try again.
pause
exit /b 1

:packages_failed
echo Installing the Python packages failed. Check the internet connection and double-click start.bat again.
pause
exit /b 1
