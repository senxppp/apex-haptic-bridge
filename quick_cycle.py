#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""设备快速重连（独立脚本，供 keepalive / 人工排障调用）

为什么是独立进程：`frame_guard_core.Guard.quick_cycle()` 判定停帧后会用
`python -I quick_cycle.py` 拉起它。用子进程而不是 in-process，是因为这条路
要跑 pnputil / usbip 这类会阻塞、可能弹控制台的命令，卡死时不能连带把守护
线程一起拖住——超时杀掉即可。

做三件事（顺序不能换）：
  1. usbip detach -p 1                       摘掉现有虚拟设备
  2. pnputil /remove-device USB\\VID_37D7&PID_2502
                                             删掉设备节点（只 detach 不删节点，
                                             重新 attach 会复用坏句柄）
  3. usbip -t 3241 attach -r 127.0.0.1 -b 1-1
                                             挂回服务端；stdout 含 "attached" 即成功

退出码：0 = 已挂上；1 = 命令跑了但没挂上；2 = 配置缺失（app.ini 里没有 usbip）
`--dry-run` 只打印将要执行的命令，不做任何设备操作（改配置后先这样验一遍）。

注意：pnputil 删设备节点需要管理员权限。以普通权限运行时本脚本会照常跑完，
但 attach 大概率失败——调用方（keepalive）会据此升级到 reload_chain，属预期。
"""
import configparser
import os
import subprocess
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

DETACH_ARGS = ["detach", "-p", "1"]
NODE = r"USB\VID_37D7&PID_2502"
ATTACH_ARGS = ["-t", "3241", "attach", "-r", "127.0.0.1", "-b", "1-1"]
SETTLE_S = 1.0


def out(s):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print(s, flush=True)


def load_usbip():
    """只读 app.ini 的 usbip 项；读不到就返回空串（由调用方报错退出）"""
    ini = configparser.ConfigParser()
    try:
        ini.read(os.path.join(BASE, "app.ini"), encoding="utf-8")
        if ini.has_section("paths"):
            return (ini.get("paths", "usbip", fallback="") or "").strip()
    except Exception as e:
        out("[warn] app.ini 解析失败: %r" % (e,))
    return ""


def run(args, timeout):
    """统一抑制 console 子进程弹窗；windowed 宿主下不留黑框"""
    return subprocess.run(args, capture_output=True, timeout=timeout,
                          creationflags=NO_WINDOW,
                          encoding="utf-8", errors="replace")


def main(argv):
    dry = "--dry-run" in argv[1:]
    usbip = load_usbip()
    if not usbip:
        out("[!!] app.ini 缺少 usbip 路径（参考 app.ini.example 填写）")
        return 2
    if not os.path.exists(usbip):
        out("[!!] usbip 不存在：%s" % usbip)
        return 2

    steps = [(usbip, DETACH_ARGS, 30),
             ("pnputil", ["/remove-device", NODE], 30),
             (usbip, ATTACH_ARGS, 60)]
    if dry:
        for exe, args, to in steps:
            out("[dry-run] %s %s (timeout=%ss)" % (exe, " ".join(args), to))
        out("[dry-run] 中间 sleep %.1fs；未触碰任何设备" % SETTLE_S)
        return 0

    r = run([usbip] + DETACH_ARGS, 30)
    out("detach: " + ((r.stdout or "") + (r.stderr or "")).strip()[:100])

    try:
        r = run(["pnputil", "/remove-device", NODE], 30)
        out("remove: " + " ".join(((r.stdout or "") + (r.stderr or "")).split())[:120])
    except Exception as e:
        # 没有管理员权限时这里常抛；继续 attach 仍有概率成功，不当作致命错误
        out("remove 跳过: %r" % (e,))
    time.sleep(SETTLE_S)

    r = run([usbip] + ATTACH_ARGS, 60)
    txt = ((r.stdout or "") + (r.stderr or "")).strip()
    out("attach: " + txt[:100])
    ok = "attached" in txt.lower()
    out("结果: " + ("已挂上" if ok else "未挂上"))
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except Exception as e:
        out("[!!] 异常: %r" % (e,))
        sys.exit(1)
