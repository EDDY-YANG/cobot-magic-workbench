@echo off
powershell.exe -NoProfile -File "%~dp0Start-Workstation.ps1"
if errorlevel 1 pause
