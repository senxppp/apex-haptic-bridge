# app.py — 八爪鱼 k5 主程序（PyInstaller 入口）
# 打包形态：app.exe（本文件，GUI+编排）+ server.exe（USB 服务端）+ k5_writer.exe（写进程）
# 编排职责：服务端进程管理 / 设备 attach / 引擎启动 / 冻结自愈（内联 keepalive）
#
# 2026-11-14 深夜修复（首启弹两次「错误框」）：
#   app.exe 是 windowed 程序，拉起 pnputil / powershell / usbip 这类 console 子进程时，
#   若不显式抑制，Windows 会为每个子进程新建控制台窗口（黑框），看起来就像错误弹窗。
#   实测启动序列：quick_cycle() 的 pnputil 弹一次，GUI 状态栏每 2s 的 PowerShell 又弹。
#   修复 = 所有外部进程调用统一 CREATE_NO_WINDOW + 启动异常全部落盘 app_error.log，
#   windowed 模式下不再出现 PyInstaller 的未捕获异常对话框。
import configparser
import os
import subprocess
import sys
import threading
import time
import traceback

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

# 非 Windows 平台 CREATE_NO_WINDOW 不存在时退化为 0（无副作用）
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
ERR_LOG = os.path.join(BASE, "app_error.log")
STARTUP_ISSUES = []
_SEEN_ISSUES = set()

# 默认留空：路径一律由同目录的 app.ini 提供（见 app.ini.example）。
# 仓库里不写死任何本机路径——写死过一次，换台机器就成了"静默不工作"。
USBIP = ""
ENGINE = ""
ENGINE_DIR = ""
STATS = os.path.join(BASE, "stats.log")
SERVER_EXE = os.path.join(BASE, "server.exe")
SERVER_PY = os.path.join(BASE, "usbip_apex6.py")

# app.ini 可覆盖路径（分发到别的机器时改这里）
try:
    _ini = configparser.ConfigParser()
    _ini.read(os.path.join(BASE, "app.ini"), encoding="utf-8")
    if _ini.has_section("paths"):
        USBIP = _ini.get("paths", "usbip", fallback=USBIP)
        ENGINE = _ini.get("paths", "engine", fallback=ENGINE)
        ENGINE_DIR = _ini.get("paths", "engine_dir", fallback=ENGINE_DIR)
except Exception as e:
    STARTUP_ISSUES.append("app.ini 读取失败: %r" % e)

# 配置缺失时明确告警，而不是等到"设备挂不上"再猜
_MISSING = [n for n, v in (("usbip", USBIP), ("engine", ENGINE)) if not v]
if _MISSING:
    STARTUP_ISSUES.append("app.ini 未配置路径: %s（复制 app.ini.example 为 app.ini 后填入）"
                          % ", ".join(_MISSING))
elif not ENGINE_DIR:
    ENGINE_DIR = os.path.dirname(ENGINE)

_proc_srv = {"p": None}
# 编排互斥锁：keepalive 的 quick_cycle、GUI 按钮的 reload_inline、热插拔自动重载
# 三者不能并发操作同一套设备（RLock 可重入，reload_inline 内部仍可调 quick_cycle）
CHAIN_LOCK = threading.RLock()


def log_error(label, exc=None):
    """异常写日志（windowed 模式没有控制台，日志是唯一线索；绝不弹框）"""
    try:
        with open(ERR_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), label))
            if exc is not None:
                f.write("".join(traceback.format_exception(
                    type(exc), exc, exc.__traceback__)))
            f.write("\n")
    except Exception:
        pass


def note(label, exc=None):
    """记录非致命启动问题：落盘 + 稍后在 GUI 日志区显示。
    同类问题只记一次（设备重连会被 keepalive 反复调用，避免日志被刷爆）。"""
    key = (label, type(exc).__name__ if exc is not None else "")
    if key in _SEEN_ISSUES:
        return
    _SEEN_ISSUES.add(key)
    msg = label if exc is None else "%s: %r" % (label, exc)
    STARTUP_ISSUES.append(msg)
    log_error(label, exc)


def _server_cmd():
    """打包形态直接跑 server.exe；源码形态（没有 exe）跑 usbip_apex6.py。

    仓库里只有源码，所以这条分支是默认路径。`-I` 隔离模式不把脚本目录放进
    sys.path，usbip_apex6.py 自己做了显式 insert（否则 trig_* 会静默导入失败）。
    """
    if os.path.exists(SERVER_EXE):
        return [SERVER_EXE]
    return [sys.executable, "-I", SERVER_PY]


def server_start():
    p = subprocess.Popen(_server_cmd(),
                         cwd=BASE,
                         stdout=open(os.path.join(BASE, "srv_out.log"), "ab"),
                         stderr=subprocess.STDOUT,
                         creationflags=NO_WINDOW)
    _proc_srv["p"] = p
    return p


def server_stop():
    p = _proc_srv.get("p")
    if p and p.poll() is None:
        try:
            p.kill()
        except Exception:
            pass
    _proc_srv["p"] = None


def usbip_detach():
    try:
        subprocess.run([USBIP, "detach", "-p", "1"],
                       capture_output=True, timeout=30,
                       creationflags=NO_WINDOW)
    except Exception as e:
        note("usbip detach 跳过", e)


