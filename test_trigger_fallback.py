# test_trigger_fallback.py — 扳机爆发回落兜底的离线回归
#
# 缺陷：0x51 是状态设定命令，mode 2 鼓点打击在 dongle 端保持；回落原本只由
# 0x57 帧处理驱动，引擎静默期挂起、0x57 停发后扳机永久停在爆发态。
# 本测试验证 _writer 里的独立超时兜底：不依赖上游帧也能回落，且不破坏
# 「常态 mode 1 阻力一直在」和「音乐持续时不误回落」。
#
# 直接运行：python -I test_trigger_fallback.py
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
import usbip_apex6 as m

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                       ("  " + str(detail)) if detail else ""))


class FakeProc:
    def poll(self):
        return None
    def kill(self):
        pass
    def wait(self, timeout=None):
        pass


def make(mod):
    k = object.__new__(mod.K5)
    attrs = dict(
        dev=None, _proc=FakeProc(), _last=None, _last_nz=(0.0, 0, 0),
        writes=0, boosted_max=0, _pend=None, _trig_q=[],
        _last_trig=None, _trig_at=0.0, _last_trig_at=0.0,
        _trig_burst_ts=0.0, _trig_mode=1, _fail_streak=0,
        _last_send=0.0, _next_send_at=0.0, _level_now=(0, 0),
        _k5_min_iv=0.08, _last_sent_at=0.0,
        _last_rumble_ts=time.time(),
        _dedup_hits=0, _wq=0, _wfail=0, _throt=0, _sloww=0, _phase=0)
    for a, v in attrs.items():
        setattr(k, a, v)
    k._pend_lock = threading.Lock()
    sent = []
    k._k5_send = lambda p: (sent.append(bytes(p)), len(p))[1]
    k._spawn_writer = lambda: None
    return k, sent


def run_writer(k, seconds=0.25):
    t = threading.Thread(target=k._writer, daemon=True)
    t.start()
    time.sleep(seconds)
    return t


