@echo off
title Odys Desktop Bridge

:: Generate bridge token if not set
if "%ODY_BRIDGE_TOKEN%"=="" (
    echo ! WARNING: ODY_BRIDGE_TOKEN not set. Generating random token...
    for /f "tokens=*" %%i in ('powershell -Command "[System.Convert]::ToBase64String((1..32|%%{[byte](Get-Random -Min 0 -Max 256)}))"') do set "ODY_BRIDGE_TOKEN=%%i"
    echo Generated token: %ODY_BRIDGE_TOKEN%
    echo SAVE THIS TOKEN! You need it in Odys container config.
)

:: Change to bridge directory
cd /d "%~dp0"

:: Install deps if needed
if not exist "venv\Scripts\python.exe" (
    echo Creating venv and installing dependencies...
    python -m venv venv
    call venv\Scripts\pip install -r requirements.txt
)

echo Starting Odys Desktop Bridge...
echo Listening on http://127.0.0.1:%ODY_BRIDGE_PORT% (default 8765)
echo Token: %ODY_BRIDGE_TOKEN%
echo.

call venv\Scripts\python desktop_bridge.py
pause
