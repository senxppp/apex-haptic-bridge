#!/usr/bin/env python3
"""D1 合并增益 + D3 自适应瞬态回归（零硬件 / 零引擎 / 零真实 HID 写）。

锁定 2026-11-21 D1（两级增益合并为单级转子曲线，交接文档 §16）与 D3（自适应瞬态
判据，rb-ema > max(floor, k×移动std)，原始 RMS 域）的行为面：
  1. legacy / merged 曲线钉值表 —— 字面值来自 backup-20261121-1118-d1-merge-gain/usbip_apex6.py
     （D1 前版本）对显式 CFG 一次求得。改曲线 = 必须同时改钉值并写明理由，不许悄悄改。
  2. 合并曲线单调不减、全部 ≤255、默认 knee 下 255 占比 0（验收目标 <5% 的下界）
  3. 通道分离：t 永远旧曲线（trig_force/trig_rattle 标定依赖它），l/r 走合并曲线
  4. lv1 瞬态判定固定在原始 RMS 域 —— 判别体 l=[6]*6+[9]*2：
       全包 rms=6.87、末两帧 rms=9，原始域 9 > 6.87*1.15 ✓ 采纳 → 95；
       若有人把比较挪回映射域：merged(6.87)=88，88/95 比值 1.08 <1.15 ✗ 漏判 → 得 88。
       断言 ==95 且 !=88，两支都钉死。
  5. D3 自适应（有状态，用例前必须 _57_reset()；EMA 用 100 包喂饱 ≈0.8s 真实时间）：
       - 安静床(4)→打击(20)：采纳 merged(20)=121（漏判则是 99）
       - 响床(40)→微变(42)：delta 2.7 < floor → 不误触发，= merged(整包)=156
       - 响床(40)→真打击(80)：采纳 merged(80)=207
       - **判别对**：床(40)→48 —— 1.15 规则漏（48 < 42.14×1.15=48.46 → 159），
         自适应抓（48-ema39.3=8.7 > floor → 167）。adaptive=0 时必须得 159。
  6. rumble boost 门控：合并=直通，回滚=旧 ×boost_x+boost_add；回滚 rumble(120,80)
     必须等于 HANDOFF-2026-11-14 记录的历史基准帧 [3,90,165,18,4,255,215]。
  7. CFG 新键代码默认值在位（import 不加载 json，防「删默认值、全靠 json」的隐性漂移）。

直接运行：python -I test_rotor_gain.py
"""
import sys
import threading
import time
import types

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, __file__.rsplit("\\", 1)[0] if "\\" in __file__ else ".")
import usbip_apex6 as new

results = []


def check(label, cond, detail=""):
    results.append(bool(cond))
    print("%s %-56s %s" % ("[PASS]" if cond else "[FAIL]", label, detail))


# ---- 显式 CFG（测试不依赖任何 motor_config.json）----
LEG = {"threshold": 2.0, "pcm_gain": 140, "strength": 100,
       "boost_x": 2.0, "boost_add": 55}
MER = dict(LEG, k5_rotor_gain=1, k5_floor=60, k5_gamma=0.6, k5_knee=215,
           k5_transient_adaptive=1, k5_transient_k=2.5,
           k5_transient_floor=3.0, k5_ema_alpha=0.04, k5_transient_hold_ms=140)


def set_cfg(d):
    new.CFG.update(d)


def reset_legacy():
    set_cfg(dict(LEG, k5_rotor_gain=0))


def reset_merged():
    set_cfg(dict(MER))
    new._57_reset()


# ---- 钉值表（祝福源：backup-20261121-1118-d1-merge-gain，见文件头）----
RMS_SET = (2, 4, 8, 16, 32, 48, 64, 80, 96, 112, 127)
LEGACY_PINS = {2: 6, 4: 11, 8: 22, 16: 43, 32: 82, 48: 117,
               64: 150, 80: 180, 96: 207, 112: 233, 127: 255}
MERGED_PINS = {2: 60, 4: 76, 8: 92, 16: 112, 32: 143, 48: 167,
               64: 188, 80: 207, 96: 224, 112: 238, 127: 245}


def body(vals3):
    """8 子帧 × 3 通道(t,l,r)，线上编码 i8+128"""
    out = bytearray()
    for t, l, r in vals3:
        out += bytes((t + 128, l + 128, r + 128))
    return bytes(out)


def body_l(lvals):
    """只动 l 通道的 8 子帧体"""
    return body([(0, v, 0) for v in lvals])


