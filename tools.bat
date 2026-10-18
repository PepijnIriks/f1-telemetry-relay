@echo off
REM F1 telemetry relay - tools: everything besides starting the relay, in one menu.
setlocal
if "%~1"==":github" goto gs_copy
cd /d "%~dp0"
title F1 relay - tools
set PYTHONPYCACHEPREFIX=%~dp0data\pycache
set PY=.venv\Scripts\python.exe

:menu
cls
echo.
echo   F1 telemetry relay - tools
echo   ==========================
echo.
echo   Database
echo     1  What is in the database (tables, rows, size)
echo     2  Export the whole database to CSV (folder data\exports)
echo     3  Compress the database (once; all data is kept)
echo     4  Remove the old uncompressed copy (after 3, checks again first)
echo     5  Cancel an unfinished compression (never touches your data)
echo.
echo   Access
echo     6  Key Vault setup (database login)
echo     7  Read-only account for the dashboard group
echo     8  Firewall check every 15 minutes: turn on
echo     9  Firewall check every 15 minutes: turn off
echo.
echo   Code
echo    10  Run the tests
echo    11  GitHub: get the newest version / put my changes on GitHub
echo.
echo     0  Close
echo.
set CHOICE=
set /p CHOICE=Choice:
if "%CHOICE%"=="0" exit /b 0
if "%CHOICE%"=="11" goto github
if not exist %PY% goto no_venv
if "%CHOICE%"=="1" call :run src\inspect_db.py
if "%CHOICE%"=="2" call :run src\export_csv.py
if "%CHOICE%"=="3" call :run src\db_upgrade.py
if "%CHOICE%"=="4" call :run src\db_upgrade.py --drop-old
if "%CHOICE%"=="5" call :run src\db_upgrade.py --cancel
if "%CHOICE%"=="6" call :run src\setup_keyvault.py
if "%CHOICE%"=="7" call :run src\create_readonly.py
if "%CHOICE%"=="8" goto firewall_on
if "%CHOICE%"=="9" goto firewall_off
if "%CHOICE%"=="10" goto tests
goto menu

:run
echo.
%PY% %*
echo.
pause
goto :eof

:no_venv
echo.
echo Double-click start.bat once first: it installs everything that is needed.
pause
goto menu

REM ---------------------------------------------------------------- firewall task
:firewall_on
schtasks /Create /F /TN "F1 relay - Azure firewall" /SC MINUTE /MO 15 /TR "\"%~dp0.venv\Scripts\pythonw.exe\" \"%~dp0src\azure_firewall.py\" --quiet"
if errorlevel 1 echo Creating the scheduled task failed.
echo Checking the firewall right now...
%PY% src\azure_firewall.py
pause
goto menu

:firewall_off
schtasks /Delete /F /TN "F1 relay - Azure firewall"
pause
goto menu

REM ---------------------------------------------------------------- tests
:tests
%PY% -m pytest --version >nul 2>nul
if errorlevel 1 %PY% -m pip install --quiet --disable-pip-version-check -r tests\requirements.txt
%PY% -m pytest -q -p no:cacheprovider tests
pause
goto menu

REM ---------------------------------------------------------------- GitHub
REM Gets the newest version from GitHub (also changes merged in a pull request). If something was changed
REM on this computer it shows what and, after a short description, puts it on GitHub. The first time it
REM links this folder to GitHub (git init + fetch): no file in the folder is changed by that.
REM It runs from a copy in %TEMP%, because the update can change tools.bat itself while it runs.
:github
copy /y "%~f0" "%TEMP%\f1-relay-sync.bat" >nul
"%TEMP%\f1-relay-sync.bat" :github "%~dp0"
:gs_copy
cd /d "%~2"
title F1 relay - GitHub
set REPO=https://github.com/PepijnIriks/f1-telemetry-relay.git
where git >nul 2>nul
if errorlevel 1 goto gs_nogit

for /f "delims=" %%a in ('git config --global user.name') do set GITNAME=%%a
if defined GITNAME goto gs_named
set /p GITNAME=Your name for GitHub changes:
set /p GITMAIL=Your e-mail address on GitHub:
git config --global user.name "%GITNAME%"
if defined GITMAIL git config --global user.email "%GITMAIL%"
:gs_named

if not exist .git goto gs_setup
git rev-parse --verify --quiet HEAD >nul 2>nul
if errorlevel 1 goto gs_fetch
goto gs_linked
:gs_setup
echo Linking this folder to GitHub (once)...
git config --global credential.helper manager
git init --quiet -b main
git remote add origin %REPO%
:gs_fetch
git fetch --quiet origin
if errorlevel 1 goto gs_nonet
git reset --quiet origin/main
if errorlevel 1 goto gs_nonet
git branch --quiet --set-upstream-to=origin/main main
echo Linked. Your files were not changed.
:gs_linked

echo.
echo Getting the newest version information from GitHub...
git fetch --quiet origin
if errorlevel 1 goto gs_nonet
git status --short > "%TEMP%\f1sync.txt"
for %%s in ("%TEMP%\f1sync.txt") do if %%~zs==0 goto gs_pull
echo.
echo Changed on this computer (M = changed, ?? = new file, D = deleted):
type "%TEMP%\f1sync.txt"
echo.
set SAVE=
set /p SAVE=Put these changes on GitHub? (y/n):
if /i not "%SAVE%"=="y" goto gs_keep_local
set MSG=
set /p MSG=Describe in a few words what you changed:
if not defined MSG goto gs_no_message
git add -A
git commit --quiet -m "%MSG%"

:gs_pull
echo.
echo Getting the newest version from GitHub...
git pull --quiet --rebase origin main
if errorlevel 1 goto gs_conflict
git push --quiet origin HEAD:main
if errorlevel 1 goto gs_nopush
goto gs_done

:gs_keep_local
echo.
echo Your changes stay only on this computer. Getting the newest version from GitHub around them...
git stash --quiet
git pull --quiet --rebase origin main
if errorlevel 1 goto gs_conflict_stash
git stash pop --quiet
if errorlevel 1 goto gs_conflict_pop
goto gs_done

:gs_done
echo.
echo In sync with GitHub. Latest changes:
git log --oneline -5
echo.
pause
goto gs_end

:gs_no_message
echo Nothing was put on GitHub: a short description is needed. Try again.
pause
goto gs_end

:gs_nogit
echo Git is not installed. Install it once (PowerShell on this computer):  winget install -e --id Git.Git
echo Then close this window and open tools.bat again.
pause
goto gs_end

:gs_nonet
echo GitHub could not be reached, or GitHub did not accept the login.
echo If it says "Invalid username or token": an old GitHub login is saved on this computer. Remove it with
echo   cmdkey /delete:git:https://github.com
echo and try again: a browser window then opens to log in to GitHub.
pause
goto gs_end

:gs_nopush
echo.
echo Putting your changes on GitHub did not work (not logged in, or no permission for this repository).
echo Your changes are saved on this computer; try again later.
pause
goto gs_end

:gs_conflict
git rebase --abort >nul 2>nul
echo.
echo Your changes and the changes on GitHub touch the same lines. Nothing was lost and nothing was sent.
echo Ask for help, or open the folder in VS Code to choose which lines to keep.
pause
goto gs_end

:gs_conflict_stash
git rebase --abort >nul 2>nul
git stash pop --quiet
goto gs_conflict

:gs_conflict_pop
echo.
echo The newest version from GitHub is here, but some of your own changes touch the same lines.
echo They are kept safely (git stash list). Ask for help to combine them.
pause
goto gs_end

:gs_end
exit /b 0