def usbip_attach():
    try:
        r = subprocess.run([USBIP, "-t", "3241", "attach", "-r", "127.0.0.1", "-b", "1-1"],
                           capture_output=True, timeout=60,
                           creationflags=NO_WINDOW)
        return "attached" in (r.stdout or b"").decode("utf-8", "replace")
    except Exception as e:
        note("usbip attach 跳过", e)
        return False


def quick_cycle():
    """设备快速重连：detach → 移除设备节点 → attach（~15 秒）"""
    usbip_detach()
    try:
        subprocess.run(["pnputil", "/remove-device", r"USB\VID_37D7&PID_2502"],
                       capture_output=True, timeout=30,
                       creationflags=NO_WINDOW)
    except Exception as e:
        note("pnputil 移除设备跳过", e)
    time.sleep(1)
    return usbip_attach()


def engine_start():
    try:
        subprocess.Popen([ENGINE], cwd=ENGINE_DIR,
                         creationflags=NO_WINDOW)
    except Exception:
        try:
            os.startfile(ENGINE)  # 需要提权时走 ShellExecute
        except Exception:
            pass


def engine_kill():
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process 'xHaptics Engine' -EA SilentlyContinue | Stop-Process -Force"],
            capture_output=True, timeout=60,
            creationflags=NO_WINDOW)
    except Exception as e:
        note("引擎停止跳过", e)
    time.sleep(2)


def reload_inline():
    """一键重载（内联版）：停服务 → 杀引擎 → 起服务 → 挂设备 → 起引擎 → 重连触发快速路径"""
    with CHAIN_LOCK:
        server_stop()
        engine_kill()
        server_start()
        time.sleep(1)
        quick_cycle()
        engine_start()
        time.sleep(20)   # 引擎设备发现（0x53 路由同步）
        quick_cycle()    # 触发快速路径开播


def keepalive_loop():
    """停帧即时自愈（内联版）：k5_probe.log 1s 心跳停更 ~5s → 立即设备快速重连。

    与外部 auto_keepalive.py 互斥：外部守护每秒 touch guard.alive，本线程见到
    新鲜标记就让位，避免两条路径并发操作同一套设备。若模块不可用则退回旧 45s 逻辑。
    """
    try:
        from frame_guard_core import FrameState, heartbeat_ages, pick_source, link_alive
    except Exception as e:
        note("秒级保活模块不可用，退回 45s 旧判据", e)
        _keepalive_legacy()
        return
    st = FrameState(stall=4.0, cooldown=45.0, confirm=2)
    guard_flag = os.path.join(BASE, "guard.alive")
    while True:
        try:
            try:
                external = time.time() - os.path.getmtime(guard_flag) < 5
            except OSError:
                external = False   # 标记不存在 = 外部守护不在场
            if external:
                time.sleep(2)      # 让位
                continue
            _, age, extra = pick_source(heartbeat_ages(BASE))
            d = st.observe(time.time(), age, st.stall + extra,
                           link_alive=link_alive())
            if d == "trigger":
                st.on_trigger(time.time())
                with CHAIN_LOCK:
                    quick_cycle()
        except Exception:
            pass
        time.sleep(0.5)


def _keepalive_legacy():
    """兜底：stats.log mtime >45s → 设备快速重连（旧行为，仅模块缺失时走）"""
    last = 0.0
    while True:
        try:
            age = time.time() - os.path.getmtime(STATS)
            if age > 45 and time.time() - last > 90:
                last = time.time()
                with CHAIN_LOCK:
                    quick_cycle()
        except Exception:
            pass
        time.sleep(10)


def start_hotplug():
    """P4 热插拔自动重载：手柄关机再开机 → 自动重建会话，替代手动 reload_chain。

    全程在后台守护线程；启动失败只记日志，绝不影响主链路与 GUI。
    只认真手柄 2501（虚拟 2502 由编排自己反复重挂，认它会自我触发死循环）。
    """
    try:
        import hotplug_core
        w = hotplug_core.HotplugWatcher(
            base=BASE, on_trigger=reload_inline,
            log_path=os.path.join(BASE, "hotplug.log"))
        threading.Thread(target=w.run, daemon=True,
                         name="hotplug-watch").start()
    except Exception as e:
        note("热插拔守护启动失败（不影响主链路）", e)


def main():
    # 编排段整体兜底：任何一步失败都只记日志，不让 GUI 起不来、更不弹异常框
    try:
        server_start()
        time.sleep(2)
        quick_cycle()
        engine_start()
        time.sleep(12)   # 引擎设备发现
        quick_cycle()    # 快速路径开播
    except Exception as e:
        note("启动编排中断（GUI 仍会启动）", e)

    try:
        threading.Thread(target=keepalive_loop, daemon=True).start()
        start_hotplug()
        import motor_panel
        p = motor_panel.Panel(recycler=quick_cycle, reloader=reload_inline)
        for msg in STARTUP_ISSUES:
            p.plog("⚠ " + msg)
        p.run()
    except Exception as e:
        note("GUI 启动失败", e)
    finally:
        # GUI 关闭 = 退出全部
        server_stop()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # 顶层兜底：windowed 模式下未捕获异常会弹 PyInstaller traceback 框，这里全部吃掉
        log_error("未捕获异常（app.exe 退出）", e)
