# trig_lab.py — k5 扳机手感标定台（线下工具，不参与主链路）
#
# 为什么需要它：
#   k5 的 0x51 SetForceTrigger 有 6 个 mode（ref_apex6/TriggerEffect.h）：
#       0 Normal（释放）  1 Race（持续阻力）  2 RecoilRattle（后坐抖动）
#       3 SniperBreak（突破）  4 Lock（锁定）  5 Vibration（震动）
#   其中 mode 5 在 ref_apex6/Apex5Protocol.cpp 里有完整构造分支，但注释写明
#   "Live mode 5 remains under-documented" —— p1/p2/p3 的物理含义没有文档，
#   只能实测。本工具把"猜参数"变成"按序号打分"：
#   它按固定节奏逐组下发，终端打印序号，用户只需记住哪几号"像转棍"。
#
# 为什么不能靠耳朵/日志判断：
#   扳机是纯输出执行器，USB HID 输出报告单向、无状态回读。手感只能由人来判。
#   所以我们只保证"第 N 组下发的参数是 X"，把判断权交给手。
#
# 用法：
#   python trig_lab.py --pads                  # 先确认哪个 XInput 槽是真的手柄
#   python trig_lab.py --seq spin              # 10 组，约 30 秒
#   python trig_lab.py --seq buzz              # mode2 低强度常驻（对照组）
#   python trig_lab.py --manual 5 80 80 40 2   # 手动：mode p1 p2 p3 秒数
#   python trig_lab.py --seq rest              # 只还原常态
#
# ⚠ 标定前把 motor_config.json 的 "trig_burst" 改成 0（热加载 1 秒生效），
#   否则主链路的鼓点爆发会抢扳机；标定完改回 1。
#   本工具退出（含 Ctrl+C）时一定会把双侧还原成常态 mode 1 p2=0,p3=0。
import argparse
import ctypes
import json
import os
import sys
import time
from ctypes import wintypes

import hid

VID, PID = 0x37D7, 0x2501
BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "trig_lab.log")
CFG_PATH = os.path.join(BASE, "motor_config.json")


def make_frame(side, mode, start=0, p1=1, p2=1, p3=1, match=0, floor=True):
    """0x51 SetForceTrigger。
    floor=True 用 max(1,·)（与主链路 k5_force_trigger 一致）；
    floor=False 原样下发——mode 1 的常态就是 p2=0,p3=0，被抬成 1 会偏硬。"""
    if floor:
        p1, p2, p3 = max(1, p1), max(1, p2), max(1, p3)
    f = bytearray(32)
    f[0], f[1], f[2], f[3], f[4] = 0x03, 0x5A, 0xA5, 0x51, 8
    f[5:13] = (1, side, mode, start, p1, p2, p3, match)
    ck = 0
    for i in range(2, 31):
        ck = (ck + f[i]) & 0xFF
    f[31] = ck
    return bytes(f)


def rest_frame(side):
    return make_frame(side, 1, 0, 1, 0, 0, floor=False)


def rumble_zero():
    return bytes([0x03, 0x5A, 0xA5, 0x12, 0x04, 0, 0])


def open_dev():
    for d in hid.enumerate(VID, PID):
        if d.get("usage_page") == 0xFFA0:
            dev = hid.device()
            dev.open_path(d["path"])
            dev.set_nonblocking(1)
            return dev
    return None


def log(line):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%H:%M:%S"), line))
    except Exception:
        pass


def quiet_others():
    """标定期间把主链路的「鼓点爆发」临时关掉，否则它会在音乐节拍上抢扳机，
    标定结果全是噪声。热加载周期 1s，所以要等 1.3s 才真正静默。
    返回原值，标定结束必须还原（异常/中断路径也还原）。"""
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return None
    old = cfg.get("trig_burst", 1)
    if old == 0:
        return None
    cfg["trig_burst"] = 0
    try:
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception:
        return None
    print("已临时关闭主链路鼓点爆发（trig_burst=0），等 1.3s 生效…")
    log("QUIET trig_burst 1->0")
    time.sleep(1.3)
    return old


