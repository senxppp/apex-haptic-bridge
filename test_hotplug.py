#!/usr/bin/env python3
"""热插拔自动重载（P4）离线回归 —— 不需要插拔手柄、不需要引擎、不改任何配置。

直接运行：python -I test_hotplug.py
全部状态推进用注入的 now_fn / present_fn 驱动，纯函数式验证边沿、去抖、冷却、
单飞、dry-run、2502 排除，外加一次本机真实 CM_Register_Notification 注册冒烟。
"""
import ctypes
import os
import subprocess
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import hotplug_core as hc

RESULTS = []
TMP_LOG = os.path.join(HERE, "_hotplug_test.log")


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))


def watcher(**kw):
    T = [1000.0]
    F = [0]

    def fire():
        F[0] += 1

    w = hc.HotplugWatcher(
        base=HERE, on_trigger=fire, log_path=TMP_LOG,
        now_fn=lambda: T[0], present_fn=lambda: 1,
        use_cm=False, **kw)
    return w, T, F


def main():
    # --- 1. 正常完整边沿：在线 → 拔出 → 插回稳定 → 恰好触发一次 ---
    w, T, F = watcher(dry_run=True)
    T[0] = 1000
    check("初始在线不触发", w.tick(T[0], 1) == "idle")
    T[0] = 1001
    check("持续在线保持 idle", w.tick(T[0], 1) == "idle")
    T[0] = 1002
    check("拔出瞬间未确认", w.tick(T[0], 0) == "absent")
    T[0] = 1003
    check("拔出不足 grace 不武装", w.tick(T[0], 0) == "absent")
    T[0] = 1004
    check("拔出满 grace 后武装", w.tick(T[0], 0) == "absent-armed")
    T[0] = 1005
    check("刚插回进入稳定确认", w.tick(T[0], 1) == "settling")
    T[0] = 1012
    check("稳定期未满不触发", w.tick(T[0], 1) == "settling")
    T[0] = 1013
    check("稳定期满触发一次", w.tick(T[0], 1) == "trigger" and w.triggers == 1)
    w.fire()
    check("dry-run 不执行重载回调", F[0] == 0)
    check("dry-run 不调用 reload_chain", not any("开始 reload_chain" in x for x in w.lines))
    T[0] = 1014
    check("触发后仍在线回到 idle", w.tick(T[0], 1) == "idle")

    # --- 2. 冷却：240s 内不重入，之后可以再触发 ---
    T[0] = 1015
    w.tick(T[0], 0)
    T[0] = 1017
    w.tick(T[0], 0)
    T[0] = 1018
    w.tick(T[0], 1)
    T[0] = 1026
    check("冷却期内被拦下", w.tick(T[0], 1) == "cooldown" and w.triggers == 1)
    T[0] = 1260
    check("冷却期满可再次触发", w.tick(T[0], 1) == "trigger" and w.triggers == 2)

    # --- 3. 初始离线：没有可靠在线边沿，绝不自动重载 ---
    w2, T2, F2 = watcher(dry_run=True)
    T2[0] = 1000
    w2.tick(T2[0], 0)
    T2[0] = 1010
    check("初始离线不会武装", w2.tick(T2[0], 0) == "absent")
    T2[0] = 1011
    check("首次上电不误触发", w2.tick(T2[0], 1) == "idle" and w2.triggers == 0)

    # --- 4. 稳定期内抖动：重新计时，不提前触发 ---
    w3, T3, _ = watcher(dry_run=True, stable_s=8.0, grace_s=2.0)
    T3[0] = 1000
    w3.tick(T3[0], 1)
    T3[0] = 1001
    w3.tick(T3[0], 0)
    T3[0] = 1003
    w3.tick(T3[0], 0)
    T3[0] = 1004
    check("抖动后仍处于稳定确认", w3.tick(T3[0], 1) == "settling")
    T3[0] = 1010
    w3.tick(T3[0], 1)
    T3[0] = 1011
    check("稳定期内再抖一次重新计时", w3.tick(T3[0], 0) in ("absent", "busy"))
    T3[0] = 1012
    w3.tick(T3[0], 1)
    T3[0] = 1019
    check("抖动后不会提前触发", w3.tick(T3[0], 1) == "settling" and w3.triggers == 0)

    # --- 5. 重载执行期间单飞 ---
    w4, T4, _ = watcher(dry_run=True)
    w4._busy = True
    T4[0] = 1000
    check("busy 期间不重入", w4.tick(T4[0], 1) == "busy")

    # --- 6. 只认真手柄 2501，明确排除虚拟设备 2502 ---
    fake = types.ModuleType("hid")
    fake.enumerate = lambda: [
        {"vendor_id": 0x37D7, "product_id": 0x2501},
        {"vendor_id": 0x37D7, "product_id": 0x2502},
        {"vendor_id": 0x37D7, "product_id": 0x2502},
        {"vendor_id": 0x054C, "product_id": 0x0CE6},
    ]
    old_hid = sys.modules.get("hid")
    sys.modules["hid"] = fake
    try:
        check("HID 枚举排除 2502 虚拟设备", hc.present_hid() == 1,
              "真手柄接口数=%d" % hc.present_hid())
    finally:
        if old_hid is None:
            sys.modules.pop("hid", None)
        else:
            sys.modules["hid"] = old_hid
    check("常量明确区分真/虚拟设备",
          hc.PID_REAL == 0x2501 and hc.PID_VIRT == 0x2502)

    # --- 7. CM_NOTIFY_FILTER 结构布局（x64 必须为 416 字节） ---
    if hc.CM_NOTIFY_FILTER is not None:
        sz = ctypes.sizeof(hc.CM_NOTIFY_FILTER)
        check("CM_NOTIFY_FILTER 布局正确", sz == 416, "sizeof=%d" % sz)
    else:
        check("CM_NOTIFY_FILTER 布局正确（非 Windows 跳过）", True, "skipped")

    # --- 8. 本机真实注册 CM_Register_Notification + 反注册 ---
    n = hc.CmNotifier()
    time.sleep(0.4)
    check("CM 通知注册或明确降级",
          n.ok or bool(n.error), "ok=%s error=%s actions=%s"
          % (n.ok, n.error, n.actions))
    if n.ok and n.actions:
        check("注册后收到现有设备接口通知", True, "actions=%s" % n.actions)
    n.close()
    check("反注册不抛异常", not n.ok)

    # --- 9. 真实设备探测函数可用（只断言返回 int，手柄可能没上电） ---
    fn = hc.make_present_fn()
    try:
        v = fn()
        check("设备探测函数可用", isinstance(v, int) and v >= 0,
              "%s → %d" % (fn.__name__, v))
    except Exception as e:
        check("设备探测函数可用", False, repr(e))

    # --- 10. 入口脚本 dry-run 短跑（禁用 CM，纯轮询，绝不触发重载） ---
    try:
        r = subprocess.run(
            [sys.executable, "-I", os.path.join(HERE, "hotplug_reload.py"),
             "--dry-run", "--duration", "3", "--no-cm", "--poll", "0.5"],
            cwd=HERE, capture_output=True, text=True, timeout=40,
            encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        check("入口脚本 dry-run 正常退出", r.returncode == 0, "exit=%d" % r.returncode)
        check("入口脚本不执行真实重载", "开始 reload_chain" not in out)
        check("入口脚本写出守护日志", "热插拔重载守护启动" in out, out[:120])
    except Exception as e:
        check("入口脚本 dry-run 正常退出", False, repr(e))

    # --- 汇总 ---
    bad = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        print("  %s %-38s %s" % ("PASS" if ok else "FAIL", name, detail))
    print("\n=== %d/%d PASS ===" % (len(RESULTS) - len(bad), len(RESULTS)))
    if os.path.isfile(TMP_LOG):
        try:
            os.remove(TMP_LOG)
        except OSError:
            pass
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
