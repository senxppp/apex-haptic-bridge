#!/usr/bin/env python3
"""手柄热插拔自动重载守护（脚本链路入口 · P4 · 2026-11-15）。

监听真手柄 VID_37D7&PID_2501 的「拔出 → 插回」，自动执行实战验证过的
reload_chain.py，替代「手柄关机/离开过，回来必手动重载」这条手工步骤。

用法：
    python -I hotplug_reload.py                  # 常驻守护
    python -I hotplug_reload.py --dry-run --duration 20   # 只观察不重载
    python -I hotplug_reload.py --no-cm         # 强制纯轮询（排障用）

安全：只认 2501 真手柄；必须有 在线→离线→在线 完整边沿；冷却 240s；
      reload_chain 的进程清理规则不含 hotplug，不会杀掉守护自己。
必须用 Python 3.12（PATH 上的 3.13 缺 hidapi，会退化到 PowerShell 探测）。
"""
import argparse
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)          # -I 隔离模式下也能 import 同目录模块

import hotplug_core                  # noqa: E402

# 用「启动本守护的同一个解释器」去跑 reload_chain.py，换机器不依赖硬编码路径；
# reload_chain.py 内部 spawn 服务端时仍会用它自己那份已实证的 3.12 全路径。
PY = sys.executable
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
RELOAD_TIMEOUT = 420                 # reload_chain 实测约 2.5 分钟，留足余量

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def do_reload(log):
    """在工作线程里执行完整重载编排（与手动一键重载完全同一份代码）。"""
    log("开始 reload_chain.py …")
    try:
        r = subprocess.run(
            [PY, "-I", os.path.join(BASE, "reload_chain.py")],
            cwd=BASE, capture_output=True, timeout=RELOAD_TIMEOUT,
            creationflags=NO_WINDOW)
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        log("reload_chain 退出码=%s" % r.returncode)
        for line in (out + err).strip().splitlines()[-6:]:
            log("  | " + line[:180])
    except subprocess.TimeoutExpired:
        log("reload_chain 超时（%ds），链路状态请手动核对" % RELOAD_TIMEOUT)
    except Exception as e:
        log("reload_chain 调用失败: %r" % e)


def main():
    ap = argparse.ArgumentParser(description="八爪鱼 k5 手柄热插拔自动重载守护")
    ap.add_argument("--dry-run", action="store_true", help="只记录判定，不实际执行重载")
    ap.add_argument("--duration", type=float, default=0,
                    help="运行多少秒后退出（0=常驻，用于测试）")
    ap.add_argument("--stable", type=float, default=hotplug_core.DEFAULT_STABLE_S)
    ap.add_argument("--grace", type=float, default=hotplug_core.DEFAULT_GRACE_S)
    ap.add_argument("--cooldown", type=float, default=hotplug_core.DEFAULT_COOLDOWN_S)
    ap.add_argument("--poll", type=float, default=hotplug_core.DEFAULT_POLL_S)
    ap.add_argument("--no-cm", action="store_true",
                    help="禁用 CM_Register_Notification，强制纯轮询")
    a = ap.parse_args()

    w = hotplug_core.HotplugWatcher(
        base=BASE, on_trigger=lambda: do_reload(w.log),
        log_path=os.path.join(BASE, "hotplug.log"),
        dry_run=a.dry_run, stable_s=a.stable, grace_s=a.grace,
        cooldown_s=a.cooldown, poll_s=a.poll,
        use_cm=not a.no_cm)
    w.run(duration=(a.duration or None))
    return 0


if __name__ == "__main__":
    sys.exit(main())
