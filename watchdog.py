# watchdog.py — 最简守护（2026-11-07 按用户要求移除自动重挂/冻结自愈）
# 只做一件事：引擎进程死亡时重启它（+清 crashpad）。其它问题交给控制台的「一键重载」。
import os
import subprocess
import time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_RAW_RUN = subprocess.run


def run(*args, **kwargs):
    """统一抑制 console 子进程新建控制台窗口（windowed GUI 下不弹黑框）"""
    kwargs.setdefault("creationflags", NO_WINDOW)
    return _RAW_RUN(*args, **kwargs)


BASE = os.path.dirname(os.path.abspath(__file__))
WD_LOG = os.path.join(BASE, "watchdog.log")


def _engine_paths():
    """引擎路径从 app.ini 读；没配就返回空，主循环里只告警不崩。"""
    import configparser
    exe = d = ""
    try:
        cp = configparser.ConfigParser()
        cp.read(os.path.join(BASE, "app.ini"), encoding="utf-8")
        if cp.has_section("paths"):
            exe = (cp.get("paths", "engine", fallback="") or "").strip()
            d = (cp.get("paths", "engine_dir", fallback="") or "").strip()
    except Exception:
        pass
    return exe, (d or (os.path.dirname(exe) if exe else ""))


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    with open(WD_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def ps(cmd):
    return run(["powershell", "-NoProfile", "-Command", cmd],
                          capture_output=True, text=True, timeout=60).stdout.strip()


def main():
    log("watchdog 最简版启动（只守护引擎进程存活；重挂/重载请在控制台手动操作）")
    engine, engine_dir = _engine_paths()
    if not engine:
        log("[!!] app.ini 未配置 engine 路径，watchdog 无法重启引擎，仅监视")
    while True:
        try:
            eng = ps("(Get-Process 'xHaptics Engine' -EA SilentlyContinue).Id").strip()
            if not eng:
                log("engine DEAD → 清 crashpad 残留并重启引擎")
                ps("Get-CimInstance Win32_Process -Filter \"Name='crashpad_handler.exe'\" | "
                   "Where-Object { -not (Get-Process -Id $_.ParentProcessId -EA SilentlyContinue) } | "
                   "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }")
                time.sleep(2)
                if engine:
                    ps("Start-Process '%s' -WorkingDirectory '%s'" % (engine, engine_dir))
                    log("engine restarted")
                else:
                    log("跳过重启：未配置引擎路径")
            time.sleep(15)
        except Exception as e:
            log("err: %r" % e)
            time.sleep(15)


if __name__ == "__main__":
    main()
