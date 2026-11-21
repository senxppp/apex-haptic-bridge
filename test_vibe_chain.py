#!/usr/bin/env python3
"""震动链路离线回归（零硬件 / 零引擎 / 零真实 HID 写）。

以 backup-20261114-2350/usbip_apex6.py（本次修复前版本）为对照，验证：
  1. 正常路径输出字节完全一致 —— 修复没有引入回归
  2. 写进程重启后震动/扳机能恢复 —— 旧版会命中去重导致永久不震
  3. TTL 兜底 —— 状态失步最多 1s/3s 自动重发，且 TTL 内仍有效去重（写入率不升高）
  4. 0x51 状态帧写失败回队重发；0x12 电平帧不回队（不抬升 dongle 写入率）
  5. DPC 节流相位 —— 修复版投喂率回到 12.5/s，旧版实测约 22/s（对照实证）
  6. 慢写保护 / 分类累计计数 / 三种不震判层函数

对照基准两份，随源码包一起交付（也兼容本机 backup 路径），用途不同：
  * ref_apex6/regression/usbip_apex6-old.py  —— 最初版本，「永久不震」bug 与帧字节零回归
  * ref_apex6/regression/usbip_apex6-prev.py —— 上一轮交付版本，节流相位回归实证
两份都请勿删除。

直接运行：python -I test_vibe_chain.py
"""
import importlib.util
import os
import sys
import threading
import time

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import usbip_apex6 as new

REG = os.path.join(HERE, "ref_apex6", "regression")
# 最初版本：「永久不震」bug 与帧字节零回归的对照基准
OLD_PATH = os.path.join(REG, "usbip_apex6-old.py")
if not os.path.isfile(OLD_PATH):
    OLD_PATH = os.path.join(HERE, "backup-20261114-2350", "usbip_apex6.py")
# 上一轮交付版本（TTL/回队修复后、节流相位修复前）：节流回归的对照基准
PREV_PATH = os.path.join(REG, "usbip_apex6-prev.py")
if not os.path.isfile(PREV_PATH):
    PREV_PATH = os.path.join(HERE, "backup-20261115-0005", "usbip_apex6-prev.py")


def load_mod(path, name):
    if not os.path.isfile(path):
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_old():
    return load_mod(OLD_PATH, "usbip_apex6_old")


def load_prev():
    return load_mod(PREV_PATH, "usbip_apex6_prev")


class FakeStdin:
    def __init__(self):
        self.frames = []

    def write(self, b):
        self.frames.append(bytes(b))
        return len(b)

    def flush(self):
        pass


class FakeProc:
    """替身写进程：不触碰真实 hid 设备"""
    def __init__(self):
        self.stdin = FakeStdin()
        self._dead = False

    def poll(self):
        return 1 if self._dead else None

    def kill(self):
        self._dead = True

    def wait(self, timeout=None):
        pass


new.subprocess.Popen = lambda *a, **kw: FakeProc()

BASE_ATTRS = dict(
    dev=None, _proc=None, _last=None, _last_nz=(0.0, 0, 0), writes=0,
    boosted_max=0, _pend=None, _trig_q=[], _last_trig=None, _trig_mode=1,
    _fail_streak=0, _last_send=0.0, _level_now=(0, 0), _k5_min_iv=0.08,
    _next_send_at=0.0, _dedup_hits=0, _wq=0, _wfail=0, _throt=0,
    _sloww=0, _phase=0,
)
NEW_EXTRA = dict(_last_sent_at=0.0, _last_trig_at=0.0)


def make(mod, extra=None):
    """绕过 __init__（它会起真实写进程和后台线程），手工装配最小 K5 实例"""
    k = object.__new__(mod.K5)
    for key, val in BASE_ATTRS.items():
        setattr(k, key, val)
    k._pend_lock = threading.Lock()
    k._trig_q = []
    k._last_rumble_ts = time.time()
    for key, val in (extra or {}).items():
        setattr(k, key, val)
    k._proc = FakeProc()
    return k


