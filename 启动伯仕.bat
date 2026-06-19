@echo off
chcp 65001 >nul
cd /d "%~dp0"

python3 "%USERPROFILE%\.boshi\health_check.py" > "%TEMP%\boshi_health.txt"
set "HEALTH_EXIT=%errorlevel%"

if "%HEALTH_EXIT%" neq "0" (
    type "%TEMP%\boshi_health.txt"
    echo.
    msg * "伯仕自检发现问题！请查看上面的红色标记" >nul 2>&1
    pause
    exit /b 1
)

del "%TEMP%\boshi_health.txt" 2>nul
"%USERPROFILE%\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe" -s boshi-memory
