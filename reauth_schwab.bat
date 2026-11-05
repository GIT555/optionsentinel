@echo off
title Schwab Automated Token Renewer
cd /d "%~dp0"
echo =======================================================
echo    SCHWAB AUTOMATED MARKET DATA TOKEN RENEWAL
echo =======================================================
python schwab_headless_auth.py
pause
