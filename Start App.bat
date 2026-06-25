@echo off
cd /d "%~dp0"
echo Starting your app...
echo A browser window should open in a few seconds.
echo.
echo To stop the app, close this window or press Ctrl+C.
echo.
"%~dp0venv\Scripts\python.exe" -m streamlit run "%~dp0app.py"
pause
