@echo off
REM Keeps this folder and GitHub (github.com/PepijnIriks/f1-telemetry-relay) in sync:
REM   1. gets the newest version from GitHub (also changes merged in a pull request)
REM   2. if you changed something here: shows what, and puts it on GitHub with a short description
REM The first time it links this folder to GitHub (your files are not changed by that).
cd /d "%~dp0"
title F1 relay - GitHub sync
set REPO=https://github.com/PepijnIriks/f1-telemetry-relay.git

where git >nul 2>nul
if errorlevel 1 goto nogit

REM ---- who you are (shown with every change on GitHub), asked only once per computer
for /f "delims=" %%a in ('git config --global user.name') do set GITNAME=%%a
if not defined GITNAME (
  set /p GITNAME=Your name for GitHub changes:
  set /p GITMAIL=Your e-mail address on GitHub:
)
if not defined GITMAIL for /f "delims=" %%a in ('git config --global user.email') do set GITMAIL=%%a
git config --global user.name "%GITNAME%"
if defined GITMAIL git config --global user.email "%GITMAIL%"

REM ---- first time: link this folder to GitHub without touching your files
if exist .git goto linked
echo Linking this folder to GitHub (once)...
if exist .git-setup rmdir /s /q .git-setup
git config --global credential.helper manager
git clone --quiet --no-checkout %REPO% .git-setup
if errorlevel 1 goto nonet
move .git-setup\.git .git >nul
rmdir /s /q .git-setup
git reset --quiet
echo Linked. Your files were not changed.
:linked

echo.
echo Getting the newest version information from GitHub...
git fetch --quiet origin
if errorlevel 1 goto nonet

REM ---- your own changes on this PC
git status --short > "%TEMP%\f1sync.txt"
for %%s in ("%TEMP%\f1sync.txt") do if %%~zs==0 goto pull
echo.
echo Changed on this PC (M = changed, ?? = new file, D = deleted):
type "%TEMP%\f1sync.txt"
echo.
set SAVE=
set /p SAVE=Put these changes on GitHub? (y/n):
if /i not "%SAVE%"=="y" goto pull_with_changes
set MSG=
set /p MSG=Describe in a few words what you changed:
if not defined MSG set MSG=Changes from %COMPUTERNAME%
git add -A
git commit --quiet -m "%MSG%"

:pull
echo.
echo Getting the newest version from GitHub...
git pull --quiet --rebase origin main
if errorlevel 1 goto conflict
git push --quiet origin HEAD:main
if errorlevel 1 goto nopush
goto done

:pull_with_changes
echo.
echo Your changes stay only on this PC. Getting the newest version from GitHub around them...
git stash --quiet
git pull --quiet --rebase origin main
if errorlevel 1 goto conflict_stash
git stash pop --quiet
if errorlevel 1 goto conflict_pop
goto done

:done
echo.
echo In sync with GitHub. Latest changes:
git log --oneline -5
echo.
pause
exit /b 0

:nogit
echo Git is not installed. Install it once (PowerShell):  winget install -e --id Git.Git
echo Then close this window and double-click github-sync.bat again.
pause
exit /b 1

:nonet
echo GitHub could not be reached, or GitHub did not accept the login.
echo If it says "Invalid username or token": an old GitHub login is saved on this PC. Remove it with
echo   cmdkey /delete:git:https://github.com
echo and run this again: a browser window then opens to log in to GitHub.
pause
exit /b 1

:nopush
echo.
echo Putting your changes on GitHub did not work (not logged in, or no permission for this repository).
echo A GitHub login window should appear the first time; try again. Your changes are saved on this PC.
pause
exit /b 1

:conflict
git rebase --abort >nul 2>nul
echo.
echo Your changes and the changes on GitHub touch the same lines. Nothing was lost: your changes are still
echo here and nothing was sent. Ask for help, or open the folder in VS Code to choose which lines to keep.
pause
exit /b 1

:conflict_stash
git rebase --abort >nul 2>nul
git stash pop --quiet
goto conflict

:conflict_pop
echo.
echo The newest version from GitHub is here, but some of your own changes touch the same lines.
echo They are kept safely (git stash list). Ask for help to combine them.
pause
exit /b 1
