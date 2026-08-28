@echo off
setlocal EnableExtensions

cd /d "%~dp0"
title Offline File Namer WebUI

if exist "OfflineFileNamer.exe" (
    "OfflineFileNamer.exe"
    goto :finish
)

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" main.py
    if not errorlevel 1 goto :finish
    echo [WARN] Project virtual environment failed; trying another Python runtime.
)

where py >nul 2>&1
if %errorlevel%==0 (
    py -3 main.py
    if not errorlevel 1 goto :finish
    echo [WARN] Python launcher failed; trying python.exe.
)

where python >nul 2>&1
if %errorlevel%==0 (
    python main.py
    if not errorlevel 1 goto :finish
)

echo [ERROR] Python 3 was not found in PATH.
echo Install Python 3.10+ and make sure the Python launcher or python.exe is available.

:finish
if not %errorlevel%==0 (
    echo.
    echo [ERROR] WebUI stopped with exit code %errorlevel%.
)
echo.
pause
endlocal
