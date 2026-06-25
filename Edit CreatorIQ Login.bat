@echo off
cd /d "%~dp0"
if not exist ".env" copy ".env.example" ".env" >nul
notepad ".env"
echo.
echo Save Notepad, then tell Cursor: ".env is ready - run login and sync"
pause
