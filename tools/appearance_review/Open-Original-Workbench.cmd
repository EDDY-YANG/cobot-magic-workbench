@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto usepython
py -3 -I -B open_native.py
goto finish
:usepython
python -I -B open_native.py
:finish
if errorlevel 1 pause
