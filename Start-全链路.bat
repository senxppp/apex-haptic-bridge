@echo off
chcp 65001 >nul
title 八爪鱼 k5 全链路启动器
cd /d "%~dp0"
echo ============================================
echo   八爪鱼 k5 全链路启动（约 2 分钟）
echo   1. 服务端 + 虚拟设备 + 官方引擎
echo   2. 停帧即时自愈（停帧约5秒自动快速重连）
echo   3. 马达控制台 GUI
echo   4. 手柄热插拔自动重载守护（P4）
echo   完成后放音乐即震
echo ============================================
python -I reload_chain.py
start "自动维持器" /min python -I auto_keepalive.py
rem 守护优先用 Python 3.12（hidapi 可用、探测零子进程）；换机器无此路径时退回 PATH python
rem 如需临时禁用：set NO_HOTPLUG=1 后再运行本脚本
if not "%NO_HOTPLUG%"=="1" (
    set "PY312=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if exist "%PY312%" (
        start "热插拔重载守护" /min "%PY312%" -I hotplug_reload.py
    ) else (
        start "热插拔重载守护" /min python -I hotplug_reload.py
    )
)
echo.
echo 全链路就绪。放音乐即可。手柄关机再开机会自动重载（约 2.5 分钟）。
echo 此窗口可最小化（服务端日志在这里）。
pause
