# hotplug_core.py — 手柄热插拔自动重载核心（P4 · 2026-11-15）
#
# 借鉴 APEXtoDS 的 CM_Register_Notification 热插拔管理，但只做一件事：
#   监听【真手柄 VID_37D7&PID_2501】的「拔出 → 插回」，自动调用已实证的重载编排，
#   替代 HANDOFF 里「手柄关机/离开过，回来必手动 reload_chain」这条手工步骤。
#
# 五条硬约束（都是本项目踩过的坑）：
#  1. 只认 2501 真手柄，绝不认 2502 虚拟设备 —— reload_chain / quick_cycle 自己会反复
#     detach + remove-device + attach 2502，按 2502 监听会自我触发成死循环。
#  2. 必须有完整的 在线→离线→在线 边沿才触发；启动时手柄不在线也【不】自动重载，
#     避免设备探测异常把正在稳定工作的链路误重启（第一优先是不打断已验证链路）。
#  3. CM 通知只是「提前唤醒」信号，设备在线与否的事实来源始终是 HID 枚举；
#     即使 CM_Register_Notification 不可用，退化成 1s 轮询，功能一点不减。
#  4. 回调运行在 CfgMgr 通知线程上，只置 Event 不执行业务逻辑；重载放到工作线程，
#     主线程持续监听，busy 期间不重入。
#  5. 冷却 240s（reload_chain 实测约 2.5 分钟），并给稳定确认留足多接口枚举时间。
#
# 本模块不含任何项目专属路径，可被 hotplug_reload.py（脚本链路）与 app.py（GUI 链路）共用。
import ctypes
import os
import subprocess
import threading
import time

try:
    from ctypes import wintypes
except Exception:                                    # pragma: no cover - 非 Windows
    wintypes = None

VID = 0x37D7
PID_REAL = 0x2501
PID_VIRT = 0x2502                                    # 明确排除，见约束 1
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DEFAULT_STABLE_S = 8.0        # 插回后连续在线多久才认定稳定（HID 多接口陆续枚举）
DEFAULT_GRACE_S = 2.0         # 拔出后持续离线多久才认定真的拔了（防瞬时抖动）
DEFAULT_COOLDOWN_S = 240.0    # 两次重载最小间隔
DEFAULT_POLL_S = 1.0          # 轮询兜底间隔

CM_NOTIFY_FILTER_FLAG_ALL_INTERFACE_CLASSES = 0x00000001
CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE = 0
MAX_DEVICE_ID_LEN = 200
CR_SUCCESS = 0


# --------------------------------------------------------------------------
# 设备在线探测：优先 hid.enumerate（已在本机实证），失败降级 PowerShell PnP
# --------------------------------------------------------------------------
def present_hid():
    """返回真手柄 2501 的 HID 接口数量。只读枚举，不打开设备，不与 k5_writer 抢句柄。"""
    import hid
    return sum(1 for d in hid.enumerate()
               if d.get("vendor_id") == VID and d.get("product_id") == PID_REAL)


_PNP_CACHE = {"t": 0.0, "v": 0}


def present_pnp():
    """降级路径：Get-PnpDevice -PresentOnly，5 秒缓存，抑制控制台窗口。"""
    now = time.time()
    if now - _PNP_CACHE["t"] < 5.0:
        return _PNP_CACHE["v"]
    cmd = ("(Get-PnpDevice -PresentOnly -EA SilentlyContinue | "
           "Where-Object { $_.InstanceId -like '*VID_37D7&PID_2501*' } | "
           "Measure-Object).Count")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=20,
                       creationflags=NO_WINDOW)
    try:
        v = int(((r.stdout or "").strip() or "0").splitlines()[-1])
    except (ValueError, IndexError):
        v = 0
    _PNP_CACHE.update(t=now, v=v)
    return v


def make_present_fn():
    try:
        import hid                                    # noqa: F401
        return present_hid
    except Exception:
        return present_pnp


