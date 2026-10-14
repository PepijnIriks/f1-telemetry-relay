@echo off
REM Removes the scheduled task made by install-firewall-task.bat
schtasks /Delete /F /TN "F1 relay - Azure firewall"
pause
