@echo off
title GeoLens Web
cd /d "%~dp0"
echo Starting GeoLens web UI: http://127.0.0.1:8081
echo Make sure the backend is already running (start-backend.bat).
echo Press Ctrl+C to stop.
echo.
start "" "http://127.0.0.1:8081"
"%~dp0runtime\py\python.exe" -u "%~dp0app\geolens_web.py" --host 127.0.0.1 --port 8081 --api http://127.0.0.1:8000
echo.
echo Web server stopped. Press any key to close.
pause >nul
