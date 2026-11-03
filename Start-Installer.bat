@echo off
rem Double-click me. Starts the Valheim Mod Installer without leaving a console window open.
rem (Installs Python first if it's missing - you'll be asked before anything is installed.)
start "" /min powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "%~dp0Start-Installer.ps1"
exit /b
