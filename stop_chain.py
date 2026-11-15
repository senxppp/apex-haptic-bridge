#!/usr/bin/env python3
"""一键干净停止八爪鱼 k5 全链路（引擎 / 服务 / 写进程 / 虚拟设备）。

只按「命令行特征」杀本项目进程，不碰其他程序；不递归删除任何文件。
用法：python -I stop_chain.py  或直接双击 Stop-全链路.bat
"""
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _ini_paths():
    """与 app.py / reload_chain.py 共用 app.ini，不另存一份路径。"""
    import configparser
    d = {"usbip": "", "python": ""}
    try:
        cp = configparser.ConfigParser()
        cp.read(os.path.join(BASE, "app.ini"), encoding="utf-8")
        if cp.has_section("paths"):
            for k in d:
                d[k] = (cp.get("paths", k, fallback="") or "").strip()
    except Exception:
        pass
    return d


_P = _ini_paths()
PY = _P["python"] or sys.executable
USBIP = _P["usbip"]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(m):
    print(m, flush=True)


def ps(cmd):
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                              capture_output=True, timeout=60,
                              creationflags=NO_WINDOW)
    except Exception as e:
        log("  powershell 调用失败: %r" % e)
        return None


def main():
    log("1/5 停引擎与 crashpad 残留")
    ps("Get-Process 'xHaptics Engine' -EA SilentlyContinue | Stop-Process -Force -EA SilentlyContinue")
    ps("Get-CimInstance Win32_Process -Filter \"Name='crashpad_handler.exe'\" | "
       "Where-Object { -not (Get-Process -Id $_.ParentProcessId -EA SilentlyContinue) } | "
       "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }")

    log("2/5 停本项目 python 链路进程（含热插拔守护；按命令行特征，不误伤其他 python）")
    ps("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
       "Where-Object { $_.CommandLine -match "
       "'usbip_apex6|tcp_proxy|xinput_activity|k5_writer|watchdog|auto_keepalive|vibe_monitor|play_test_tone|run_live_test|hotplug' } | "
       "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }")
    time.sleep(1)

    log("3/5 摘除虚拟设备")
    try:
        subprocess.run([USBIP, "detach", "-p", "1"], capture_output=True,
                       timeout=30, creationflags=NO_WINDOW)
    except Exception as e:
        log("  detach 跳过: %r" % e)
    try:
        subprocess.run(["pnputil", "/remove-device", r"USB\VID_37D7&PID_2502"],
                       capture_output=True, timeout=30, creationflags=NO_WINDOW)
    except Exception as e:
        log("  remove-device 跳过: %r" % e)

    log("4/5 核对残留")
    r = ps("Get-CimInstance Win32_Process | Where-Object { "
           "$_.Name -in @('app.exe','server.exe','k5_writer.exe') -or "
           "$_.Name -like '*xHaptics*' } | Select-Object -ExpandProperty ProcessId")
    left = (r.stdout or b"").decode("utf-8", "replace").strip() if r else ""
    log("  残留核心进程: %s" % (left or "无"))
    log("5/5 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
