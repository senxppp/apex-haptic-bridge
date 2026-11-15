# test_trig_force.py — 「音频包络 → 阻尼力度」渲染器的离线回归
# 不启动引擎、不挂设备、不动真链路；纯假时钟。
#
# 直接运行：python -I test_trig_force.py
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import trig_force as tf

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                       ("  " + str(detail)) if detail else ""))


def cfg(**kw):
    c = dict(tf.DEFAULTS)
    c.update(kw)
    return c


def run(r, seq):
    """seq = [(now, level)] → 力值序列"""
    return [r.step(t, lv) for t, lv in seq]


def test_base_and_cap():
    c = cfg(tf_base=1, tf_max=140)
    r = tf.ForceRenderer(lambda: c)
    check("首帧静默 = 基线力", r.step(0.0, 0) == 1.0, r.force)
    seq = [(0.1 * i, 255) for i in range(1, 40)]
    vals = run(r, seq)
    check("满幅持续输入后顶到上限", abs(vals[-1] - 140) < 3, vals[-1])
    check("不会越过 tf_max", max(vals) <= 140.0, max(vals))


def test_attack_faster_than_release():
    c = cfg(tf_base=1, tf_max=200, tf_attack_ms=15, tf_release_ms=180)
    r = tf.ForceRenderer(lambda: c)
    r.step(0.0, 0)
    a = r.step(0.03, 200)          # 30ms 上升
    up = a - 1.0
    b0 = r.step(0.06, 0)           # 30ms 下降
    down = a - b0
    check("上升快于下降（有惯性感）", up > down * 2, (round(up, 1), round(down, 1)))


def test_punch():
    c = cfg(tf_base=1, tf_max=255, tf_punch_db=20, tf_punch_ms=90)
    r = tf.ForceRenderer(lambda: c)
    r.step(0.0, 0)
    base = r.step(0.05, 20)        # 稳态小音量
    hit = r.step(0.10, 200)        # 突然爆发 → 瞬态
    check("瞬态立刻加力（高于稳态）", hit > base + 10, (round(base, 1), round(hit, 1)))
    late = r.step(0.30, 200)       # 过了 punch 窗口
    check("punch 到时自动收掉", late < hit, (round(hit, 1), round(late, 1)))


def test_idle_release():
    c = cfg(tf_base=20, tf_idle_release_s=0.5)
    r = tf.ForceRenderer(lambda: c)
    r.step(0.0, 200)
    v = r.step(0.2, 200)
    check("有信号时按包络出力", v > 20, v)
    v2 = r.step(1.0, 0)
    check("静默超过 0.5s 后归零（mode0 释放）", v2 == 0.0, v2)


def test_quantize():
    c = cfg(tf_step=8)
    r = tf.ForceRenderer(lambda: c)
    check("量化到 8 的倍数", r.quantize(37) == 40, r.quantize(37))
    check("量化不越 255", r.quantize(300) == 255, r.quantize(300))
    check("量化不为负", r.quantize(-5) == 0, r.quantize(-5))
    check("force>0 时最小档是 step，不会量化成 0（0=释放）",
          r.quantize(1) == 8, r.quantize(1))
    check("force=0 仍量化成 0", r.quantize(0) == 0, r.quantize(0))
    c2 = cfg(tf_step=1)
    r2 = tf.ForceRenderer(lambda: c2)
    check("step=1 时原样通过", r2.quantize(37) == 37, r2.quantize(37))


def test_gate():
    c = cfg(tf_min_iv_ms=100)
    g = tf.WriteGate(lambda: c)
    check("首帧放行", g.allow(1.0, 40) is True)
    check("同值不重复写", g.allow(1.2, 40) is False)
    check("间隔不足被挡", g.allow(1.05, 60) is False)
    check("间隔够了放行", g.allow(1.15, 60) is True)
    check("统计写入次数", g.writes == 2, g.writes)
    check("被挡计数", g.blocked == 1, g.blocked)
    # 大幅跳变（瞬态）允许突破间隔：否则 attack 被闸门磨平
    g2 = tf.WriteGate(lambda: cfg(tf_min_iv_ms=200, tf_jump_bypass=24))
    check("首帧放行(2)", g2.allow(0.0, 40) is True)
    check("小幅变化仍被间隔挡住", g2.allow(0.05, 48) is False)
    check("大幅跳变立刻放行", g2.allow(0.06, 96) is True)
    g.reset()
    check("reset 后同值可再写（设备重init自愈）", g.allow(2.0, 60) is True)


def test_budget():
    """模拟 1 秒真实包络（125Hz 帧率）→ 写入次数必须远低于 12.5/s 的预算。"""
    c = cfg(tf_min_iv_ms=95, tf_step=8, tf_base=1, tf_max=140)
    r = tf.ForceRenderer(lambda: c)
    g = tf.WriteGate(lambda: c)
    t = 0.0
    for i in range(125):           # 8ms 一帧
        lvl = 0 if i < 20 else (60 + (i % 7) * 10)
        f = r.step(t, lvl)
        g.allow(t, r.quantize(f))
        t += 0.008
    check("1 秒内扳机写入 ≤ 11 次", g.writes <= 11, g.writes)
    check("量化确实减少了重复写", g.blocked > 0, g.blocked)


def test_disable_semantics():
    """tf_enable=0 时渲染器不参与主链路（调用方判断），此处只验证基线值可靠。"""
    c = cfg(tf_enable=0, tf_base=1)
    r = tf.ForceRenderer(lambda: c)
    v = run(r, [(0.0, 0), (0.1, 0)])
    check("关闭时静默仍输出基线（1=最轻）", v[-1] == 1.0, v)


def main():
    test_base_and_cap()
    test_attack_faster_than_release()
    test_punch()
    test_idle_release()
    test_quantize()
    test_gate()
    test_budget()
    test_disable_semantics()
    bad = [r for r in RESULTS if not r[1]]
    print("\n=== %d/%d PASS ===" % (len(RESULTS) - len(bad), len(RESULTS)))
    for r in bad:
        print("  FAIL:", r[0], r[2])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
