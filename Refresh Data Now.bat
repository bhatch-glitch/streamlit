@echo off
cd /d "%~dp0"
echo Refreshing dashboard from latest CSV exports (no browser)...
venv\Scripts\python.exe scripts\refresh_data.py --fast
if %ERRORLEVEL% EQU 0 (
    echo Done. Open the Streamlit dashboard to see updated numbers.
) else (
    echo Refresh had issues. Export payouts CSV from CreatorIQ to data\incoming\ and try again.
)
pause
