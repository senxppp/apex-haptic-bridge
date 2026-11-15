@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%~1"=="" (
  echo 用法: build.bat ^<目标exe名^>
  echo 例如: build.bat ForzaHorizon5.exe
  echo       名字要跟引擎里已有的游戏配置对得上
  exit /b 1
)
set CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe
if not exist "%CSC%" set CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe
if not exist "%CSC%" (
  echo [!!] 找不到 csc.exe（.NET Framework 4.x 自带，正常 Windows 都有）
  exit /b 1
)
"%CSC%" /nologo /target:exe /out:"%~1" playtone.cs
if errorlevel 1 (
  echo [!!] 编译失败
  exit /b 1
)
echo 已生成 %~1
