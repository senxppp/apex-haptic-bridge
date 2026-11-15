# auto_keepalive.py — 停帧即时自愈维持器（v2 · 2026-11-15）
#
# v1：stats.log mtime >45s → 设备快速重连（20s 写周期 + 45s 阈值，恢复要 45~65s）
# v2：k5_probe.log 1s 心跳 → 停帧 ~5s 内立即 quick_cycle（1.2s 救活），
#     4s 后未恢复才升级整套 reload_chain。逻辑在 frame_guard_core.py，
#     本文件只做入口与参数解析，保持文件名不变（启停脚本无需改动）。
#
# 用法：
#   python -I auto_keepalive.py                     # 默认 stall=4s / cooldown=45s
#   python -I auto_keepalive.py --stall 3 --cooldown 60
#   python -I auto_keepalive.py --dry-run          # 只打日志不动设备
#   python -I auto_keepalive.py --self-test 12     # 前 12s 人为注入停帧，走一次真实触发链
# 环境变量亦可覆盖：FRAME_STALL / FRAME_COOLDOWN / FRAME_POLL
import argparse
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
# -I 隔离模式不把脚本目录放进 sys.path，必须显式补上（run_live_test 踩过这个坑）
if BASE not in sys.path:
    sys.path.insert(0, BASE)

LOG = os.path.join(BASE, "keepalive.log")


def log(msg):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), msg)
    try:
        print(line, flush=True)
    except Exception:
        pass
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def main():
    ap = argparse.ArgumentParser(description="停帧即时自愈维持器")
    ap.add_argument("--stall", type=float,
                    default=float(os.environ.get("FRAME_STALL", "4")),
                    help="心跳停滞超过该秒数判定停帧（默认 4）")
    ap.add_argument("--cooldown", type=float,
                    default=float(os.environ.get("FRAME_COOLDOWN", "45")),
                    help="两次处置最小间隔（默认 45s，连续触发会指数退避）")
    ap.add_argument("--poll", type=float,
                    default=float(os.environ.get("FRAME_POLL", "0.5")),
                    help="轮询间隔秒（默认 0.5）")
    ap.add_argument("--confirm", type=int, default=2,
                    help="连续几次采样停帧才触发（去抖，默认 2）")
    ap.add_argument("--dry-run", action="store_true", help="只打日志，不动设备")
    ap.add_argument("--self-test", type=float, default=0.0, metavar="SECONDS",
                    help="启动后前 N 秒伪造停帧，实机验收触发链用")
    ap.add_argument("--max-seconds", type=float, default=0.0, metavar="SECONDS",
                    help="运行 N 秒后自动退出（验收/短测用；0=常驻）")
    a = ap.parse_args()

    from frame_guard_core import Guard
    g = Guard(BASE, stall=a.stall, cooldown=a.cooldown, poll=a.poll,
              confirm=a.confirm, dry_run=a.dry_run, logger=log,
              force_stall=a.self_test)
    log("维持器 v2：心跳=k5_probe.log(1s) → 停帧 %.0fs 触发快速重连；"
        "旧 45s/20s 判据仅作降级兜底" % a.stall)
    try:
        g.run(max_seconds=a.max_seconds or None)
    except KeyboardInterrupt:
        log("手动停止")


if __name__ == "__main__":
    main()
