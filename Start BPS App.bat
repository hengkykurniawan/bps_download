@echo off
REM Double-click to launch the BPS Data Downloader web app.
REM Keep this window open while using the app. Close it (or press Ctrl+C) to stop.
cd /d "%~dp0"
echo Starting BPS Data Downloader...
echo Your browser will open at http://127.0.0.1:8765
echo Keep this window open. Close it to stop the app.
echo.
python bps_app.py
echo.
echo App stopped. Press any key to close this window.
pause >nul
