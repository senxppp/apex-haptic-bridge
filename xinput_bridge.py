# -*- coding: utf-8 -*-
"""xinput_bridge.py — 把物理手柄的真实操作桥接给「我们模拟的 APEX6」。

为什么要这个（2026-11-21 用户定案，做法同 ApexSenseBridge）：
  引擎只认它自己那台设备（我们模拟的 37D7:2502），而那台虚拟设备原本没有真实输入，
  只能靠每 0.5s 假半扣一次 LT 来"证明有人"，既不真也不稳。
  物理手柄（37D7:2501 → XInput）时时刻刻在被驱动轮询、状态一直在刷新，
  直接读它的 XInput 状态填进虚拟设备的输入报告，就不需要任何"假扮"了。

为什么读 XInput 而不是飞智的厂商 HID 报告：
  · XINPUT_GAMEPAD 的字段顺序与我们输出的 20B Xbox360 报告**完全一致**，1:1 直填；
  · 纯读，不开厂商句柄，飞智空间站的原生功能（扳机防抖/映射/背键）照旧生效；
  · 体感不必在这里管——空间站或社区映射已经把陀螺仪映射成摇杆/鼠标。

⚠ 最大的坑：XInput 槽位索引会漂移。
  拔掉物理手柄后它从 XInput 消失，我们自己的虚拟设备会滑到前面，
  这时按索引读就会读到"自己注入的输入"，形成自激（表现为输入永远不变）。
  所以定位物理手柄不能靠索引，只能靠**特征**：
    · 真实设备的状态刷新率约 100~250 包/s（实测 119/s）；
    · 我们自己的虚拟设备被驱动高频轮询，包号涨得极快（实测 6000+/s）；
    · 实在分不清 → 桥自动关闭，回落到原来的假半扣。
"""

import ctypes as C
import threading
import time
from ctypes import wintypes as W

# ---- XInput 结构（与 20B Xbox360 报告字段顺序一致）----


class XINPUT_GAMEPAD(C.Structure):
    _fields_ = [("wButtons", W.WORD),
                ("bLeftTrigger", C.c_ubyte), ("bRightTrigger", C.c_ubyte),
                ("sThumbLX", C.c_short), ("sThumbLY", C.c_short),
                ("sThumbRX", C.c_short), ("sThumbRY", C.c_short)]


