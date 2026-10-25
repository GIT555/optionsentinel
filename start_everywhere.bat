@echo off
title Option Sentinel - Live Multi-Device Server
cd /d "%~dp0"
echo ============================================================================
echo   Starting Option Sentinel Live Server (Accessible on All Devices)
echo ============================================================================
python option_sentinel.py --server --host 0.0.0.0 --port 8765
pause