def rumble_frame(mod, low, high, extra=None):
    k = make(mod, extra)
    k.rumble(low, high)
    return list(k._pend) if k._pend else None


def trig_frames(mod, extra=None):
    k = make(mod, extra)
    k.trigger(1, 2, 0, 1, 130, 50)
    return [bytes(f) for f in k._trig_q]


def main():
    results = []
    skips = []

    def check(label, cond, detail=""):
        results.append((label, bool(cond), detail))
        print("%s %-58s %s" % ("[PASS]" if cond else "[FAIL]", label, detail))

    def skip(label, why="缺对照基准"):
        # 显式跳过，而不是写成 `old is None or ...` 那样恒真。
        # 恒真断言会打印 [PASS]，等于把"没验证"伪装成"验证过"——这是本项目
        # 最不能接受的一类假绿（2026-11-15 代码审计第 2 条）。
        skips.append((label, why))
        print("%s %-58s %s" % ("[SKIP]", label, why))

    old = load_old()
    body = bytes(v + 128 for v in (30, 60, 90, -30, -60, -90, 12, 24,
                                   36, 48, 60, 72, 84, 96, 108, 120,
                                   8, 16, 24, 32, 40, 48, 56, 64))

    # --- 1. 正常路径零回归（与最初版本逐字节对照） ---
    new_t = trig_frames(new, NEW_EXTRA)
    if old is None:
        skip("decode_57 输出与修复前一致")
        skip("0x12 转子帧与修复前字节一致")
        skip("0x51 扳机帧与修复前字节一致")
    else:
        check("decode_57 输出与修复前一致",
              old.decode_57(body) == new.decode_57(body),
              str(new.decode_57(body)))
        new_r = rumble_frame(new, 120, 80, NEW_EXTRA)
        check("0x12 转子帧与修复前字节一致",
              rumble_frame(old, 120, 80) == new_r, str(new_r))
        check("0x51 扳机帧与修复前字节一致",
              trig_frames(old) == new_t, "%d 帧" % len(new_t))
    check("0x51 帧校验和自洽",
          bool(new_t) and sum(new_t[0][2:31]) & 0xFF == new_t[0][31],
          "sum=%d ck=%d" % (sum(new_t[0][2:31]) & 0xFF, new_t[0][31]) if new_t else "")

    # --- 2. 写进程重启后自愈（旧版复现 bug） ---
    k = make(new, NEW_EXTRA)
    k.rumble(120, 80)
    first = list(k._pend)
    k._pend = None
    k._spawn_writer()          # 模拟 k5_writer 重连：子进程清转子 + 双扳机设常态
    k.rumble(120, 80)
    check("重启后同值震动重新入队（修复）", k._pend == first, str(k._pend))
    check("重启后 _trig_mode 归常态 1", k._trig_mode == 1)

    if old is not None:
        o = make(old)
        o.rumble(120, 80)
        o._pend = None
        o._spawn_writer()
        o.rumble(120, 80)
        check("旧版确实复现「永久不震」", o._pend is None,
              "旧版重启后 _pend=%r" % (o._pend,))

    kt = make(new, NEW_EXTRA)
    kt.trigger(1, 2, 0, 1, 130, 50)
    n1 = len(kt._trig_q)
    kt._trig_q.clear()
    kt._spawn_writer()
    kt.trigger(1, 2, 0, 1, 130, 50)
    check("重启后扳机状态帧重新入队", len(kt._trig_q) == n1 and n1 > 0,
          "%d 帧" % len(kt._trig_q))

    # --- 3. TTL 兜底 + 去重仍有效 ---
    k = make(new, NEW_EXTRA)
    k.rumble(120, 80)
    k._pend = None
    k.rumble(120, 80)
    check("TTL 内仍去重（写入率不升高）", k._pend is None)
    k._last_sent_at -= new.K5._RUMBLE_TTL + 0.1
    k.rumble(120, 80)
    check("TTL 到期强制重发（状态失步自愈）", k._pend == first)

    k = make(new, NEW_EXTRA)
    k.trigger(1, 2, 0, 1, 130, 50)
    k._trig_q.clear()
    k.trigger(1, 2, 0, 1, 130, 50)
    check("扳机 TTL 内仍去重", not k._trig_q)
    k._last_trig_at -= new.K5._TRIG_TTL + 0.1
    k.trigger(1, 2, 0, 1, 130, 50)
    check("扳机 TTL 到期强制重发", len(k._trig_q) > 0)

    # --- 4. 写失败不丢 0x51 状态帧 ---
    # 写线程是「弹出 → 等令牌 → 发送 → 失败回队」的循环，帧在队里的可见性有窗口：
    # 直接 sleep 固定时长再断言会撞上"帧正在飞行中"的那一瞬（本用例 2026-11-15
    # 就因此假失败过）。改成轮询到条件成立，最多等 1 秒。
    def wait_until(pred, timeout=1.0, step=0.005):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if pred():
                return True
            time.sleep(step)
        return False

    k = make(new, NEW_EXTRA)
    k._k5_send = lambda pend: 0
    tf = bytes(new.k5_force_trigger(1, 2, 0, 1, 130, 50))
    k._trig_q.append(tf)
    threading.Thread(target=k._writer, daemon=True).start()
    ok_back = wait_until(lambda: bool(k._trig_q) and bytes(k._trig_q[0]) == tf)
    check("0x51 写失败回队重发", ok_back,
          "队列 %d 失败 %d" % (len(k._trig_q), k._wfail))

    k2 = make(new, NEW_EXTRA)
    k2._k5_send = lambda pend: 0
    k2._pend = [0x03, 0x5A, 0xA5, 0x12, 0x04, 100, 100]
    threading.Thread(target=k2._writer, daemon=True).start()
    time.sleep(0.12)
    check("0x12 写失败不回队（不抬升写入率）", not k2._trig_q,
          "队列 %d" % len(k2._trig_q))

    # --- 5. DPC 节流相位：离线实测投喂率，与修复前版本对照 ---
    def measure_rate(mod, seconds=0.6):
        k = make(mod, NEW_EXTRA)
        stop = [False]

        def feed():
            while not stop[0]:
                with k._pend_lock:
                    k._pend = [0x03, 0x5A, 0xA5, 0x12, 0x04, 100, 100]
                time.sleep(0.004)

        threading.Thread(target=feed, daemon=True).start()
        threading.Thread(target=k._writer, daemon=True).start()
        t0 = time.time()
        time.sleep(seconds)
        elapsed = time.time() - t0
        stop[0] = True
        return k, (k.writes / elapsed if elapsed else 0)

    # 上限跟着配置走（1/k5_min_iv），不再写死数字：21 号把 min_iv 从 0.08 放回 0.04
    # （7 写/s 也断联 → 断联与写入率无因果；12.5 反而把包络切太粗，手感变差）。
    _iv = float(new.CFG.get("k5_min_iv", 0.08))
    _cap = 1.0 / _iv
    _lim = _cap + 1.5          # 1 格借用的余量 + 计时抖动
    kn, rate_n = measure_rate(new)
    check("节流后投喂率 <= %.1f 写/s（设计上限 %.1f）" % (_lim, _cap), rate_n <= _lim,
          "%.1f 写/s" % rate_n)
    check("节流与投喂分类计数递增", kn._throt > 0 and kn._wq > 0,
          "节流=%d 投喂=%d 相位重置=%d" % (kn._throt, kn._wq, kn._phase))

    # --- 5b. 有声场景：抢发不得绕过预算 ---
    # 旧实现的 allow_burst 直接跳过令牌检查、只受 hard_iv(28ms) 约束，音频一响
    # 就等价于 1/0.028 ≈ 35 写/s —— 界面那张"12.5 预算"表长期是装饰
    # （2026-11-21 探针实测：有声平均 27.6 写/s、峰值 40 写/s）。
    # 这里每 4ms 换一次大跳变，旧代码 100% 走抢发路径，新代码必须仍被限住。
    def measure_rate_varying(mod, seconds=1.0, period=0.004):
        k = make(mod, NEW_EXTRA)
        k._last = None
        stop = [False]
        lv = [60]

        def feed():
            while not stop[0]:
                lv[0] = 200 if lv[0] < 120 else 60
                with k._pend_lock:
                    k._pend = [0x03, 0x5A, 0xA5, 0x12, 0x04, lv[0], lv[0]]
                time.sleep(period)

        threading.Thread(target=feed, daemon=True).start()
        threading.Thread(target=k._writer, daemon=True).start()
        t0 = time.time()
        time.sleep(seconds)
        elapsed = time.time() - t0
        stop[0] = True
        return k, (k.writes / elapsed if elapsed else 0)

    kv, rate_v = measure_rate_varying(new)
    check("大跳变连发时投喂率仍 <= %.1f 写/s（抢发不得绕过预算）" % _lim, rate_v <= _lim,
          "%.1f 写/s" % rate_v)

    # --- 5c. 需求整形：小抖动不占预算，起手不能误伤 ---
    # 死区作用在"映射/增益之后"的电平上，这里先把增益切到生产配置（motor_config.json
    # 里 k5_rotor_gain=1），否则旧两级增益会把 105 顶成 255，判据无从验证。
    _rg_save = new.CFG.get("k5_rotor_gain")
    new.CFG["k5_rotor_gain"] = 1
    kk = make(new, NEW_EXTRA)
    nq = 0
    for i in range(200):                      # 200 帧，电平只在 100±1 抖
        kk.rumble(100 + (i % 3) - 1, 100)
        if kk._pend:
            nq += 1
            kk._pend = None
    check("±1 点抖动被死区合并（200 帧最多入队 1 次）", nq <= 1, "入队 %d 次" % nq)

    ka = make(new, NEW_EXTRA)
    ka._last = (100, 100)
    ka._last_sent_at = time.time()
    ka.rumble(112, 100)
    check("起手 +12 不被下降沿大死区误伤", ka._pend is not None,
          "pend=%r" % (ka._pend,))

    kr = make(new, NEW_EXTRA)
    kr._last = (120, 120)
    kr._last_sent_at = time.time()
    kr.rumble(105, 120)
    check("缓降 -15 被下降沿死区合并", kr._pend is None, "pend=%r" % (kr._pend,))

    kb = make(new, NEW_EXTRA)
    kb._last = (200, 200)
    kb._last_sent_at = time.time()
    kb.rumble(60, 200)
    check("断崖下降 -140 不受死区约束（必须立刻发）", kb._pend is not None,
          "pend=%r" % (kb._pend,))

    # 连击重打：第二下峰值 238 比第一下 245 低 7 点，但包络刚从 120 冲上来。
    # 按"比上次写出的值高"判上升沿会被大死区吃掉 → 转子不重新起转 → 连击糊成一片。
    kh = make(new, NEW_EXTRA)
    kh._last = (245, 245)         # 上一拍已写出
    kh._prev_lv = (120, 120)      # 这一帧包络在涨
    kh._last_sent_at = time.time()
    kh.rumble(238, 238)
    check("连击重打（峰值略低但包络在涨）不被合并", kh._pend is not None,
          "pend=%r" % (kh._pend,))

    kz = make(new, NEW_EXTRA)
    kz._last = (245, 245)
    kz._prev_lv = (240, 240)      # 包络在跌
    kz._last_sent_at = time.time()
    kz.rumble(238, 238)
    check("包络在跌时的 -7 点抖动被合并", kz._pend is None, "pend=%r" % (kz._pend,))

    # 低音量保活：电平长期稳定在 floor 附近时，死区会把所有帧合并掉 → 只剩 1s 的 TTL，
    # 转子就停了（21 号实机反馈"低音量没有震动"）。必须按 k5_refresh_ms 重发同一电平。
    kp = make(new, NEW_EXTRA)
    kp._last = (60, 60)
    kp._last_sent_at = time.time() - 0.30
    kp.rumble(60, 60)
    check("电平不变但超过 refresh_ms → 保活重发", kp._pend is not None,
          "pend=%r" % (kp._pend,))

    kq = make(new, NEW_EXTRA)
    kq._last = (60, 60)
    kq._last_sent_at = time.time() - 0.03
    kq.rumble(60, 60)
    check("refresh_ms 窗口内不重复喂", kq._pend is None, "pend=%r" % (kq._pend,))

    # 零电平不进保活：静音时不该每 120ms 喂一个 0（只保留 1s 的 TTL 兜底）
    k0 = make(new, NEW_EXTRA)
    k0._last = (0, 0)
    k0._last_sent_at = time.time() - 0.30
    k0.rumble(0, 0)
    check("静音不按 refresh_ms 喂零", k0._pend is None, "pend=%r" % (k0._pend,))
    new.CFG["k5_rotor_gain"] = _rg_save

    # --- 6. 慢写保护：管道背压时额外拉开一个周期 ---
    ks = make(new, NEW_EXTRA)

    def slow_send(pend):
        time.sleep(0.012)
        return len(pend)

    ks._k5_send = slow_send
    ks._pend = [0x03, 0x5A, 0xA5, 0x12, 0x04, 100, 100]
    threading.Thread(target=ks._writer, daemon=True).start()
    time.sleep(0.12)
    check("慢写被识别并额外拉开一个周期", ks._sloww > 0,
          "sloww=%d" % ks._sloww)

    # --- 7. 三种「不震」判层函数 ---
    check("判层：音源未点火",
          new.flow_verdict(0, 0, 0, 0) == "引擎有帧但全零(音源未点火)")
    check("判层：第 3 层 dongle 写失败", "第3层" in new.flow_verdict(5, 0, 3, 0))
    check("判层：第 2 层全被去重挡下", "第2层" in new.flow_verdict(5, 0, 0, 9))
    check("判层：正常转发", new.flow_verdict(5, 7, 0, 9) == "正常转发")
    # 2026-11-20 实机：残留 mute.flag 让"有信号不转发"被报成「无有效帧(检查引擎)」，
    # 排查方向被带偏两轮。静音必须是独立判层，且优先级最高。
    check("判层：静音开关优先于其它层",
          new.flow_verdict(5, 0, 0, 0, muted=True) == "静音开关开启(mute.flag)",
          new.flow_verdict(5, 0, 0, 0, muted=True))
    snap = dict(n=10, nz=6, ml=120, mr=90, kw=8)
    line = new.stats_line(snap, "正常转发",
                          dict(_wq=7, _wfail=0, _dedup_hits=9, _throt=7,
                               _sloww=0, _phase=1), 1.2)
    check("stats.log 分类行格式可渲染（参数对齐）",
          "判层=正常转发" in line and "投喂=7" in line and "静默=1.2s" in line,
          line[-88:])

    # --- 8. 去重命中计入分类统计 ---
    kd = make(new, NEW_EXTRA)
    kd.rumble(120, 80)
    kd._pend = None
    kd.rumble(120, 80)
    check("去重命中被计入分类统计", kd._dedup_hits == 1,
          "dedup=%d" % kd._dedup_hits)

    # --- 9. 输入桥：XINPUT_GAMEPAD → 20B Xbox360 报告的字段位置 ---
    # 这段是纯函数，钉住字节位置就等于钉住了"引擎/游戏看到的输入"，
    # 比"跑起来看起来能动"可靠（后者在没人碰手柄时永远是 0，验不出错位）。
    import xinput_bridge as xb
    r0 = xb.to_report((0x0000, 0, 0, 0, 0, 0, 0))
    check("零状态报告 = 00 14 后全零（共 20B）",
          r0[:2] == b"\x00\x14" and r0[2:] == bytes(18) and len(r0) == 20,
          r0.hex())

    rb = xb.to_report((0x1234, 200, 100, 0, 0, 0, 0))
    check("按键低/高字节在 [2]/[3]（小端）",
          rb[2] == 0x34 and rb[3] == 0x12 and rb[4] == 200 and rb[5] == 100,
          rb[:6].hex())

    rl = xb.to_report((0, 0, 0, -32768, 32767, -1, 40000))
    check("摇杆有符号小端：LX=-32768 → 00 80", rl[6] == 0x00 and rl[7] == 0x80,
          rl[6:8].hex())
    check("摇杆有符号小端：LY=32767 → FF 7F", rl[8] == 0xFF and rl[9] == 0x7F,
          rl[8:10].hex())
    check("摇杆有符号小端：RX=-1 → FF FF", rl[10] == 0xFF and rl[11] == 0xFF,
          rl[10:12].hex())
    check("摇杆越界被夹到 32767：RY=40000 → FF 7F", rl[12] == 0xFF and rl[13] == 0x7F,
          rl[12:14].hex())

    rc = xb.to_report((0, 300, -5, 0, 0, 0, 0))
    check("扳机越界夹到 0..255", rc[4] == 255 and rc[5] == 0, rc[4:6].hex())

    check("桥未启用时 current_report 返回 None（回落假半扣）",
          xb.current_report(lambda k, d=0: 0) is None, "None")

    # --- 9b. 槽位重扫不得阻塞读线程 ---
    # 2026-11-21 实测事故：find_physical 内部要 sleep 0.8s 才能算出包速，
    # 被放在读循环里 → 每 5 秒冻结输入 760ms（游戏里摇杆每 5 秒僵一下）。
    # 这条用例把"重扫必须在独立线程"钉死：让 find_physical 故意慢 0.6s，
    # 读次数仍须照常增长。改回内联重扫时这里会立刻掉到 0。
    import time as _t
    _orig_find = xb.find_physical

    def _slow_find(probe_s=0.8):
        _t.sleep(0.6)
        return 0, {0: 100.0}

    xb.find_physical = _slow_find
    try:
        bb = xb.Bridge(hz=200).start()
        _t.sleep(1.5)                      # 首扫 0.6s 完成后应已接管
        _n0 = bb.stats["reads"]
        _t.sleep(0.8)                      # 这段里重扫线程不一定在跑，只验读线程不受影响
        _n1 = bb.stats["reads"]
        bb.stop()
    finally:
        xb.find_physical = _orig_find
    check("槽位重扫不阻塞读线程（0.8s 内读次数仍增长 >50）", _n1 - _n0 > 50,
          "%d → %d" % (_n0, _n1))

    # --- 9c. 冷启动闸门：引擎还没开始发 0x57 帧之前，不得接管真输入 ---
    # 2026-11-21 事故：冷启动时桥立刻接管真输入，而"没人碰手柄"的真输入是一份
    # 恒定全零的报告；引擎 attach 时看到这个就不进流模式，frames 从此为 0。
    # 热切换没事（引擎已在流）。所以闸门必须是"先假半扣，看到引擎发帧再接管"。
    _orig_cr = xb.current_report
    xb.current_report = lambda cfg: b"\x00\x14" + bytes(18)
    _save_seen = new._ENGINE_SEEN
    _save_n = new._STATS["n"]
    try:
        obj = object.__new__(new.Apex6Virtual)      # 只测方法，不建整套设备
        new._ENGINE_SEEN = False
        new._STATS["n"] = 0
        check("引擎未发帧时桥不接管（回落假半扣）", obj._bridge_report() is None,
              "None")
        new._STATS["n"] = 51
        _got = obj._bridge_report()
        check("引擎开始发帧后桥接管真输入", _got == b"\x00\x14" + bytes(18),
              (_got or b"").hex())
        new._STATS["n"] = 0
        _still = obj._bridge_report()
        check("接管后即使帧计数回退也不回退（防振荡）",
              _still == b"\x00\x14" + bytes(18), (_still or b"").hex())
    finally:
        xb.current_report = _orig_cr
        new._ENGINE_SEEN = _save_seen
        new._STATS["n"] = _save_n

    # --- 9d. 静止保活：引擎约 10 秒收不到输入变化就停流，桥必须自己打脉冲 ---
    _b = xb.Bridge(hz=500, idle_kick_ms=250, kick_lt=40, idle_after_ms=0)
    _b._slot = 0
    _b._snap = (0x0000, 0, 0, 100, -50, 0, 0)      # 轻微漂移，仍算松手
    _lxs, _t0 = set(), _t.time()
    while _t.time() - _t0 < 0.9:
        _r = _b.report()
        _lxs.add(int.from_bytes(_r[6:8], "little", signed=True))
        _t.sleep(0.01)
    # 注意：早期版本用 LX±1 做保活，实测**无效**（引擎只认扳机），已删掉这两条断言。
    _rr = _b.report()
    check("保活不动其它字段（LY 保持 -50）",
          int.from_bytes(_rr[8:10], "little", signed=True) == -50, _rr[8:10].hex())

    # 保活必须真的动扳机：引擎认的是 LT>=40，摇杆±1 / 保留字节 / LT=6 都不认
    # （2026-11-21 四次冷启动实测：1359/1362/1347 帧后停流，只有 kick_lt=40 能维持）
    _b2 = xb.Bridge(hz=500, idle_kick_ms=250, kick_lt=40, idle_after_ms=0)
    _b2._slot = 0
    _b2._snap = (0, 0, 0, 0, 0, 0, 0)
    _lts, _t0 = set(), _t.time()
    while _t.time() - _t0 < 0.9:
        _lts.add(_b2.report()[4])
        _t.sleep(0.01)
    check("保活方波把 LT 顶到 kick_lt（引擎只认这个）", _lts == {0, 40}, "LT 取值 %s" % sorted(_lts))

    _b3 = xb.Bridge(hz=500, idle_kick_ms=250, kick_lt=40, idle_after_ms=0)
    _b3._slot = 0
    _b3._snap = (0, 200, 0, 0, 0, 0, 0)      # 用户按住 LT 不放
    _lts3, _t0 = set(), _t.time()
    while _t.time() - _t0 < 0.9:
        _lts3.add(_b3.report()[4])
        _t.sleep(0.01)
    check("按住 LT 时保活取最大值，不覆盖真值", _lts3 == {200}, "LT 取值 %s" % sorted(_lts3))

    # 玩家只要还在操作，就绝不能伪造扳机——否则推着摇杆保持不动也会举枪
    # （2026-11-21 实测：一轮采样里 slot1 有 1437 行 LT=40，物理只有 2 行）
    for _label, _snap in (("推着摇杆不动", (0, 0, 0, 18000, 0, 0, 0)),
                          ("按着 A 键不动", (0x1000, 0, 0, 0, 0, 0, 0)),
                          ("按住 RT=255", (0, 0, 255, 0, 0, 0, 0))):
        _bx = xb.Bridge(hz=500, idle_kick_ms=250, kick_lt=40, idle_after_ms=0)
        _bx._slot = 0
        _bx._snap = _snap
        _seen, _t0 = set(), _t.time()
        while _t.time() - _t0 < 0.6:
            _seen.add(_bx.report()[4])
            _t.sleep(0.01)
        check("玩家在操作时不伪造扳机（%s）" % _label, 40 not in _seen,
              "LT 取值 %s" % sorted(_seen))

    bad = [r for r in results if not r[1]]
    print("\n=== %d/%d PASS%s ===" % (len(results) - len(bad), len(results),
                                     ("，%d SKIP" % len(skips)) if skips else ""))
    if skips:
        print("以下用例需要对照基准源码（未随仓库发布），本次未执行——不计入 PASS：")
        for label, why in skips:
            print("  - %s（%s）" % (label, why))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
