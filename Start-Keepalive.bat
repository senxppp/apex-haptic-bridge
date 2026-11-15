@echo off
chcp 65001 >nul
title 停帧自愈守护 / keepalive
cd /d "%~dp0"
rem 可选参数：--stall 3 --cooldown 60 --dry-run（只打日志不动设备）
:loop
python -I auto_keepalive.py %* >> keepalive_console.log 2>&1
echo [%date% %time%] auto_keepalive 退出，5 秒后重启 >> keepalive_console.log
timeout /t 5 /nobreak >nul
goto loop
