"""实时对比物理手柄与虚拟设备的 XInput 状态，验证输入桥是否在把真输入转过去。

用法：
    python tools/probe_bridge_live.py --sec 40 --out _bridge_live.txt

原理：XInput 只给"当前状态"，不给历史；所以必须在用户动作的同时采样。
本脚本同时读两个槽位，把**出现过变化的时刻**记下来，便于事后对齐
"物理变了多少 / 虚拟是否跟着变"。
"""
import argparse
import ctypes as C
import os
import sys
import time

from ctypes import wintypes as W


class GAMEPAD(C.Structure):
    _fields_ = [("wButtons", W.WORD), ("bLeftTrigger", C.c_ubyte),
                ("bRightTrigger", C.c_ubyte), ("sThumbLX", C.c_short),
                ("sThumbLY", C.c_short), ("sThumbRX", C.c_short),
                ("sThumbRY", C.c_short)]


class STATE(C.Structure):
    _fields_ = [("dwPacketNumber", W.DWORD), ("Gamepad", GAMEPAD)]


def load_xinput():
    for dll in ("xinput1_4.dll", "xinput1_3.dll", "xinput9_1_0.dll"):
        try:
            lib = C.WinDLL(dll)
            fn = lib.XInputGetState
            fn.argtypes = [W.DWORD, C.POINTER(STATE)]
            fn.restype = W.DWORD
            return fn
        except OSError:
            continue
    raise RuntimeError("找不到 XInput 库")


def snap(fn, idx):
    st = STATE()
    if fn(idx, C.byref(st)) != 0:
        return None
    g = st.Gamepad
    return (st.dwPacketNumber, g.wButtons, g.bLeftTrigger, g.bRightTrigger,
            g.sThumbLX, g.sThumbLY, g.sThumbRX, g.sThumbRY)


def fmt(r):
    if r is None:
        return "  (未连接)   "
    _, b, lt, rt, lx, ly, rx, ry = r
    return "btn=0x%04X LT=%3d RT=%3d LX=%6d LY=%6d RX=%6d RY=%6d" % (
        b, lt, rt, lx, ly, rx, ry)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sec", type=float, default=40.0)
    ap.add_argument("--hz", type=float, default=40.0)
    ap.add_argument("--out", default="_bridge_live.txt")
    ap.add_argument("--slots", default="0,1")
    args = ap.parse_args()

    fn = load_xinput()
    slots = [int(x) for x in args.slots.split(",") if x.strip() != ""]
    period = 1.0 / max(1.0, args.hz)

    last = {i: None for i in slots}
    pkt0 = {i: None for i in slots}
    lines = []
    changes = {i: 0 for i in slots}
    t0 = time.time()

    for i in slots:
        r = snap(fn, i)
        last[i] = r
        pkt0[i] = r[0] if r else None
        lines.append("[%6.2f] slot%d 起始 %s" % (0.0, i, fmt(r)))

    while time.time() - t0 < args.sec:
        for i in slots:
            r = snap(fn, i)
            if r != last[i]:
                changes[i] += 1
                lines.append("[%6.2f] slot%d 变化 %s" % (time.time() - t0, i, fmt(r)))
                last[i] = r
        time.sleep(period)

    lines.append("")
    for i in slots:
        r = snap(fn, i)
        pkt = r[0] if r else None
        d = (pkt - pkt0[i]) if (pkt is not None and pkt0[i] is not None) else -1
        lines.append("slot%d：%d 次状态变化，包号 %s → %s（+%d，%.0f 包/s）"
                     % (i, changes[i], pkt0[i], pkt, d, d / max(0.001, args.sec)))

    text = "\n".join(lines) + "\n"
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    sys.stdout.write(text)


if __name__ == "__main__":
    main()
