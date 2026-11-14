@echo off
title GeoLens Web (LAN)
cd /d "%~dp0"
set "LANIP="
for /f %%i in ('powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue).Where({$_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notlike '169.254*'})[0].IPAddress"') do set "LANIP=%%i"
if "%LANIP%"=="" set "LANIP=127.0.0.1"
echo Starting GeoLens web UI (LAN mode)
echo Local: http://127.0.0.1:8081
echo LAN:   http://%LANIP%:8081
echo Make sure the backend is already running (start-backend-lan.bat).
echo Press Ctrl+C to stop.
echo.
start "" "http://127.0.0.1:8081"
"%~dp0runtime\py\python.exe" -u "%~dp0app\geolens_web.py" --host 0.0.0.0 --port 8081 --api http://127.0.0.1:8000
echo.
echo Web server stopped. Press any key to close.
pause >nul
