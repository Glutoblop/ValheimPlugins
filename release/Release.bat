@echo off
rem Double-click to release the Valheim Mod Installer: bumps the patch version, commits + pushes,
rem and publishes a GitHub release. Plugins are released with their own release\Release.bat.
rem "Release.bat --build-only" just builds the zip (no bump, no publish).
rem Other options are passed through, e.g.:  Release.bat -Bump minor
if /i "%~1"=="--build-only" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" -BuildOnly
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" %*
)
echo.
pause
