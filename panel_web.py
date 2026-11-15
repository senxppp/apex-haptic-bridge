# panel_web.py — 控制台界面（WebView2 + HTML/CSS/JS）
#
# 为什么不用 Tk：Tk 的控件是系统原生绘制，做不出 180ms 缓动、数字补间、折线动画
# 这类手感；换 WebView2 之后界面就是普通网页，视觉完全可控。
#
# 分工：这个文件只做「状态搬运 + 参数写入 + 命令投递」，不碰设备。
# 设备操作一律走 app.py 注入的回调（recycler / reloader），与旧面板一致。
import collections
import json
import os
import subprocess
import sys
import threading
import time

if getattr(sys, "frozen", False):
    BASE = os.path.dirname(sys.executable)
else:
    BASE = os.path.dirname(os.path.abspath(__file__))

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
CFG_PATH = os.path.join(BASE, "motor_config.json")
STATS = os.path.join(BASE, "stats.log")
PROBE = os.path.join(BASE, "k5_probe.log")
MUTE = os.path.join(BASE, "mute.flag")   # 服务端每秒看一次，存在=静音
CMD = os.path.join(BASE, "cmd.txt")      # 服务端每 0.5 秒看一次，取走即删


def _ui_dir():
    if getattr(sys, "frozen", False):
        for c in (os.path.join(getattr(sys, "_MEIPASS", ""), "ui"),
                  os.path.join(os.path.dirname(sys.executable), "ui")):
            if os.path.isdir(c):
                return c
    return os.path.join(BASE, "ui")


def _tail_json(path, max_bytes=4000):
    """读文件尾部最后一行 JSON（探针/状态都是一行一条）"""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read().decode("utf-8", "replace")
        for line in reversed(data.splitlines()):
            line = line.strip()
            if line.startswith("{"):
                return json.loads(line)
    except Exception:
        pass
    return None


def _stats_line():
    try:
        with open(STATS, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 2000))
            data = f.read().decode("utf-8", "replace")
        lines = [x for x in data.splitlines() if x.strip()]
        return lines[-1] if lines else ""
    except Exception:
        return ""


