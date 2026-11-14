@echo off
title GeoLens Backend
cd /d "%~dp0"
echo Starting GeoLens backend: database + API + worker + tiles
echo Frontend: run start-web.bat, then open http://127.0.0.1:8081
echo Press Ctrl+C to stop.
echo.
"%~dp0runtime\py\python.exe" -u "%~dp0app\geolens_server.py" start --port 8000
echo.
echo Backend stopped. Press any key to close.
pause >nul
