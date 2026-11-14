@echo off
title GeoLens
cd /d "%~dp0"
echo Starting GeoLens (database + API + worker + web)
echo The browser will open automatically when the web server is ready.
echo Press Ctrl+C to stop everything.
echo.
"%~dp0runtime\py\python.exe" -u "%~dp0app\geolens_server.py" start --port 8000 --web-port 8081 --open-browser
echo.
echo GeoLens stopped. Press any key to close.
pause >nul