# --------------------------------------------------------------------------
# CfgMgr32 设备接口通知（CM_Register_Notification / CM_Unregister_Notification）
# --------------------------------------------------------------------------
if os.name == "nt" and wintypes is not None:
    class _CMNotifyFilterUnion(ctypes.Union):
        _fields_ = [
            ("ClassGuid", ctypes.c_byte * 16),
            ("hTarget", ctypes.c_void_p),
            ("InstanceId", ctypes.c_wchar * MAX_DEVICE_ID_LEN),
        ]

    class CM_NOTIFY_FILTER(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("Flags", wintypes.DWORD),
            ("Type", ctypes.c_uint32),
            ("u", _CMNotifyFilterUnion),
        ]

    CM_NOTIFY_CALLBACK = ctypes.WINFUNCTYPE(
        wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_void_p, wintypes.DWORD)
else:                                                 # pragma: no cover
    CM_NOTIFY_FILTER = None
    CM_NOTIFY_CALLBACK = None


class CmNotifier:
    """设备接口通知。注册失败时 ok=False，调用方自动退化为纯轮询（功能不减）。"""

    def __init__(self, log=None):
        self.ok = False
        self.error = ""
        self.event = threading.Event()
        self.actions = {}
        self._cb = None
        self._handle = None
        self._unreg = None
        self._log = log or (lambda m: None)
        if CM_NOTIFY_FILTER is None:
            self.error = "非 Windows 平台"
            return
        try:
            dll = ctypes.WinDLL("cfgmgr32.dll")
            reg = dll.CM_Register_Notification
            reg.argtypes = [ctypes.POINTER(CM_NOTIFY_FILTER), ctypes.c_void_p,
                            CM_NOTIFY_CALLBACK, ctypes.POINTER(ctypes.c_void_p)]
            reg.restype = ctypes.c_uint32
            self._unreg = dll.CM_Unregister_Notification
            self._unreg.argtypes = [ctypes.c_void_p]
            self._unreg.restype = ctypes.c_uint32

            # 必须持有回调对象的强引用，否则被 GC 后通知线程会跳到野指针
            self._cb = CM_NOTIFY_CALLBACK(self._on_notify)
            filt = CM_NOTIFY_FILTER()
            filt.cbSize = ctypes.sizeof(CM_NOTIFY_FILTER)
            filt.Flags = CM_NOTIFY_FILTER_FLAG_ALL_INTERFACE_CLASSES
            filt.Type = CM_NOTIFY_FILTER_TYPE_DEVICEINTERFACE
            handle = ctypes.c_void_p()
            ret = reg(ctypes.byref(filt), None, self._cb, ctypes.byref(handle))
            if ret == CR_SUCCESS:
                self._handle = handle
                self.ok = True
                self._log("CM_Register_Notification 成功（sizeof filter=%d）"
                          % filt.cbSize)
            else:
                self.error = "CONFIGRET=%d" % ret
        except Exception as e:
            self.error = repr(e)

    def _on_notify(self, handle, context, action, data, size):
        # CfgMgr 通知线程：只计数 + 置事件，绝不在这里做任何业务逻辑
        try:
            self.actions[action] = self.actions.get(action, 0) + 1
        except Exception:
            pass
        self.event.set()
        return 0

    def wait(self, timeout):
        self.event.wait(timeout)
        self.event.clear()

    def close(self):
        if self.ok and self._handle is not None and self._unreg is not None:
            try:
                self._unreg(self._handle)
            except Exception:
                pass
        self.ok = False


