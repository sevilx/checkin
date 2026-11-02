@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

rem ============================================================
rem  Double-click to push Firefox cookies to the NAS panel.
rem  Change the NAS address below if it moves.
rem  Keep this file ASCII-only: cmd.exe misparses UTF-8 batch files.
rem ============================================================
if not defined NAS set "NAS=http://192.168.99.130:8181"

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [!] Python not found in PATH.
    echo       Install Python 3.9+ and tick "Add python.exe to PATH".
    echo.
    pause
    exit /b 1
)

python tools\sync_cookies.py --nas %NAS% %*
set CODE=%ERRORLEVEL%

echo.
if not "%CODE%"=="0" (
    echo   Failed with exit code %CODE%. See the messages above.
) else (
    echo   Done. Close this window.
)
pause