class XINPUT_STATE(C.Structure):
    _fields_ = [("dwPacketNumber", W.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


_xi = None
for _dll in ("xinput1_4.dll", "xinput9_1_0.dll"):
    try:
        _xi = C.WinDLL(_dll)
        break
    except OSError:
        continue

if _xi is not None:
    _get_state = _xi.XInputGetState
    _get_state.argtypes = [W.DWORD, C.POINTER(XINPUT_STATE)]
    _get_state.restype = W.DWORD
else:                                   # 非 Windows / 没有 XInput：桥永久禁用
    _get_state = None


def _read(idx):
    """读一个槽位。返回 (packet, state) 或 None。"""
    if _get_state is None:
        return None
    st = XINPUT_STATE()
    if _get_state(idx, C.byref(st)) != 0:
        return None
    g = st.Gamepad
    return st.dwPacketNumber, (g.wButtons, g.bLeftTrigger, g.bRightTrigger,
                               g.sThumbLX, g.sThumbLY, g.sThumbRX, g.sThumbRY)


def to_report(snap):
    """快照 → 20B Xbox360/XInput 输入报告（xusb22 认的格式）。

    布局： [0]=0x00 [1]=0x14 [2:4]=按键(小端) [4]=LT [5]=RT
           [6:8]=LX [8:10]=LY [10:12]=RX [12:14]=RY（有符号小端）
    与 XINPUT_GAMEPAD 的字段顺序一一对应，所以是直填、不做任何换算。
    """
    b, lt, rt, lx, ly, rx, ry = snap
    rep = bytearray(20)
    rep[0], rep[1] = 0x00, 0x14
    rep[2] = b & 0xFF
    rep[3] = (b >> 8) & 0xFF
    rep[4] = max(0, min(255, lt))
    rep[5] = max(0, min(255, rt))

    def put(off, v):
        v = max(-32768, min(32767, int(v)))
        rep[off] = v & 0xFF
        rep[off + 1] = (v >> 8) & 0xFF

    put(6, lx)
    put(8, ly)
    put(10, rx)
    put(12, ry)
    return bytes(rep)


# 松手判据：摇杆在 6% 行程内（2000/32768）、扳机为零、无按键 → 视为"手离开了手柄"。
# 只在**松手**时才允许伪造扳机保活：玩家只要在推摇杆/按键，引擎就绝不会超时，
# 这时候再伪造 LT=40 就是纯粹的干扰（2026-11-21 实测：推着摇杆保持不动 0.4 秒
# 就会触发保活，整轮 40% 的时间 LT 被顶到 40，游戏里等于一直举枪）。
_IDLE_AXIS = 2000


def _is_idle(snap):
    btn, lt, rt, lx, ly, rx, ry = snap
    return (btn == 0 and lt == 0 and rt == 0
            and abs(lx) <= _IDLE_AXIS and abs(ly) <= _IDLE_AXIS
            and abs(rx) <= _IDLE_AXIS and abs(ry) <= _IDLE_AXIS)


def find_physical(probe_s=0.8):
    """找出物理手柄所在的槽位。

    判据：连接中 + 包号在涨（>=20 包/s，排除"没被轮询的死设备"），
          然后在候选里取**刷新率最低**的那个。
    为什么取最低而不是卡一个绝对值区间：我们自己的虚拟设备被驱动高频轮询
    （实测 16k 包/s，物理约 0.9k），两者差 10 倍以上，"取最低"比"卡区间"稳，
    物理手柄被高频轮询时也不会误判。
    返回 (idx, rate) 或 (None, {}) 表示无法确定。
    """
    if _get_state is None:
        return None, {}
    first = {}
    for i in range(4):
        r = _read(i)
        if r:
            first[i] = r[0]
    if not first:
        return None, {}
    time.sleep(probe_s)
    info, best = {}, None
    for i, p0 in first.items():
        r = _read(i)
        if not r:
            continue
        rate = (r[0] - p0) / probe_s
        info[i] = round(rate, 1)
        if rate < 20.0:            # 没被轮询（或刚拔出）→ 不做候选
            continue
        if best is None or rate < info.get(best, 1e18):
            best = i
    return best, info


class Bridge:
    """常驻读线程：500Hz 拉物理手柄状态 → 无锁快照。"""

    def __init__(self, hz=500, idle_kick_ms=400, kick_lt=6, idle_after_ms=2500):
        self._snap = None            # (buttons, lt, rt, lx, ly, rx, ry)
        self._slot = None
        self._rate = {}
        self._stop = False
        self._hz = hz
        self._miss = 0
        self._t_scan = 0.0
        self._pending = None         # 重扫线程 → 主线程的单槽信箱（("set", idx)）
        # --- 活性保活（2026-11-21 实测：不保活引擎约 10 秒后停流）---
        self._idle_kick = max(0, int(idle_kick_ms)) / 1000.0
        self._kick_lt = max(0, min(255, int(kick_lt)))     # 保活方波的高电平扳机量
        # 松手后先等这么久再开始伪造扳机（引擎超时是 10.9s，留足余量）：
        # 太短会在"推着摇杆保持不动"时误触发，太长则可能赶不上引擎的超时窗口。
        self._idle_after = max(0, int(idle_after_ms)) / 1000.0
        self._prev_out = None        # 上次真正输出给设备的原始快照
        self._t_change = 0.0         # 上次快照变化（或上次保活脉冲）的时刻
        self._tick = 0               # 保活脉冲的相位 0/1
        self.stats = {"reads": 0, "fails": 0, "last_scan": 0.0, "kicks": 0}

    # ---- 对外 ----
    @property
    def active(self):
        return self._slot is not None and self._snap is not None

    def report(self):
        """新鲜快照 → 20B 报告；没有就返回 None（调用方回落假半扣）。

        ⚠ 必须保活：引擎盯着输入端点，约 **10.9 秒**（实测两次都精确停在 1359 帧
        = 10.87s）完全没有"像有人在操作"的变化，就直接停止出流（frames 归零、
        只剩 client connected 在刷），而且再也不会自己恢复。
        历史上的"假半扣 LT 每 0.5s 打一次"本质上就是这个保活。

        但保活内容有讲究（实测结论）：
          · 只抖摇杆 ±1 **不管用** —— 引擎不认"摇杆漂了 1 点"，照样 10.9s 停流；
          · 它认的是**扳机**。所以脉冲打在 LT 上，幅度取 6/255 ≈ 2.4%，
            低于大多数游戏的扳机死区（通常 15~30/255），不会误触发瞄准/射击；
          · 摇杆 ±1 一起留着（双保险，且对游戏无害）。
        静止时才打脉冲；用户一动，立刻原样输出真值。
        """
        if self._slot is None or self._snap is None:
            return None
        snap = self._snap
        now = time.time()
        if snap == self._prev_out:
            # 完全静止：按 idle_kick 打**方波**保活（高相位持续 idle_kick 那么久）。
            # ⚠ 这里踩过一次坑：脉冲不能只在"一次 report() 调用"里返回——
            # bulk_in 每秒被调上万次，单次 tick 的脉冲只存在约 0.08ms，
            # 引擎按 125Hz 采样根本读不到，等于没保活（实测照样 10.9s 停流）。
            # 假半扣能一直有效，正是因为它给的是 0.5s 高 / 0.5s 低的方波。
            if (self._idle_kick > 0
                    and (now - self._t_change) >= self._idle_after
                    and _is_idle(snap)):
                phase = int(now / self._idle_kick) % 2
                if phase:
                    if not self._tick:
                        self._tick = 1
                        self.stats["kicks"] += 1
                    btn, lt, rt, lx, ly, rx, ry = snap
                    if self._kick_lt > 0:
                        # 引擎认的是"扳机量 >= 40"，所以保活必须动 LT。
                        # 但**取最大值**而不是覆盖：用户按住 LT 不放时（snap 不变、
                        # 会走进保活分支），真值 200 不能被压成 40。
                        lt = max(lt, self._kick_lt)
                    rep = bytearray(to_report((btn, lt, rt, lx, ly, rx, ry)))
                    # 保留字节（14..19）：Xbox360 20B 报告里游戏不解析这几位，
                    # 但如果引擎是对整份报告做"有没有变化"的判断，这里动了就够了。
                    rep[14] = 0x01
                    return bytes(rep)
                if self._tick:
                    self._tick = 0
        else:
            self._prev_out = snap
            self._t_change = now
        return to_report(snap)

    def slot(self):
        return self._slot

    def start(self):
        threading.Thread(target=self._run, daemon=True, name="xin-bridge").start()
        # 槽位重扫必须独立成线程：find_physical 内部要 sleep 0.8s 才能算出包速，
        # 放在读循环里就是"每 5 秒冻 0.76 秒输入"（2026-11-21 实测复现，
        # 游戏里表现为摇杆每 5 秒僵一下）。
        threading.Thread(target=self._rescan_loop, daemon=True, name="xin-rescan").start()
        return self

    def stop(self):
        self._stop = True

    # ---- 内部 ----
    def _rescan_loop(self):
        """每 5 秒重扫一次槽位，结果投到 self._pending，从不阻塞读线程。"""
        while not self._stop:
            idx, info = find_physical()
            self._rate = info
            self.stats["last_scan"] = time.time()
            # 只有扫到明确结果才投递：一次探活失手（0.8s 样本里刚好没被轮询）
            # 不该把正在用的桥拆掉——真拔了的话读线程自己会连续 miss 到 100 次收手。
            if idx is not None:
                self._pending = ("set", idx)
            slept = 0.0
            while slept < 5.0 and not self._stop:
                time.sleep(0.1)
                slept += 0.1

    def _run(self):
        period = 1.0 / max(50, self._hz)
        while not self._stop:
            # 只消费重扫结果，不在读循环里做任何等待
            pend = self._pending
            if pend is not None:
                self._pending = None
                _, idx = pend
                if idx != self._slot:
                    self._slot = idx
                    self._miss = 0
                    if idx is None:
                        self._snap = None
            if self._slot is not None:
                r = _read(self._slot)
                self.stats["reads"] += 1
                if r is None:
                    self.stats["fails"] += 1
                    self._miss += 1
                    self._snap = None
                    if self._miss >= 100:      # 连续 100 次读不到 ≈ 拔了
                        self._slot = None
                        self._t_scan = 0.0
                else:
                    self._snap = r[1]
                    self._miss = 0
            time.sleep(period)


_INST = None
_LOCK = threading.Lock()


def start_if_enabled(cfg_get):
    """按配置启一次（cfg_get = 类似 CFG.get 的可调用）。返回实例或 None。"""
    global _INST
    if not cfg_get("input_bridge", 0):
        return None
    with _LOCK:
        if _INST is None:
            _INST = Bridge(hz=int(cfg_get("input_bridge_hz", 500) or 500),
                           idle_kick_ms=int(cfg_get("input_bridge_idle_ms", 400) or 400),
                           kick_lt=int(cfg_get("input_bridge_kick_lt", 6) or 0),
                           idle_after_ms=int(cfg_get("input_bridge_idle_after_ms", 2500) or 0))
            _INST.start()
        return _INST


def current_report(cfg_get):
    """bulk_in 每帧调用：有桥且新鲜就返回 20B，否则 None（调用方回落）。

    快路径不加锁：bulk_in 被驱动高频轮询（实测 6000+/s），
    每次都抢锁会让 urb 线程白等——这里只在"还没建过"时才走加锁分支。
    """
    inst = _INST
    if inst is None:
        inst = start_if_enabled(cfg_get)
    return inst.report() if inst is not None else None
