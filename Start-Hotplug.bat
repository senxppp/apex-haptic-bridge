@echo off
chcp 65001 >nul
title 八爪鱼 k5 手柄热插拔自动重载守护
cd /d "%~dp0"
echo ============================================
echo   手柄热插拔自动重载守护
echo   监听真手柄 VID_37D7^&PID_2501 的拔出-插回
echo   自动执行 reload_chain.py 重建会话
echo   关闭本窗口即停止守护
echo ============================================
echo.
python -I hotplug_reload.py %*
echo.
echo 守护已退出。
pause
