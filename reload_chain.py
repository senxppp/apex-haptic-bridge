# reload_chain.py — 一键重载全链路（控制台按钮调用）
# 顺序：杀引擎(+crashpad) → 杀服务/代理 → 摘设备 → 起服务/代理 → 挂设备 → 探针 → 起引擎
import os
import subprocess
import sys
import time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_RAW_RUN = subprocess.run


def run(*args, **kwargs):
    """统一抑制 console 子进程新建控制台窗口（windowed GUI 下不弹黑框）"""
    kwargs.setdefault("creationflags", NO_WINDOW)
    return _RAW_RUN(*args, **kwargs)


BASE = os.path.dirname(os.path.abspath(__file__))

# stdout 被重定向到管道/文件时，Python 会用系统代码页（cp936）编码。
# 下游按 UTF-8 读就是一堆"ͣ����"。统一成 UTF-8，控制台和日志都干净。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg):
    print(msg, flush=True)


def _ini_paths():
    """路径一律从 app.ini 读，和 app.py 共用一份配置——两处各写一套迟早分叉。

    以前这里写死了 usbip 位置、引擎位置、以及"必须用哪个 Python"的绝对路径：
    换台机器、换个 Python 目录，就是一堆看不到原因的失败。
    """
    import configparser
    keys = ("usbip", "engine", "engine_dir", "python", "fakegame")
    d = dict.fromkeys(keys, "")
    try:
        cp = configparser.ConfigParser()
        cp.read(os.path.join(BASE, "app.ini"), encoding="utf-8")
        if cp.has_section("paths"):
            for k in keys:
                d[k] = (cp.get("paths", k, fallback="") or "").strip()
    except Exception as e:
        log("[cfg] app.ini 读取失败: %r" % e)
    return d


_P = _ini_paths()
USBIP = _P["usbip"]
ENGINE = _P["engine"]
ENGINE_DIR = _P["engine_dir"] or (os.path.dirname(ENGINE) if ENGINE else "")
# 默认用当前解释器；只有确实需要另一个 Python（比如 hidapi 只装在那儿）才在 app.ini 指定
PY = _P["python"] or sys.executable
FAKE = _P["fakegame"] or BASE


def ps(cmd):
    run(["powershell", "-NoProfile", "-Command", cmd],
                   capture_output=True, text=True, errors="replace", timeout=60)


def spawn(script, cwd, logname):
    path = os.path.join(cwd, script) if cwd == BASE else script
    if not os.path.exists(path):
        # 可选组件（比如没有部署的 XInput 探针）不该让整条重载链崩在半路
        log("   [skip] 未找到 %s，跳过" % script)
        return None
    return subprocess.Popen(
        [PY, "-I", path],
        cwd=cwd,
        stdout=open(os.path.join(cwd, logname), "ab"),
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW)


