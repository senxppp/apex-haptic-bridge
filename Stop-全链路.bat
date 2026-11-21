@echo off
chcp 65001 >nul
title 停止八爪鱼 k5 全链路
cd /d "%~dp0"
echo ==========================================
echo   一键干净停止：引擎 / 服务 / 写进程 / 虚拟设备
echo ==========================================
python -I stop_chain.py
echo.
pause