def feed_l(bed, n):
    """喂 n 包恒定床电平，让 EMA/环形缓冲收敛（125 包/s ≈ n/125 秒）"""
    for _ in range(n):
        new.decode_57(body_l([bed] * 8))


class FakeProc:
    def __init__(self):
        self.stdin = types.SimpleNamespace(write=lambda b: len(b), flush=lambda: None)

    def poll(self):
        return None

    def kill(self):
        pass

    def wait(self, timeout=None):
        pass


def make_k5():
    k = object.__new__(new.K5)
    for key, val in dict(dev=None, _proc=None, _last=None, _last_nz=(0.0, 0, 0),
                         writes=0, boosted_max=0, _pend=None, _trig_q=[],
                         _last_trig=None, _trig_mode=1, _fail_streak=0,
                         _last_send=0.0, _level_now=(0, 0), _k5_min_iv=0.08,
                         _next_send_at=0.0, _dedup_hits=0, _wq=0, _wfail=0,
                         _throt=0, _sloww=0, _phase=0,
                         _last_sent_at=0.0, _last_trig_at=0.0).items():
        setattr(k, key, val)
    k._pend_lock = threading.Lock()
    k._trig_q = []
    k._last_rumble_ts = time.time()
    k._proc = FakeProc()
    return k


def main():
    # 0. CFG 新键代码默认值在位（必须在任何 set_cfg 之前断言，import 不加载 json）
    defaults_ok = (new.CFG.get("k5_rotor_gain") == 0
                   and new.CFG.get("k5_floor") == 60
                   and new.CFG.get("k5_gamma") == 0.6
                   and new.CFG.get("k5_knee") == 215
                   and new.CFG.get("k5_jump_bypass_merged") == 40
                   and new.CFG.get("k5_transient_adaptive") == 1
                   and new.CFG.get("k5_transient_k") == 2.5
                   and new.CFG.get("k5_transient_floor") == 3.0
                   and new.CFG.get("k5_ema_alpha") == 0.04
                   and new.CFG.get("k5_transient_hold_ms") == 140)
    check("CFG 新键代码默认在位（回滚基线不被删）", defaults_ok,
          "rotor=%s adap=%s k=%s alpha=%s hold=%s" % (
              new.CFG.get("k5_rotor_gain"), new.CFG.get("k5_transient_adaptive"),
              new.CFG.get("k5_transient_k"), new.CFG.get("k5_ema_alpha"),
              new.CFG.get("k5_transient_hold_ms")))

    # 1. legacy 钉值（回滚模式 = D1 前曲线）
    reset_legacy()
    bad = [r for r in RMS_SET if new._level_from_rms(r) != LEGACY_PINS[r]]
    check("legacy 曲线钉值表（11 点）", not bad, "不一致: %s" % bad if bad else "")

    # 2. merged 钉值
    reset_merged()
    bad = [r for r in RMS_SET if new._level_from_rms(r, True) != MERGED_PINS[r]]
    check("merged 曲线钉值表（11 点）", not bad, "不一致: %s" % bad if bad else "")

    # 3/4. 单调 + 上界
    reset_merged()
    vals = [new._level_from_rms(r, True) for r in range(128)]
    mono = all(b >= a for a, b in zip(vals, vals[1:]))
    check("merged 0..127 单调不减", mono, "" if mono else "首个下坡 @%d" %
          next(i for i in range(1, 128) if vals[i] < vals[i - 1]))
    over = [v for v in vals if v > 255]
    check("merged 全部 ≤255（软限幅不溢出）", not over, str(over[:3]))

    # 5. 255 占比（验收目标 <5% 的下界：默认 knee 下应为 0）
    sat = sum(1 for v in vals if v == 255)
    check("默认参数 255 占比 = 0（目标 <5%）", sat == 0, "255 帧 %d/128" % sat)

    # 6. strength=150 极端增益下仍不硬裁溢出
    set_cfg({"strength": 150})
    vals2 = [new._level_from_rms(r, True) for r in range(128)]
    over2 = [v for v in vals2 if v > 255]
    mono2 = all(b >= a for a, b in zip(vals2, vals2[1:]))
    check("strength=150 仍单调且 ≤255", not over2 and mono2, "max=%d" % max(vals2))
    set_cfg({"strength": 100})

    # 7. 通道分离：t 恒旧曲线，l/r 走合并曲线
    reset_merged()
    b2 = body([(60, 30, 30)] * 8)
    t, l, r = new.decode_57(b2)
    check("t 通道 = 旧曲线钉值 142（trig 标定不变）", t == 142, str(t))
    check("l/r = 合并曲线钉值 139", (l, r) == (139, 139), str((l, r)))

    # 8. 回滚模式整包 decode = 旧曲线（legacy(30)=77）
    reset_legacy()
    t, l, r = new.decode_57(b2)
    check("回滚模式 decode = (142,77,77) = D1 前行为", (t, l, r) == (142, 77, 77),
          str((t, l, r)))

    # 9. 瞬态判据在原始 RMS 域（判别体：映射域会漏判成 88）
    reset_merged()
    bt = body_l([6] * 6 + [9] * 2)
    t, l, r = new.decode_57(bt)
    check("瞬态原始域判定采纳末两帧 → 95", l == 95, str(l))
    check("不是映射域判定的 88（判据位置被挪会挂）", l != 88, str(l))

    # 10. 无瞬态时返回整包映射值 merged(6)=85（先重置：上一用例的命中会闩锁 140ms）
    reset_merged()
    b3 = body_l([6] * 8)
    t, l, r = new.decode_57(b3)
    check("无瞬态 → 整包值 merged(6)=85", l == 85, str(l))

    # ---- 11. D3 自适应（有状态；EMA 100 包喂饱）----
    reset_merged()
    feed_l(4, 100)
    t, l, r = new.decode_57(body_l([4] * 6 + [20] * 2))
    check("D3 安静床(4)→打击(20) 采纳 → 121", l == 121, str(l))

    reset_merged()
    feed_l(40, 100)
    t, l, r = new.decode_57(body_l([40] * 6 + [42] * 2))
    check("D3 响床(40)→微变(42) 不误触发 → 156", l == 156, str(l))

    reset_merged()
    feed_l(40, 100)
    t, l, r = new.decode_57(body_l([40] * 6 + [80] * 2))
    check("D3 响床(40)→真打击(80) 采纳 → 207", l == 207, str(l))

    # 12. 判别对：床(40)→48，1.15 规则漏（48<42.14×1.15）、自适应抓
    reset_merged()
    set_cfg({"k5_transient_adaptive": 0})
    feed_l(40, 100)
    t, l, r = new.decode_57(body_l([40] * 6 + [48] * 2))
    check("adaptive=0 退回 1.15 规则 → 漏判 159", l == 159, str(l))
    reset_merged()   # 恢复 adaptive=1（reset_merged 会重置全部 MER 键）
    feed_l(40, 100)
    t, l, r = new.decode_57(body_l([40] * 6 + [48] * 2))
    check("adaptive=1 自适应抓起手 → 167", l == 167, str(l))

    # 13. 闩锁：一次打击的滑窗衰减期内不同电平数 ≤3（2026-11-21 断联事故回归——
    #     无闩锁时衰减窗 8 包各给一个递减电平 = 7~8 次写/击，写入率超 DPC 预算）
    reset_merged()
    samples = [4] * 100 + [20] + [4] * 12
    levels = []
    for i in range(len(samples) - 7):
        t, l, r = new.decode_57(body_l(samples[i:i + 8]))
        levels.append(l)
    hit_idx = len([4] * 100) - 7          # 首个包含打击样本的窗口
    post = levels[hit_idx:hit_idx + 13]   # 衰减窗 + 闩锁期 + 回落
    distinct = len(set(v for v in post if v))
    check("闩锁：打击后 13 包非零电平去重 ≤3", distinct <= 3,
          "distinct=%d levels=%s" % (distinct, post))
    check("闩锁期电平恒定（首窗 = 次窗）", post[0] == post[1], str(post[:3]))

    # 14. rumble boost 门控
    reset_merged()
    k = make_k5()
    k.rumble(66, 40)
    check("合并模式 rumble 直通（不再放大）",
          list(k._pend) == [3, 90, 165, 18, 4, 66, 40], str(list(k._pend)))
    reset_legacy()
    k = make_k5()
    k.rumble(66, 40)
    check("回滚模式 rumble = 旧 ×2.0+55",
          list(k._pend) == [3, 90, 165, 18, 4, 187, 135], str(list(k._pend)))
    k = make_k5()
    k.rumble(120, 80)
    check("回滚 rumble(120,80) = HANDOFF 历史基准帧",
          list(k._pend) == [3, 90, 165, 18, 4, 255, 215], str(list(k._pend)))

    # 14.（已挪到 main 开头：默认值断言必须在任何 set_cfg 之前）

    n_pass = sum(results)
    print("=== %d/%d PASS ===" % (n_pass, len(results)))
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
