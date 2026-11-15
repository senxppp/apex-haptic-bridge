# test_trig_wire.py — 「0x57 帧 → 扳机」接线回归（不启动引擎、不碰设备）
#
# 为什么需要它：12:30 实机踩到的 bug 是**接线**问题，不是算法问题——
# 渲染器刚把 mode 2 设上，on_out 里旧的"爆发到时回落"分支下一秒就把它改回 mode 1，
# 结果扳机只剩一串 m1 帧、完全不抖。单元测试测不到这种相互覆盖，必须喂真帧。
#
# 直接运行：python -I test_trig_wire.py
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import usbip_apex6 as U

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                       ("  " + str(detail)) if detail else ""))


class FakeK5:
    """记录所有扳机/转子调用，供断言。"""

    def __init__(self):
        self._trig_mode = 1
        self._trig_burst_ts = 0.0
        self._last_nz = (0.0, 0, 0)
        self._last_rumble_ts = 0.0
        self._tr_last = 0.0
        self._tr = U.trig_rattle.Rattle(lambda: U.CFG) if U.trig_rattle else None
        self._trg = U.trig_rattle.Gate(lambda: U.CFG) if U.trig_rattle else None
        self.calls = []
        self.rumble_calls = []
        self.writes = 0

    def set_rattle(self, p2, p3):
        self._trig_mode = 2
        self.calls.append(("rattle", p2, p3))

    def trigger(self, side, mode, start=0, p1=1, p2=1, p3=1, match=0):
        self._trig_mode = mode
        self.calls.append(("trigger", mode, p2, p3))

    def trigger_rest(self):
        self._trig_mode = 1
        self.calls.append(("rest",))

    def set_damp(self, force, now=None):
        self.calls.append(("damp", force))

    def rumble(self, lo, hi):
        self.rumble_calls.append((lo, hi))


def frame57(levels):
    """构造一帧 0x57：24 字节 = 8 子帧 × (t, l, r)，线上编码 i8+128。"""
    body = bytearray()
    for i in range(8):
        t = levels[i] if i < len(levels) else 0
        body += bytes([max(0, min(255, t + 128)), 128, 128])
    f = bytearray(32)
    f[0], f[1], f[2], f[3], f[4] = 0x5A, 0xA5, 0x57, len(body), 0x01
    f[5:5 + len(body)] = body
    return bytes(f)


def feed(v, levels, n=1):
    for _ in range(n):
        v.on_out(frame57(levels))


def main():
    if U.trig_rattle is None:
        print("trig_rattle 不可用（sys.path 问题），跳过")
        return 1

    U.CFG["tr_enable"] = 1
    U.CFG["tf_enable"] = 0
    U.CFG["trig_burst"] = 1

    k = FakeK5()
    v = U.Apex6Virtual(k5=k)

    # 1) 静音：不该有任何扳机写入
    feed(v, [0] * 8, 5)
    check("静音不下发扳机帧", k.calls == [], k.calls)

    # 2) 一次打击：应出现 rattle(200, 高频)，且**不被旧的回落分支立刻改回 m1**
    feed(v, [120] * 8, 1)
    rattles = [c for c in k.calls if c[0] == "rattle"]
    check("打击触发 mode2 抖动", len(rattles) >= 1, rattles[:3])
    check("头冲强度到顶（200）", rattles and rattles[0][1] == 200, rattles[:1])
    n_rest = len([c for c in k.calls if c[0] == "rest"])
    check("打击后没有立刻被改回 mode1", n_rest == 0, k.calls)

    # 3) 持续音：应继续维持 mode2（不许掉回 m1）
    k.calls.clear()
    feed(v, [90] * 8, 10)
    check("持续音期间保持 mode2", all(c[0] != "rest" for c in k.calls), k.calls[:4])

    # 4) 静音足够久：必须回落 mode1
    k.calls.clear()
    import time as _t
    t0 = _t.time()
    while _t.time() - t0 < 0.5:
        v.on_out(frame57([0] * 8))
        _t.sleep(0.01)
    check("长时间静音后回落常态", any(c[0] == "rest" for c in k.calls), k.calls[:4])

    # 5) 转子通道照常工作（别为了扳机把握把弄丢）
    check("转子帧仍在转发", len(k.rumble_calls) > 0, len(k.rumble_calls))

    # 6) 旧爆发层在 tr_enable=1 时不再触发
    k.calls.clear()
    feed(v, [200] * 8, 3)
    legacy = [c for c in k.calls if c[0] == "trigger"]
    check("旧 mode2 爆发已让位", legacy == [], legacy)

    bad = [r for r in RESULTS if not r[1]]
    print("\n=== %d/%d PASS ===" % (len(RESULTS) - len(bad), len(RESULTS)))
    for r in bad:
        print("  FAIL:", r[0], r[2])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
