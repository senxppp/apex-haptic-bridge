@echo off
chcp 65001 >nul
title 停止全链路 / stop chain
cd /d "%~dp0"
echo ==========================================
echo   干净停止：引擎 / 服务 / 写进程 / 虚拟设备
echo ==========================================
python -I stop_chain.py
pause
