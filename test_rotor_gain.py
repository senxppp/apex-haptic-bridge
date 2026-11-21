#!/usr/bin/env python3
"""D1 合并增益回归（零硬件 / 零引擎 / 零真实 HID 写）。2026-11-21 随 D1 落地新增。

锁定 2026-11-21 D1（两级增益合并为单级转子曲线，交接文档 §16 / audio-side-audit.md §8）的行为面：
  1. legacy / merged 曲线钉值表 —— 字面值来自 backup-20261121-1118-d1-merge-gain/usbip_apex6.py
     （D1 前版本）对显式 CFG 一次求得。改曲线 = 必须同时改钉值并写明理由，不许悄悄改。
  2. 合并曲线单调不减、全部 ≤255、默认 knee 下 255 占比 0（验收目标 <5% 的下界）
  3. 通道分离：t 永远旧曲线（trig_force/trig_rattle 标定依赖它），l/r 走合并曲线
  4. lv1 瞬态判定固定在原始 RMS 域 —— 判别体 l=[6]*6+[9]*2：
       全包 rms=6.87、末两帧 rms=9，原始域 9 > 6.87*1.15 ✓ 采纳 → 95；
       若有人把比较挪回映射域：merged(6.87)=88 < 95*1.15？→ 88/95 比值 1.08 <1.15 ✗ 漏判 → 得 88。
       断言 ==95 且 !=88，两支都钉死。
  5. rumble boost 门控：合并=直通，回滚=旧 ×boost_x+boost_add；回滚 rumble(120,80)
     必须等于 HANDOFF-2026-11-14 记录的历史基准帧 [3,90,165,18,4,255,215]。
  6. CFG 新键代码默认值在位（import 不加载 json，防「删默认值、全靠 json」的隐性漂移）。

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
MER = dict(LEG, k5_rotor_gain=1, k5_floor=60, k5_gamma=0.6, k5_knee=215)


def set_cfg(d):
    new.CFG.update(d)


def reset_legacy():
    set_cfg(dict(LEG, k5_rotor_gain=0))


def reset_merged():
    set_cfg(dict(MER))


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
    check("strength=150 仍单调且 ≤255", not over2 and mono2,
          "max=%d" % max(vals2))
    set_cfg({"strength": 100})

    # 7. 通道分离：t 恒旧曲线，l/r 走合并曲线
    reset_merged()
    b2 = body([(60, 30, 30)] * 8)
    t, l, r = new.decode_57(b2)
    check("t 通道 = 旧曲线钉值 142（trig 标定不变）", t == 142, str(t))
    check("l/r = 合并曲线钉值 139", (l, r) == (139, 139), "%s" % ((l, r),))

    # 8. 回滚模式整包 decode = 旧曲线（legacy(30)=77）
    reset_legacy()
    t, l, r = new.decode_57(b2)
    check("回滚模式 decode = (142,77,77) = D1 前行为", (t, l, r) == (142, 77, 77),
          str((t, l, r)))

    # 9. 瞬态判据在原始 RMS 域（判别体：映射域会漏判成 88）
    reset_merged()
    bt = body([(0, 6, 0)] * 6 + [(0, 9, 0)] * 2)
    t, l, r = new.decode_57(bt)
    check("瞬态原始域判定采纳末两帧 → 95", l == 95, str(l))
    check("不是映射域判定的 88（判据位置被挪会挂）", l != 88, str(l))

    # 10. 无瞬态时返回整包映射值 merged(6)=85
    b3 = body([(0, 6, 0)] * 8)
    t, l, r = new.decode_57(b3)
    check("无瞬态 → 整包值 merged(6)=85", l == 85, str(l))

    # 11/12/13. rumble boost 门控
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

    # 14. CFG 新键代码默认值在位（import 不加载 json，这里断言的就是代码默认）
    defaults_ok = (new.CFG.get("k5_rotor_gain") == 0
                   and new.CFG.get("k5_floor") == 60
                   and new.CFG.get("k5_gamma") == 0.6
                   and new.CFG.get("k5_knee") == 215
                   and new.CFG.get("k5_jump_bypass_merged") == 40)
    check("CFG 新键代码默认在位（回滚基线不被删）", defaults_ok,
          "rotor_gain=%s floor=%s gamma=%s knee=%s jb_m=%s" % (
              new.CFG.get("k5_rotor_gain"), new.CFG.get("k5_floor"),
              new.CFG.get("k5_gamma"), new.CFG.get("k5_knee"),
              new.CFG.get("k5_jump_bypass_merged")))

    n_pass = sum(results)
    print("=== %d/%d PASS ===" % (n_pass, len(results)))
    return 0 if n_pass == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
