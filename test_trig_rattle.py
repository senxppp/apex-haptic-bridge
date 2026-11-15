# test_trig_rattle.py — 「机枪抖动 v2」离线回归（假时钟，无设备/无引擎）
#
# 直接运行：python -I test_trig_rattle.py
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import trig_rattle as tr

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                       ("  " + str(detail)) if detail else ""))


def cfg(**kw):
    c = dict(tr.DEFAULTS)
    c.update(kw)
    return c


def test_compose():
    check("compose(0)=0", abs(tr.compose(0, 0.5, 0.8)) < 1e-9)
    check("compose(1)=ceiling", abs(tr.compose(1, 0.5, 0.8) - 0.8) < 1e-6)
    mid = tr.compose(0.5, 0.5, 0.8)
    lo = tr.compose(0.25, 0.5, 0.8)
    hi = tr.compose(0.75, 0.5, 0.8)
    check("拐点单调递增", lo < mid < hi, (lo, mid, hi))
    check("拐点处连续（左右极限接近）",
          abs(tr.compose(0.4999, 0.5, 0.8) - tr.compose(0.5001, 0.5, 0.8)) < 1e-3,
          (tr.compose(0.4999, 0.5, 0.8), tr.compose(0.5001, 0.5, 0.8)))
    check("拐点以上被压缩（斜率变小）",
          (tr.compose(0.6, 0.5, 0.8) - mid) < (mid - tr.compose(0.4, 0.5, 0.8)),
          "上半程增量更小")


def test_silence_rest():
    c = cfg()
    r = tr.Rattle(lambda: c)
    check("静音首帧不动作", r.update(0.0, 0) is None)
    # 有信号 → 出力
    acts = [r.update(0.01 * i, 120) for i in range(10)]
    check("有信号时出力", any(a and a[0] == tr.ACT_SET for a in acts))
    # 静音超过 release_ms → rest
    t = 0.10
    rest_at = None
    for i in range(40):
        out = r.update(t, 0)
        if out and out[0] == tr.ACT_REST:
            rest_at = t
            break
        t += 0.01
    check("静音超过释放时间后回落常态", rest_at is not None and 0.15 < rest_at < 0.45, rest_at)


def test_head():
    c = cfg(tr_head_ms=30, tr_lockout_ms=70)
    r = tr.Rattle(lambda: c)
    r.update(0.000, 10)
    a = r.update(0.008, 200)          # 突然爆发 → 头
    check("瞬态立刻给满强度", a and a[0] == tr.ACT_SET and a[1] == 200, a)
    check("头被计数", r.heads == 1, r.heads)
    # 头窗口内维持满强度（同值不重复返回）
    a2 = r.update(0.020, 200)
    check("头窗口内不重复下发", a2 is None, a2)
    # 头结束后落到身
    a3 = r.update(0.060, 200)
    check("头结束后强度低于满档", a3 and a3[0] == tr.ACT_SET and a3[1] < 200, a3)


def test_lockout():
    c = cfg(tr_lockout_ms=70)
    r = tr.Rattle(lambda: c)
    r.update(0.000, 10)
    r.update(0.008, 200)              # 第 1 个头
    n1 = r.heads
    r.update(0.020, 10)               # 掉下去
    r.update(0.030, 200)              # 锁定期内再爆 → 不应起新头
    check("锁定期内不重复起头（防机关枪）", r.heads == n1, r.heads)
    r.update(0.120, 10)
    r.update(0.130, 200)              # 过锁 → 可再起头
    check("过锁后可再起头", r.heads == n1 + 1, r.heads)


def test_body_follows_level():
    c = cfg(tr_head_ms=0, tr_lockout_ms=0)   # 关掉头，只看身
    lo = tr.Rattle(lambda: c)
    hi = tr.Rattle(lambda: c)
    vals_lo, vals_hi = [], []
    for i in range(30):
        t = i * 0.05
        a = lo.update(t, 40)
        b = hi.update(t, 220)
        if a:
            vals_lo.append(a[1])
        if b:
            vals_hi.append(b[1])
    check("大音量比小音量强度更高", max(vals_hi) > max(vals_lo), (max(vals_lo), max(vals_hi)))
    check("强度不越界（55..200）",
          min(vals_lo) >= 55 and max(vals_hi) <= 200, (min(vals_lo), max(vals_hi)))


def test_quantize():
    c = cfg()
    r = tr.Rattle(lambda: c)
    got = []
    for i in range(30):
        a = r.update(i * 0.05, 30 + i * 7)
        if a and a[0] == tr.ACT_SET:
            got.append(a[1])
    step = int(c.get("tr_step", 10))
    check("p2 都对齐量化步长", all(v % step == 0 for v in got), got[:8])


def test_p3_sharpness():
    c = cfg(tr_head_ms=0, tr_lockout_ms=0)
    r = tr.Rattle(lambda: c)
    r.update(0.0, 20)
    a = r.update(0.008, 220)     # 陡
    check("陡上升 → 高频档（p3 高）", a and a[2] >= 65, a)
    r2 = tr.Rattle(lambda: c)
    r2.update(0.0, 200)
    b = r2.update(0.008, 210)    # 平缓
    check("平缓上升 → 频率档较低", b and b[2] < 65, b)


def test_gate():
    c = cfg(tr_min_iv_ms=140, tr_jump_bypass=60)
    g = tr.Gate(lambda: c)
    check("首帧放行", g.allow(0.0, 60) is True)
    check("同值不写", g.allow(0.5, 60) is False)
    check("间隔内小变化被挡", g.allow(0.05, 70) is False)
    check("大幅跳变放行（头）", g.allow(0.06, 200) is True)
    check("间隔够了放行", g.allow(0.30, 90) is True)


def test_budget():
    """模拟 1 秒 125Hz 输入（含两次打击），写入次数必须远低于 12.5/s。"""
    c = cfg()
    r = tr.Rattle(lambda: c)
    g = tr.Gate(lambda: c)
    writes = 0
    t = 0.0
    for i in range(125):
        lvl = 200 if 30 <= i < 40 else (30 + (i % 5) * 8)
        a = r.update(t, lvl)
        if a and a[0] == tr.ACT_SET:
            if g.allow(t, a[1]):
                writes += 1
        elif a and a[0] == tr.ACT_REST:
            writes += 1
        t += 0.008
    check("1 秒内抖动写入 ≤ 10 次", writes <= 10, writes)


def main():
    test_compose()
    test_silence_rest()
    test_head()
    test_lockout()
    test_body_follows_level()
    test_quantize()
    test_p3_sharpness()
    test_gate()
    test_budget()
    bad = [r for r in RESULTS if not r[1]]
    print("\n=== %d/%d PASS ===" % (len(RESULTS) - len(bad), len(RESULTS)))
    for r in bad:
        print("  FAIL:", r[0], r[2])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
