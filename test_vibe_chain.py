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

    def check(label, cond, detail=""):
        results.append((label, bool(cond), detail))
        print("%s %-58s %s" % ("[PASS]" if cond else "[FAIL]", label, detail))

    old = load_old()
    body = bytes(v + 128 for v in (30, 60, 90, -30, -60, -90, 12, 24,
                                   36, 48, 60, 72, 84, 96, 108, 120,
                                   8, 16, 24, 32, 40, 48, 56, 64))

    # --- 1. 正常路径零回归 ---
    check("decode_57 输出与修复前一致",
          old is None or old.decode_57(body) == new.decode_57(body),
          str(new.decode_57(body)))
    new_r = rumble_frame(new, 120, 80, NEW_EXTRA)
    check("0x12 转子帧与修复前字节一致",
          old is None or rumble_frame(old, 120, 80) == new_r,
          str(new_r))
    new_t = trig_frames(new, NEW_EXTRA)
    check("0x51 扳机帧与修复前字节一致",
          old is None or trig_frames(old) == new_t,
          "%d 帧" % len(new_t))
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
    k = make(new, NEW_EXTRA)
    k._k5_send = lambda pend: 0
    tf = bytes(new.k5_force_trigger(1, 2, 0, 1, 130, 50))
    k._trig_q.append(tf)
    threading.Thread(target=k._writer, daemon=True).start()
    time.sleep(0.15)
    check("0x51 写失败回队重发", k._trig_q and bytes(k._trig_q[0]) == tf,
          "队列 %d" % len(k._trig_q))

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

    kn, rate_n = measure_rate(new)
    check("节流后投喂率 <= 13.5 写/s（设计上限 12.5）", rate_n <= 13.5,
          "%.1f 写/s" % rate_n)
    check("节流与投喂分类计数递增", kn._throt > 0 and kn._wq > 0,
          "节流=%d 投喂=%d 相位重置=%d" % (kn._throt, kn._wq, kn._phase))
    prev = load_prev()
    if prev is not None:
        ko, rate_o = measure_rate(prev)
        check("上一轮版本确实超速率（节流相位回归对照实证）", rate_o > rate_n * 1.3,
              "上一轮 %.1f vs 本轮 %.1f 写/s" % (rate_o, rate_n))

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

    bad = [r for r in results if not r[1]]
    print("\n=== %d/%d PASS ===" % (len(results) - len(bad), len(results)))
    if old is None:
        print("注意：未找到最初版本，跳过字节级零回归对照")
    if prev is None:
        print("注意：未找到上一轮版本，跳过节流相位回归对照")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