def restore_others(old):
    if old is None:
        return
    try:
        with open(CFG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["trig_burst"] = old
        with open(CFG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
        log("QUIET trig_burst 0->%s" % old)
        print("已还原主链路鼓点爆发（trig_burst=%s）。" % old)
    except Exception as e:
        print("⚠ 还原 trig_burst 失败：%r（请手动把 motor_config.json 的 trig_burst 改回 1）" % e)


# ---------------- XInput 手柄识别 ----------------
class XSTATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", wintypes.DWORD), ("wButtons", wintypes.WORD),
                ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


def xinput():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return ctypes.WinDLL(name)
        except OSError:
            continue
    return None


def scan_pads(dur=1.6):
    """采样一遍所有 XInput 槽，标出「虚拟设备」。
    虚拟设备特征（实测）：LT 只在 0 / 40 之间以 ~2Hz 跳变——那是主链路为了让
    引擎保持活跃做的假输入（bulk_in ep1 每 0.5s 半扣一次）。真手柄 LT 不会这样。"""
    x = xinput()
    if x is None:
        return []
    out = []
    for i in range(4):
        st = XSTATE()
        vals, pk = [], []
        t_end = time.time() + dur
        while time.time() < t_end:
            if x.XInputGetState(i, ctypes.byref(st)) == 0:
                vals.append(st.bLeftTrigger)
                pk.append(st.dwPacketNumber)
            time.sleep(0.05)
        if not vals:
            out.append({"idx": i, "ok": False})
            continue
        uniq = set(vals)
        toggles = sum(1 for a, b in zip(vals, vals[1:]) if a != b)
        virtual = uniq <= {0, 40} and toggles >= 2
        out.append({"idx": i, "ok": True, "lt": max(vals), "uniq": sorted(uniq),
                    "toggles": toggles, "rate": len(pk), "virtual": virtual})
    return out


def print_pads():
    rows = scan_pads()
    if not rows:
        print("XInput 不可用（xinput1_4/1_3/9_1_0 都加载失败）")
        return
    print("槽  状态       LT峰值  取值集合           跳变次数  判定")
    for r in rows:
        if not r["ok"]:
            print(" %d  未连接" % r["idx"])
            continue
        verdict = "虚拟设备(我方注入)" if r["virtual"] else "候选：真手柄"
        print(" %d  已连接   %3d    %-16s %5d     %s" % (
            r["idx"], r["lt"], str(r["uniq"])[:16], r["toggles"], verdict))
    real = [r for r in rows if r["ok"] and not r["virtual"]]
    if real:
        print("\n→ 建议 --pad %d（真手柄）。拿它做「LT 按住 = 棍花」判据。" % real[0]["idx"])
    else:
        print("\n→ 没找到真手柄：手柄可能没连/没开机，或只连着虚拟设备。")


# ---------------- 标定序列 ----------------
def seq_spin():
    """mode 5 参数矩阵：12 组，看清 p1/p2/p3 各自是强度还是频率。
    对应物理目标：棍花 = 按住 LT 持续旋转，要"一直在动"而不是"打一下"。"""
    out = []
    for p1 in (40, 80, 160):
        for p2 in (40, 80, 120):
            out.append(("m5 p=%d,%d,40" % (p1, p2), 5, 0, p1, p2, 40, 1.4))
    return out


def seq_buzz():
    """mode 2 低强度常驻（今天就能用的低保真版），3 档强度 + 2 档次级。"""
    return [
        ("m2 p=55,40", 2, 0, 1, 55, 40, 1.4),
        ("m2 p=70,40", 2, 0, 1, 70, 40, 1.4),
        ("m2 p=90,60", 2, 0, 1, 90, 60, 1.4),
        ("m2 p=110,70", 2, 0, 1, 110, 70, 1.4),
    ]


def seq_damp():
    """mode 1「赛车/阻尼」：空间站里这一页只有两个滑块——阻尼力度(1..255)与
    阻尼起始位置(0..192)。我们一直发的是 (start=0, force=1)，也就是"最轻的阻尼
    从第 0 毫米就开始"。如果手感偏硬，只可能是 course 参数落错字节或 0 被当最大值。
    这组用矩阵把两个旋钮分开：先看力度有没有反馈，再看起始位置有没有区别。"""
    return [
        ("m1 start=0  force=1(现状)", 1, 0, 1, 0, 0, 1.4),
        ("m1 start=0  force=8", 1, 0, 8, 0, 0, 1.4),
        ("m1 start=0  force=32", 1, 0, 32, 0, 0, 1.4),
        ("m1 start=0  force=120", 1, 0, 120, 0, 0, 1.4),
        ("m1 start=20 force=8", 1, 20, 8, 0, 0, 1.4),
        ("m1 start=64 force=8", 1, 64, 8, 0, 0, 1.4),
        ("m1 start=64 force=120", 1, 64, 120, 0, 0, 1.4),
        ("m1 start=0  force=255", 1, 0, 255, 0, 0, 1.4),
        ("m0 释放(无阻力)", 0, 0, 0, 0, 0, 1.4),
    ]


def seq_vib5():
    """mode 5「扳机震动」：空间站文案说它跟着**握把震动数据**走，参数是
    起始位置/起始力度/震动力度/频率/行程/屏蔽值。这里按 [pos, level, dist, freq] 试探，
    看这支手柄到底是固件自己跟 0x12、还是靠参数自己抖。"""
    return [
        ("m5 pos=0 lvl=1 dist=0 f=0", 5, 0, 1, 0, 0, 1.4),
        ("m5 pos=0 lvl=8 dist=255 f=30", 5, 0, 8, 255, 30, 1.4),
        ("m5 pos=0 lvl=30 dist=255 f=60", 5, 0, 30, 255, 60, 1.4),
        ("m5 pos=0 lvl=120 dist=255 f=60", 5, 0, 120, 255, 60, 1.4),
        ("m5 pos=64 lvl=30 dist=128 f=90", 5, 64, 30, 128, 90, 1.4),
    ]


def seq_modes():
    """六种 mode 各来一次，先建立"这个手柄能做到什么"的地图。"""
    return [
        ("m1 阻力 p=1,0", 1, 0, 1, 1, 0, 1.2),
        ("m1 阻力 p=120,0", 1, 0, 1, 120, 0, 1.2),
        ("m2 抖动 p=90,50", 2, 0, 1, 90, 50, 1.2),
        ("m3 突破 p=90,60", 3, 0, 1, 90, 60, 1.2),
        ("m4 锁定 p=255", 4, 0, 255, 1, 1, 1.2),
        ("m5 震动 p=80,80,40", 5, 0, 80, 80, 40, 1.2),
    ]


SEQS = {"spin": seq_spin, "buzz": seq_buzz, "modes": seq_modes,
        "damp": seq_damp, "vib5": seq_vib5}


def run_steps(dev, steps, sides, on_s, off_s):
    total = len(steps)
    print("共 %d 组，每组 %.1fs 效果 + %.1fs 间隔，预计 %.0f 秒\n" % (
        total, on_s, off_s, total * (on_s + off_s)))
    for n, (name, mode, start, p1, p2, p3, sec) in enumerate(steps, 1):
        sec = on_s or sec
        print("  [%2d/%d] %-22s" % (n, total, name), flush=True)
        log("STEP %d/%d %s" % (n, total, name))
        for s in sides:
            dev.write(list(make_frame(s, mode, start, p1, p2, p3)))
        time.sleep(sec)
        for s in sides:
            dev.write(list(rest_frame(s)))
        time.sleep(off_s)
    print("\n标定结束。记住像「转棍」的序号，回来告诉我。")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pads", action="store_true", help="扫描 XInput 槽，识别真手柄")
    ap.add_argument("--seq", choices=sorted(SEQS), help="内置标定序列")
    ap.add_argument("--manual", nargs=5, metavar=("MODE", "P1", "P2", "P3", "SEC"),
                    help="手动下发一组：mode p1 p2 p3 秒数")
    ap.add_argument("--side", choices=("1", "2", "both"), default="1",
                    help="哪个扳机：1=左(LT/L2) 2=右(RT/R2) both=双")
    ap.add_argument("--on", type=float, default=0.0, help="覆盖每组持续秒数")
    ap.add_argument("--off", type=float, default=0.7, help="组间间隔秒数")
    ap.add_argument("--no-solo", action="store_true",
                    help="不改 motor_config（默认会临时关掉主链路的鼓点爆发）")
    a = ap.parse_args()

    if a.pads:
        print_pads()
        return 0

    if not a.seq and not a.manual:
        ap.print_help()
        return 0

    sides = [1, 2] if a.side == "both" else [int(a.side)]
    dev = open_dev()
    if dev is None:
        print("打不开 k5（VID 37D7 PID 2501 / usage_page 0xFFA0）。")
        print("检查：手柄是否开机连上？写进程是否已退出？")
        return 2

    print("k5 已连接。目标扳机：%s" % ("双" if len(sides) == 2 else sides[0]))
    log("OPEN sides=%s" % sides)
    old_burst = None if a.no_solo else quiet_others()
    rc = 0
    try:
        if a.manual:
            mode, p1, p2, p3, sec = a.manual
            mode, p1, p2, p3, sec = int(mode), int(p1), int(p2), int(p3), float(sec)
            print("手动：mode %d  p=%d,%d,%d  %.1fs" % (mode, p1, p2, p3, sec))
            log("MANUAL m%d p%d,%d,%d %.1fs" % (mode, p1, p2, p3, sec))
            for s in sides:
                dev.write(list(make_frame(s, mode, 0, p1, p2, p3)))
            time.sleep(sec)
        else:
            steps = SEQS[a.seq]()
            run_steps(dev, steps, sides, a.on, a.off)
    except KeyboardInterrupt:
        print("\n中断。")
    finally:
        for s in sides:
            try:
                dev.write(list(rest_frame(s)))
            except Exception:
                pass
        try:
            dev.write(list(rumble_zero()))
        except Exception:
            pass
        log("REST sides=%s" % sides)
        print("已还原常态（mode 1 p2=0,p3=0）+ 握把归零。")
        restore_others(old_burst)
    return rc


if __name__ == "__main__":
    sys.exit(main())
