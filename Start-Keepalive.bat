@echo off
rem 停帧即时自愈 v2：k5_probe.log 1s 心跳 → 停帧约 5 秒内自动设备快速重连
rem 可选参数：--stall 3 --cooldown 60 --dry-run（只打日志不动设备）
cd /d "%~dp0"
if exist keepalive_stopped.flag del keepalive_stopped.flag
:loop
python -I auto_keepalive.py >> keepalive_console.log 2>&1
echo [%date% %time%] auto_keepalive 退出，5秒后重启 >> keepalive_console.log
timeout /t 5 /nobreak >nul
goto loop
