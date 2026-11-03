@echo off
rem Double-click to build all plugins and publish any new versions as GitHub releases.
rem Pass --build-only (or run "Release.bat --build-only") to just build the packages.
set ARGS=
if /i "%~1"=="--build-only" set ARGS=-BuildOnly
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" %ARGS%
echo.
pause
