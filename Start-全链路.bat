@echo off
chcp 65001 >nul
title 全链路启动 / start chain
cd /d "%~dp0"

if not exist app.ini (
  echo [!!] 缺少 app.ini
  echo      先复制 app.ini.example 为 app.ini 并填入 usbip / engine 路径
  pause
  exit /b 1
)

echo ============================================
echo   1. 服务端 + 虚拟设备 + 引擎（约 2 分钟）
echo   2. 停帧自愈（约 5 秒内自动快速重连）
echo   3. 扳机 / 马达输出
echo   4. 热插拔自动重载守护
echo ============================================
python -I reload_chain.py
if errorlevel 2 (
  echo [!!] 链路启动失败，见上方输出
  pause
  exit /b 1
)
start "keepalive" /min python -I auto_keepalive.py
echo 链路已就绪，放音源即可。
pause