def _engine_pid():
    """Toolhelp32 枚举，零子进程（windowed 下不弹黑框）"""
    try:
        import ctypes
        from ctypes import wintypes

        class PE32(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return ""
        try:
            e = PE32()
            e.dwSize = ctypes.sizeof(PE32)
            ok = k32.Process32FirstW(snap, ctypes.byref(e))
            while ok:
                n = e.szExeFile.lower()
                if "xhaptics" in n and "engine" in n:
                    return str(e.th32ProcessID)
                ok = k32.Process32NextW(snap, ctypes.byref(e))
            return ""
        finally:
            k32.CloseHandle(snap)
    except Exception:
        return ""


class Api:
    def __init__(self, recycler=None, reloader=None, issues=None):
        self._recycler = recycler
        self._reloader = reloader
        self._log = collections.deque(maxlen=80)
        self._prev = (None, None)      # (writes, t) 用于算写入速率
        self._last_frames = None
        self._last_mode = "常态"
        for m in (issues or []):
            self.plog(m)

    # ---- 日志 ----
    def plog(self, msg):
        self._log.append("[%s] %s" % (time.strftime("%H:%M:%S"), msg))

    def _reset_log(self):
        pass

    # ---- 配置 ----
    def cfg(self):
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            self.plog("读取 motor_config.json 失败")
            return {}

    def set_cfg(self, key, value):
        """只接受数值键，白名单外直接拒绝——避免前端手滑把整份配置写坏"""
        allowed = {"tr_p2_max", "tr_head_ms", "tr_lockout_ms", "tr_release_ms",
                   "tr_p3_min", "tr_p3_max", "tf_enable", "tf_max", "tf_base",
                   "tr_enable", "threshold", "strength"}
        if key not in allowed:
            return {"ok": False, "message": "不支持的参数"}
        try:
            v = int(value)
        except Exception:
            return {"ok": False, "message": "参数必须是整数"}
        try:
            with open(CFG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f, object_pairs_hook=collections.OrderedDict)
            cfg[key] = v
            with open(CFG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=1)
            self.plog("%s = %d（热加载约 1s）" % (key, v))
            return {"ok": True, "message": "%s 已改为 %d" % (key, v)}
        except Exception as e:
            self.plog("写配置失败: %r" % (e,))
            return {"ok": False, "message": "写配置失败"}

    # ---- 静音 ----
    def set_mute(self, on):
        try:
            if on:
                with open(MUTE, "w", encoding="utf-8") as f:
                    f.write(str(int(time.time())))
                self.plog("已暂停震动（马达归零 + 扳机回落）")
                return {"ok": True, "message": "已暂停，声音照常但不震"}
            if os.path.exists(MUTE):
                os.remove(MUTE)
            self.plog("已恢复震动")
            return {"ok": True, "message": "已恢复"}
        except Exception as e:
            self.plog("切换静音失败: %r" % (e,))
            return {"ok": False, "message": "切换失败"}

    def muted(self):
        return os.path.exists(MUTE)

    # ---- 动作 ----
    def action(self, name):
        if name == "zero":
            try:
                with open(CMD, "w", encoding="utf-8") as f:
                    f.write("zero")
                self.plog("马达归零（当前会话，不摘设备）")
                return {"ok": True, "message": "马达已归零"}
            except Exception as e:
                return {"ok": False, "message": "投递失败: %r" % (e,)}
        if name == "test":
            try:
                with open(CMD, "w", encoding="utf-8") as f:
                    f.write("buzz")
                self.plog("测试强震 255（约 2 秒后自动归零）")
                return {"ok": True, "message": "已发强震"}
            except Exception as e:
                return {"ok": False, "message": "投递失败: %r" % (e,)}
        if name in ("recycle", "reload"):
            fn = self._recycler if name == "recycle" else self._reloader
            if fn is None:
                return {"ok": False, "message": "当前形态不支持（请用脚本形态）"}

            def run():
                try:
                    self.plog("开始 %s …" % ("设备重连" if name == "recycle" else "重载全链路"))
                    fn()
                    self.plog("%s 完成" % ("设备重连" if name == "recycle" else "重载全链路"))
                except Exception as e:
                    self.plog("%s 失败: %r" % (name, e))
            threading.Thread(target=run, daemon=True).start()
            return {"ok": True, "message": "已开始，日志里会有进度"}
        return {"ok": False, "message": "未知动作"}

    # ---- 状态 ----
    def state(self):
        probe = _tail_json(PROBE) or {}
        stats = _stats_line()
        writes = probe.get("writes") or 0
        now = time.time()
        w_per_s = 0.0
        pw, pt = self._prev
        if pw is not None and now - pt > 0.2:
            w_per_s = max(0.0, (writes - pw) / (now - pt))
        self._prev = (writes, now)

        ch = probe.get("level") or [0, 0]
        # 扳机通道不在 level 里（那是握把左右），从 trig_log 最后一条读模式
        trig = probe.get("trig_log") or []
        if trig:
            last = trig[-1]
            self._last_mode = "冲击" if "m2" in last else "常态"

        age = now - os.path.getmtime(STATS) if os.path.exists(STATS) else 999
        pid = _engine_pid()
        if not pid:
            link_state, link_text = "bad", "引擎未运行"
        elif age > 8:
            link_state, link_text = "warn", "流已静默 %s" % (
                ">99s" if age > 99 else "%ds" % int(age))
        else:
            link_state, link_text = "on", "链路在线"

        # 握手：看 stats 的 frames 是否还在涨（涨=引擎确实在发 0x57）
        frames = 0
        line = ""
        if stats:
            seg = stats.split("|")
            head = seg[0]
            if "frames=" in head:
                try:
                    frames = int(head.split("frames=")[1].split()[0])
                except Exception:
                    frames = 0
            verdict = ""
            for s in seg:
                if "判层=" in s:
                    verdict = s.split("判层=")[1].split("本周期")[0].strip()
            body = head.split("]")[-1].strip() if "]" in head else ""
            line = "%s · %s" % (body, verdict) if body else verdict

        if frames and self._last_frames is not None and frames > self._last_frames:
            handshake = "0x57 在流"
        elif frames:
            handshake = "0x57 静默"
        else:
            handshake = "等待握手"
        self._last_frames = frames

        return {
            "channels": [0, ch[0], ch[1]],
            "writes_per_s": round(w_per_s, 1),
            "fails": probe.get("fail", 0),
            "avg_ms": probe.get("avg_ms", 0.0),
            "link_state": link_state,
            "link_text": link_text,
            "engine_pid": pid or "",
            "handshake": handshake,
            "trig_mode": self._last_mode,
            "status_line": line,
            "muted": self.muted(),
            "log": list(self._log)[-40:],
        }


def _dark_titlebar(title, timeout=8.0):
    """把系统标题栏切成深色。

    WebView2 只管内容区，标题栏是 DWM 画的——不改就是白条压在深色界面上，
    一眼就是"半成品"。DWMWA_USE_IMMERSIVE_DARK_MODE：新版本 20，旧版本 19。
    """
    def work():
        try:
            import ctypes
            u = ctypes.WinDLL("user32", use_last_error=True)
            u.FindWindowW.restype = ctypes.c_void_p
            t0 = time.time()
            hwnd = 0
            while time.time() - t0 < timeout:
                hwnd = u.FindWindowW(None, title) or 0
                if hwnd:
                    break
                time.sleep(0.12)
            if not hwnd:
                return
            dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
            val = ctypes.c_int(1)
            for attr in (20, 19):
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr,
                                          ctypes.byref(val), ctypes.sizeof(val))
        except Exception:
            pass
    threading.Thread(target=work, daemon=True).start()


def run(recycler=None, reloader=None, issues=None):
    import webview

    api = Api(recycler=recycler, reloader=reloader, issues=issues)
    ui = _ui_dir()
    index = os.path.join(ui, "index.html")
    if not os.path.isfile(index):
        raise RuntimeError("缺少界面文件: %s" % index)

    api.plog("界面载入 %s" % os.path.basename(index))
    _dark_titlebar("haptics bridge")
    w = webview.create_window(
        "haptics bridge", url=index, js_api=api,
        width=1040, height=812, min_size=(900, 640),
        background_color="#08080a", text_select=False, easy_drag=False,
    )
    webview.start(debug=False, http_server=False)
    return w
