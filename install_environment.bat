@echo off
setlocal EnableExtensions

cd /d "%~dp0"
title Offline File Namer - Install Environment

set "PYTHON_CMD="
where py >nul 2>&1
if not errorlevel 1 set "PYTHON_CMD=py -3"

if not defined PYTHON_CMD (
    where python >nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=python"
)

if not defined PYTHON_CMD (
    echo [ERROR] Python 3 was not found in PATH.
    echo Install Python 3.10 or newer from python.org, then run this file again.
    echo The official Windows installer should include pip and Tcl/Tk.
    echo.
    pause
    exit /b 1
)

echo [1/3] Checking Python...
%PYTHON_CMD% --version
if errorlevel 1 goto :failed

if not exist ".venv\Scripts\python.exe" (
    echo [2/3] Creating local virtual environment...
    %PYTHON_CMD% -m venv .venv
    if errorlevel 1 goto :failed
) else (
    echo [2/3] Existing local virtual environment found.
)

echo [3/3] Installing Python dependencies...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto :failed

echo.
echo Environment installation completed.
echo Double-click start_webui.bat to launch the WebUI.
echo.
pause
exit /b 0

:failed
echo.
echo [ERROR] Environment setup failed with exit code %errorlevel%.
echo Check the Python installation and network/package source, then try again.
echo.
pause
exit /b 1
