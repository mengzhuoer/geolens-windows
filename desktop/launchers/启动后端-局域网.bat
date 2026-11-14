@echo off
title GeoLens Backend (LAN)
cd /d "%~dp0"
set "LANIP="
for /f %%i in ('powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue).Where({$_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notlike '169.254*'})[0].IPAddress"') do set "LANIP=%%i"
if "%LANIP%"=="" set "LANIP=127.0.0.1"
echo Starting GeoLens backend (LAN mode)
echo LAN address: http://%LANIP%:8081
echo Run start-web-lan.bat in another window.
echo Press Ctrl+C to stop.
echo.
"%~dp0runtime\py\python.exe" -u "%~dp0app\geolens_server.py" start --port 8000 --public-host %LANIP%
echo.
echo Backend stopped. Press any key to close.
pause >nul
