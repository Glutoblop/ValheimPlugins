@echo off
rem Double-click to release everything: each plugin and the installer get their patch version
rem bumped, rebuilt, committed + pushed and published as GitHub releases.
rem "Release.bat --build-only" just builds the packages (no bump, no publish).
rem Other options are passed through, e.g.:  Release.bat -NoPlugins -Bump minor -Changes "Added X"
if /i "%~1"=="--build-only" (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" -BuildOnly
) else (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0release.ps1" %*
)
echo.
pause
