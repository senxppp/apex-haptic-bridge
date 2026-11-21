# -*- coding: utf-8 -*-
"""连采 4 秒，看哪个 XInput 槽位会周期出现 LT≈0x28（我们注入的假半扣）。

假半扣是我们自己在 bulk_in ep1 里每 0.5s 打一次 —— 哪个槽位跟着跳，
哪个就是我们模拟的 APEX6；另一个才是物理手柄。
"""
import ctypes as C
import time
from ctypes import wintypes as W


class XINPUT_GAMEPAD(C.Structure):
    _fields_ = [("wButtons", W.WORD), ("bLeftTrigger", C.c_ubyte), ("bRightTrigger", C.c_ubyte),
                ("sThumbLX", C.c_short), ("sThumbLY", C.c_short),
                ("sThumbRX", C.c_short), ("sThumbRY", C.c_short)]


class XINPUT_STATE(C.Structure):
    _fields_ = [("dwPacketNumber", W.DWORD), ("Gamepad", XINPUT_GAMEPAD)]


xi = C.WinDLL("xinput1_4.dll")
get_state = xi.XInputGetState
get_state.argtypes = [W.DWORD, C.POINTER(XINPUT_STATE)]
get_state.restype = W.DWORD

print("采样 4s（每 100ms 一次）—— 关注 LT 列：谁在 40 和 0 之间跳，谁就是虚拟设备")
print("%-6s %-7s %-7s %-9s %-9s" % ("t", "slot0LT", "slot1LT", "slot0包", "slot1包"))
lt0, lt1 = [], []
t0 = time.time()
while time.time() - t0 < 4.0:
    s = XINPUT_STATE()
    r0 = get_state(0, C.byref(s))
    lt0.append(s.Gamepad.bLeftTrigger if r0 == 0 else -1)
    p0 = s.dwPacketNumber if r0 == 0 else -1
    s = XINPUT_STATE()
    r1 = get_state(1, C.byref(s))
    lt1.append(s.Gamepad.bLeftTrigger if r1 == 0 else -1)
    p1 = s.dwPacketNumber if r1 == 0 else -1
    print("%-6.1f %-7d %-7d %-9d %-9d" % (time.time() - t0, lt0[-1], lt1[-1], p0, p1))
    time.sleep(0.1)

print("\nslot0 LT 取值集合:", sorted(set(lt0)))
print("slot1 LT 取值集合:", sorted(set(lt1)))
for name, arr in (("slot0", lt0), ("slot1", lt1)):
    hi = sum(1 for v in arr if v >= 30)
    if 0 < hi < len(arr):
        print("  → %s 有 %d/%d 个采样 LT>=30 且在跳变 = 虚拟设备（假半扣）" % (name, hi, len(arr)))
    elif hi == len(arr):
        print("  → %s 全程 LT>=30 = 真实扳机被按住？" % name)
    else:
        print("  → %s 全程 LT<30 = 更像物理手柄（没被按）" % name)
