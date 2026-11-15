@echo off
chcp 65001 >nul
title 热插拔自动重载守护 / hotplug guard
cd /d "%~dp0"
echo ============================================
echo   监听真手柄拔出-插回，自动重建会话
echo   去抖 2s / 稳定 8s / 冷却 240s
echo   关闭本窗口即停止
echo ============================================
echo.
python -I hotplug_reload.py %*
echo.
echo 守护已退出。
pause