def main():
    if not USBIP:
        log("[!!] app.ini 未配置 usbip 路径（复制 app.ini.example → app.ini）")
        return 2
    if not os.path.exists(USBIP):
        log("[!!] 找不到 usbip：%s" % USBIP)
        return 2
    log("0/8 暂停 watchdog（防抢跑导致引擎单实例冲突）")
    ps("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
       "Where-Object { $_.CommandLine -like '*watchdog*' } | "
       "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }")
    log("1/8 停引擎 + 清 crashpad 残留")
    ps("Get-Process 'xHaptics Engine' -EA SilentlyContinue | Stop-Process -Force")
    # 只清「父进程已退出」的孤儿 crashpad（引擎被杀后的残留）。
    # 旧逻辑按空 CommandLine 无差别清，会误杀 LenovoTray / LAVService 的 crashpad
    ps("Get-CimInstance Win32_Process -Filter \"Name='crashpad_handler.exe'\" | "
       "Where-Object { -not (Get-Process -Id $_.ParentProcessId -EA SilentlyContinue) } | "
       "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -EA SilentlyContinue }")
    log("2/8 停服务/代理/旧 XInput 探针（保留控制台和 watchdog）")
    # 旧版不清 xinput_activity，每重载一次就多一个探针同时 XInputSetState 互相覆盖
    ps("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
       "Where-Object { $_.CommandLine -like '*usbip_apex6*' -or $_.CommandLine -like '*tcp_proxy*' "
       "-or $_.CommandLine -like '*xinput_activity*' } | "
       "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }")
    time.sleep(1)
    log("3/8 摘除虚拟设备")
    run([USBIP, "detach", "-p", "1"], capture_output=True, timeout=30)
    run(["pnputil", "/remove-device", r"USB\VID_37D7&PID_2502"],
                   capture_output=True, timeout=30)
    time.sleep(1)
    log("4/8 启动服务端 + 代理")
    spawn("usbip_apex6.py", BASE, "server_v%d.log" % int(time.time() % 1000))
    time.sleep(2)
    spawn("tcp_proxy2.py", BASE, "proxy_outN.log")
    time.sleep(1)
    log("5/8 挂载虚拟设备")
    r = run([USBIP, "-t", "3241", "attach", "-r", "127.0.0.1", "-b", "1-1"],
                       capture_output=True, text=True, errors="replace", timeout=60)
    log("   " + ((r.stdout or r.stderr or "")).strip()[:50])
    time.sleep(6)
    log("6/9 启动 XInput 活动探针，等设备完全稳定 30 秒")
    spawn("xinput_activity.py", FAKE, "xinput_activity_r.log")
    time.sleep(30)
    log("7/9 启动官方引擎")
    if not ENGINE:
        log("   [skip] app.ini 未配置 engine 路径，请手动启动引擎")
    elif os.path.exists(ENGINE):
        ps("Start-Process '%s' -WorkingDirectory '%s'" % (ENGINE, ENGINE_DIR))
    else:
        log("   [!!] 引擎不存在：%s" % ENGINE)
    log("8/9 等引擎完成设备发现（监测 0x53 使能，最多 60 秒）…")
    newest = None
    for _ in range(60):
        time.sleep(1)
        import glob
        logs = sorted(glob.glob(os.path.join(BASE, "server_v*.log")), key=os.path.getmtime)
        if not logs:
            continue
        newest = logs[-1]
        try:
            with open(newest, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if "0x53 MotorRoute" in content:
                log("   检测到 0x53 使能（发现完成）")
                break
        except OSError:
            continue
    time.sleep(60)  # 引擎发现流程需自行超时一次，之后重连才会触发快速路径
    log("   设备重连（触发快速路径开播）")
    run([USBIP, "detach", "-p", "1"], capture_output=True, timeout=30)
    run(["pnputil", "/remove-device", r"USB\VID_37D7&PID_2502"],
                   capture_output=True, timeout=30)
    time.sleep(1)
    r = run([USBIP, "-t", "3241", "attach", "-r", "127.0.0.1", "-b", "1-1"],
                       capture_output=True, text=True, errors="replace", timeout=60)
    log("   " + ((r.stdout or r.stderr or "")).strip()[:50])
    time.sleep(15)
    # 旧的检测读 server_v*.log 里的 "[0x57" / "[stats" 标记，但 0x57 帧根本不打印、
    # stats 由独立线程写进 stats.log → 检测恒为失败 → 每次都额外重连一次，
    # 把刚点燃的会话打断。改为直接观测 stats.log 的 frames 增长，且不再自动重连。
    log("   验证 0x57 流（stats.log 的 frames 增长）…")
    import re

    def last_stats():
        try:
            with open(os.path.join(BASE, "stats.log"), "rb") as f:
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 2000))
                d = f.read().decode("utf-8", "replace")
            v = re.findall(r"frames=(\d+)\s+nonzero=(\d+)\s+maxL=\d+\s+maxR=\d+\s+k5w=(\d+)", d)
            return tuple(map(int, v[-1])) if v else (0, 0, 0)
        except OSError:
            return (0, 0, 0)

    prev = last_stats()
    ok = False
    for _ in range(6):
        time.sleep(5)
        cur = last_stats()
        log("   stats frames=%d nonzero=%d k5w=%d" % cur)
        if cur[0] > prev[0] and cur[0] > 100:
            ok = True
            break
        prev = cur
    if ok:
        log("   [OK] 0x57 流持续增长（保持链路不动，不打断会话）")
    else:
        log("   [!!] 未检测到流，保持链路不动；请在控制台手动再按一次重载")
    log("9/9 重启 watchdog")
    spawn("watchdog.py", BASE, "watchdog_outN.log")
    log("9/9 完成（链路已就绪，放音源即可震动）")
    return 0


if __name__ == "__main__":
    # 这个脚本没有参数：一执行就会拆掉整条链路。随手敲 --help 就真的开跑了，
    # 2026-11-15 实测踩过一次（把刚起来的引擎和 watchdog 一起停了）。
    if len(sys.argv) > 1:
        print("用法: python -I reload_chain.py    （无参数；执行会重启整条链路，约 3 分钟）")
        print("只想看配置是否就绪：python -c \"import reload_chain as r; print(r.USBIP, r.ENGINE, r.PY)\"")
        sys.exit(64)
    sys.exit(main())
