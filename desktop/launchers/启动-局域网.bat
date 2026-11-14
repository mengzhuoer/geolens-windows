@echo off
title GeoLens (LAN)
cd /d "%~dp0"
set "LANIP="
for /f %%i in ('powershell -NoProfile -Command "(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue).Where({$_.InterfaceAlias -notmatch 'Loopback' -and $_.IPAddress -notlike '169.254*'})[0].IPAddress"') do set "LANIP=%%i"
if "%LANIP%"=="" set "LANIP=127.0.0.1"
echo Starting GeoLens (database + API + worker + web, LAN mode)
echo Local: http://127.0.0.1:8081
echo LAN:   http://%LANIP%:8081
echo The browser will open automatically when the web server is ready.
echo Press Ctrl+C to stop everything.
echo.
"%~dp0runtime\py\python.exe" -u "%~dp0app\geolens_server.py" start --port 8000 --web-port 8081 --web-host 0.0.0.0 --public-host %LANIP% --open-browser
echo.
echo GeoLens stopped. Press any key to close.
pause >nul