def main():
    m.CFG["rt_mode"] = "idle"
    m.CFG["trig_burst_ms"] = 180

    # 1. 爆发命令本身
    k, sent = make(m)
    k.trigger(1, 2, 0, 1, 130, 50)
    check("爆发命令入队且 _trig_mode=2",
          k._trig_mode == 2 and len(k._trig_q) == 1, (k._trig_mode, len(k._trig_q)))
    check("爆发时刻已记录", k._trig_burst_ts > 0, k._trig_burst_ts)

    # 2. 上游流断 → 独立回落（不调用任何 0x57 / rumble）
    k._last_rumble_ts = time.time() - 3.0
    run_writer(k, 0.3)
    check("流断后自动回落到 mode 1", k._trig_mode == 1, k._trig_mode)
    check("回落时刻标记已清零", k._trig_burst_ts == 0.0, k._trig_burst_ts)
    fallbacks = [f for f in sent if len(f) > 7 and f[3] == 0x51 and f[7] == 1]
    check("确实发出 0x51 mode1 回落帧", len(fallbacks) >= 1,
          [f[:9].hex() for f in fallbacks])
    check("残留的 mode2 爆发帧已被清除，不会回落后再被打回爆发态",
          not any(len(f) > 7 and f[3] == 0x51 and f[7] == 2 for f in sent),
          [f[:9].hex() for f in sent if len(f) > 7 and f[3] == 0x51])
    if fallbacks:
        f = fallbacks[0]
        check("回落帧校验和自洽", sum(f[2:-1]) & 0xFF == f[-1], f[-1])
        check("回落帧 side=左扳机", f[6] == 1, f[6])
        # 关键：回落参数必须与 k5_writer.open_dev() 的常态完全一致，
        # 否则回落后的「常态」比开机常态更硬（用户「停了还在震」）
        check("回落帧 p2/p3 = 0（与 open_dev 常态一致）",
              f[10] == 0 and f[11] == 0, (f[10], f[11]))
        check("回落帧就是 open_dev 常态帧",
              bytes(f[5:13]) == bytes((1, 1, 1, 0, 1, 0, 0, 0)), bytes(f[5:13]).hex())

    # 3. 常态 mode 1 不该被兜底打扰（"常态阻力一直在"）
    k2, sent2 = make(m)
    k2._trig_mode = 1
    k2._last_rumble_ts = time.time() - 10.0
    run_writer(k2, 0.25)
    check("常态下不误发任何 0x51", not any(f[3] == 0x51 for f in sent2),
          len(sent2))

    # 4. 音乐持续（不断刷新爆发）不误回落
    k3, sent3 = make(m)
    k3._trig_mode = 2
    k3._trig_burst_ts = time.time()
    k3._last_rumble_ts = time.time()
    stop = [False]

    def feed():
        while not stop[0]:
            k3._trig_burst_ts = time.time()
            k3._last_rumble_ts = time.time()
            time.sleep(0.02)
    th = threading.Thread(target=feed, daemon=True)
    th.start()
    run_writer(k3, 0.45)
    stop[0] = True
    check("持续鼓点期间不误回落", k3._trig_mode == 2, k3._trig_mode)

    # 5. mirror 模式下双侧都回落
    m.CFG["rt_mode"] = "mirror"
    k4, sent4 = make(m)
    k4._trig_mode = 2
    k4._trig_burst_ts = time.time()
    k4._last_rumble_ts = time.time() - 3.0
    run_writer(k4, 0.3)
    sides = sorted({f[6] for f in sent4 if len(f) > 7 and f[3] == 0x51 and f[7] == 1})
    check("mirror 模式左右扳机都回落", sides == [1, 2], sides)
    m.CFG["rt_mode"] = "idle"

    # 6. 停音残响：上游仍在发帧（不满足流断），但转子已静默 → 必须回落
    k6, _ = make(m)
    k6._trig_mode = 2
    k6._trig_burst_ts = time.time()          # 刚爆发，未超 0.68s
    k6._last_rumble_ts = time.time()         # 上游仍在发帧，不满足流断 2s
    k6._last_nz = (time.time() - 2.0, 100, 100)   # 但握把转子已静默 2s
    run_writer(k6, 0.3)
    check("转子静默时即使上游仍在发帧也回落（停音残响场景）",
          k6._trig_mode == 1, k6._trig_mode)

    # 6b. mirror 残留：队列里压着右扳机爆发帧时，trigger_rest 必须清掉它
    m.CFG["rt_mode"] = "mirror"
    k6b, _ = make(m)
    k6b._trig_q = [m.k5_force_trigger(2, 2, 0, 1, 200, 70)]   # 残留的 m2/s2
    k6b.trigger_rest()
    modes = [(f[6], f[7]) for f in k6b._trig_q]
    check("trigger_rest 清掉残留爆发帧（防止右扳机停在 mode 2）",
          all(md == 1 for _, md in modes) and sorted(s for s, _ in modes) == [1, 2],
          modes)
    m.CFG["rt_mode"] = "idle"

    # 7. 转子活跃时不误回落
    k7, _ = make(m)
    k7._trig_mode = 2
    k7._trig_burst_ts = time.time()
    k7._last_rumble_ts = time.time()
    k7._last_nz = (time.time(), 100, 100)
    run_writer(k7, 0.3)
    check("转子仍在震时不误回落", k7._trig_mode == 2, k7._trig_mode)

    # 8. 写进程重启会复位爆发状态（避免旧状态残留）
    k5, _ = make(m)
    k5._trig_mode = 2
    k5._trig_burst_ts = time.time()
    k5._last = (100, 100)
    k5._proc = None
    k5._spawn_writer = m.K5._spawn_writer.__get__(k5, m.K5)
    m.subprocess.Popen = lambda *a, **kw: FakeProc()
    k5._spawn_writer()
    check("写进程重启后 _trig_mode 复位为 1", k5._trig_mode == 1, k5._trig_mode)
    check("写进程重启后爆发时刻清零", k5._trig_burst_ts == 0.0, k5._trig_burst_ts)

    bad = [r for r in RESULTS if not r[1]]
    print("\n=== %d/%d PASS ===" % (len(RESULTS) - len(bad), len(RESULTS)))
    for r in bad:
        print("  FAIL:", r[0], r[2])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