# --------------------------------------------------------------------------
# 状态机
# --------------------------------------------------------------------------
class HotplugWatcher:
    """在线→离线→在线 边沿检测 + 去抖 + 冷却 + 单飞执行。

    依赖全部可注入（present_fn / now_fn / notifier / on_trigger），
    因此 test_hotplug.py 能完全离线驱动，不需要真的插拔手柄。
    """

    def __init__(self, base, on_trigger, log_path=None, dry_run=False,
                 stable_s=DEFAULT_STABLE_S, grace_s=DEFAULT_GRACE_S,
                 cooldown_s=DEFAULT_COOLDOWN_S, poll_s=DEFAULT_POLL_S,
                 present_fn=None, now_fn=None, notifier=None, use_cm=True):
        self.base = base
        self.on_trigger = on_trigger
        self.log_path = log_path or os.path.join(base, "hotplug.log")
        self.dry_run = dry_run
        self.stable_s = float(stable_s)
        self.grace_s = float(grace_s)
        self.cooldown_s = float(cooldown_s)
        self.poll_s = float(poll_s)
        self.present_fn = present_fn or make_present_fn()
        self.now_fn = now_fn or time.time
        self.notifier = notifier
        if self.notifier is None and use_cm:
            self.notifier = CmNotifier(log=self.log)

        self._armed = False          # 观测到「可靠在线后又离线」才置位
        self._online_seen = False    # 必须可靠观测到在线（防探测异常误触发）
        self._present_since = None
        self._absent_since = None
        self._last_trigger = -1e18
        self._busy = False
        self._lock = threading.Lock()
        self.triggers = 0
        self.lines = []

    def log(self, msg):
        line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
        self.lines.append(line)
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def tick(self, now, present_count):
        """推进一次状态判定。返回动作名；'trigger' 表示应发起一次重载。"""
        present = present_count > 0
        with self._lock:
            if self._busy:
                return "busy"
            if present:
                self._online_seen = True
                self._absent_since = None
                if self._armed:
                    if self._present_since is None:
                        self._present_since = now
                        return "settling"
                    if now - self._present_since < self.stable_s:
                        return "settling"
                    if now - self._last_trigger < self.cooldown_s:
                        return "cooldown"
                    self._armed = False
                    self._present_since = None
                    self._last_trigger = now
                    self.triggers += 1
                    return "trigger"
                self._present_since = None
                return "idle"
            # 离线：只有此前确实观测到在线，才允许武装
            self._present_since = None
            if self._absent_since is None:
                self._absent_since = now
            if self._online_seen and now - self._absent_since >= self.grace_s:
                self._armed = True
                return "absent-armed"
            return "absent"

    def fire(self):
        """工作线程里执行重载；dry-run 只记日志。"""
        with self._lock:
            if self._busy:
                return
            self._busy = True
        try:
            self.log("触发自动重载%s" % ("（dry-run，跳过实际执行）" if self.dry_run else ""))
            if not self.dry_run:
                self.on_trigger()
            self.log("自动重载调用结束，进入 %.0fs 冷却" % self.cooldown_s)
        except Exception as e:
            self.log("自动重载失败: %r" % e)
        finally:
            with self._lock:
                self._busy = False

    def run(self, duration=None, sync_fire=False):
        n = self.notifier
        if n is not None and not n.ok:
            self.log("CM 通知不可用（%s）→ 降级为 %.1fs 轮询，功能不减"
                     % (n.error or "未注册", self.poll_s))
        self.log("热插拔重载守护启动：目标 VID_%04X&PID_%04X（排除 %04X 虚拟设备）"
                 " 稳定=%.0fs 拔出确认=%.0fs 冷却=%.0fs dry_run=%s"
                 % (VID, PID_REAL, PID_VIRT, self.stable_s, self.grace_s,
                    self.cooldown_s, self.dry_run))
        t0 = self.now_fn()
        last_act = None
        try:
            while True:
                now = self.now_fn()
                try:
                    count = self.present_fn()
                except Exception as e:
                    # 探测失败时保持状态不变，绝不把「读不到」当成「设备拔了」
                    self.log("设备探测异常（本轮跳过判定）: %r" % e)
                    count = None
                if count is not None:
                    act = self.tick(now, count)
                    if act != last_act:
                        self.log("状态 %s → %s（在线接口=%d）"
                                 % (last_act or "init", act, count))
                        last_act = act
                    if act == "trigger":
                        if sync_fire:
                            self.fire()
                        else:
                            threading.Thread(target=self.fire, daemon=True,
                                             name="hotplug-reload").start()
                if duration is not None and now - t0 >= duration:
                    break
                if n is not None and n.ok:
                    n.wait(self.poll_s)
                else:
                    time.sleep(self.poll_s)
        finally:
            if n is not None:
                self.log("CM 通知回调累计: %s（ok=%s）" % (n.actions, n.ok))
                n.close()
            self.log("热插拔重载守护退出（累计触发 %d 次）" % self.triggers)
