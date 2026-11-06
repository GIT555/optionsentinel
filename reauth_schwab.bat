@echo off
setlocal enabledelayedexpansion
title Schwab Token Renewer
cd /d "%~dp0"

echo =======================================================
echo    SCHWAB MARKET DATA TOKEN RENEWAL (7-DAY ACCESS)
echo =======================================================
echo.

if not "%~1"=="" (
    echo [*] Exchanging provided URL / code argument...
    python option_sentinel.py --schwab-auth "%~1"
    goto :done
)

echo [Option 1] Paste redirect URL (starts with https://127.0.0.1/?code=...)
echo [Option 2] Or press ENTER to automatically launch Chrome and capture it
echo.
set /p "USER_URL=Paste Schwab URL (or press Enter for Chrome Auto-Auth): "

if "!USER_URL!"=="" (
    echo.
    echo [*] Launching Google Chrome for 1-click authorization...
    python option_sentinel.py --auto-auth
) else (
    echo.
    echo [*] Exchanging pasted redirect URL with Schwab OAuth...
    python option_sentinel.py --schwab-auth "!USER_URL!"
)

:done
echo.
pause
