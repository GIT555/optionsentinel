@echo off
title Schwab Automated Token Renewer
cd /d "%~dp0"
echo =======================================================
echo    SCHWAB AUTOMATED MARKET DATA TOKEN RENEWAL
echo =======================================================
python -m option_trading.trading.schwab_auth
pause
