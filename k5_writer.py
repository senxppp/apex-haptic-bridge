# k5_writer.py — k5 dongle 独立写进程
# 目的：hid write 是持 GIL 的 C 调用，dongle 慢写（实测 21ms）会阻塞服务端 urb 线程
# → 引擎写入超 8ms 预算 → 写失败累积 → 引擎弃权实时流（手柄冻结）。
# 移到独立进程后与 urb 线程的 GIL 彻底隔离，dongle 再慢也不影响引擎会话。
# 协议：stdin 读 [长度字节][帧内容]，逐帧写 HID；慢写记录 k5slow.log。
import json
import os
import sys
import time

import hid

VID, PID = 0x37D7, 0x2501
SLOW_MS = 10.0
# 慢写日志写到 exe / 脚本同目录（原写死本机绝对路径，换机器后诊断日志会丢）
if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(_BASE, "k5slow.log")
# 无人值守探针：每次真实 HID 写的累计/电平/耗时，每秒落一行 JSON。
# 这是离物理震动最近的一层软件证据（USB HID 输出报告单向、手柄不上报电机状态）。
PROBE = os.path.join(_BASE, "k5_probe.log")


def write_probe(st):
    rec = {
        "ts": time.strftime("%H:%M:%S"), "writes": st["writes"],
        "ok": st["ok"], "fail": st["fail"],
        # level 用「本刷新周期峰值」，不能用最后一帧：音频带节拍时最后一帧
        # 几乎总是归零帧，会把满幅震动误报成「全程没震」（01:4x 实测踩过）。
        "last_cmd": "0x%02X" % st["last_cmd"], "level": st["peak_level"],
        "nz_period": st["nz_period"], "nz": st["nz"],
        "max_level": st["max_level"], "last_hex": st["last_hex"],
        # 0x51 扳机命令时间线：state-change 才发，必须单独记，否则被每秒
        # 最后一次 0x12 覆盖，看不出「爆发→回落」或「挂起后不回落」。
        "trig_period": len(st["trig_log"]), "trig_log": st["trig_log"][:8],
        "max_ms": round(st["max_ms"], 1),
        "avg_ms": round(st["sum_ms"] / max(1, st["writes"] + st["fail"]), 2),
    }
    try:
        with open(PROBE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def open_dev():
    for d in hid.enumerate(VID, PID):
        if d.get("usage_page") == 0xFFA0:
            dev = hid.device()
            dev.open_path(d["path"])
            dev.set_nonblocking(1)
            dev.write([0x03, 0x5A, 0xA5, 0x12, 0x04, 0, 0])  # 启动清电平
            # 扳机常态化：双侧轻阻力（mode 1 Race，官方刹车阻力参数 [0,1,0,0]）
            # 用户要求恒定阻力（力反馈到位需要时间，动态切换很吵），此后不再发扳机命令
            for side in (1, 2):
                frame = bytearray(32)
                frame[0], frame[1], frame[2], frame[3], frame[4] = 0x03, 0x5A, 0xA5, 0x51, 8
                frame[5:13] = (1, side, 1, 0, 1, 0, 0, 0)
                ck = 0
                for i in range(2, 31):
                    ck = (ck + frame[i]) & 0xFF
                frame[31] = ck
                dev.write(list(frame))
            return dev
    return None


def log_slow(ms):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %.1fms\n" % (time.strftime("%H:%M:%S"), ms))
    except Exception:
        pass


def main():
    dev = open_dev()
    streak = 0
    st = {"writes": 0, "ok": 0, "fail": 0, "max_ms": 0.0, "sum_ms": 0.0,
          "last_cmd": 0, "level": [0, 0], "nz": 0, "max_level": [0, 0],
          "peak_level": [0, 0], "nz_period": 0, "trig_log": [],
          "last_hex": "", "last_flush": time.time()}
    stdin = sys.stdin.buffer
    while True:
        b = stdin.read(1)
        if not b:
            break
        n = b[0]
        frame = stdin.read(n)
        if len(frame) < n:
            break
        if dev is None:
            dev = open_dev()
            if dev is None:
                time.sleep(0.5)
                continue
        t0 = time.time()
        try:
            r = dev.write(list(frame))
        except Exception:
            r = 0
        ms = (time.time() - t0) * 1000.0
        if ms > SLOW_MS:
            log_slow(ms)
        # 探针累计（纯内存；文件 I/O 每秒最多一次，不给写进程添阻塞）
        st["writes"] += 1
        st["sum_ms"] += ms
        st["max_ms"] = max(st["max_ms"], ms)
        st["last_cmd"] = frame[3] if len(frame) > 3 else 0
        st["last_hex"] = bytes(frame).hex()
        if len(frame) > 7 and frame[3] == 0x51:
            # payload: [apply, side, mode, start, p1, p2, p3, match]
            # 0x51 载荷 = [apply, side, mode, start, p1, p2, p3, match]
            # p1 是 mode1 的「阻尼力度」/ mode2 的起始系数；p2 是机器枪强度。
            # 两者都记，否则力包络层（改 p1）在日志里完全看不见。
            st["trig_log"].append("%s m%s/s%s f%s p%s,%s" % (
                time.strftime("%H:%M:%S"), frame[7], frame[6],
                frame[9], frame[10], frame[11]))
        if len(frame) > 6 and frame[3] == 0x12:
            st["level"] = [frame[5], frame[6]]
            if frame[5] or frame[6]:
                st["nz"] += 1
                st["nz_period"] += 1
                st["max_level"] = [max(st["max_level"][0], frame[5]),
                                   max(st["max_level"][1], frame[6])]
                st["peak_level"] = [max(st["peak_level"][0], frame[5]),
                                    max(st["peak_level"][1], frame[6])]
        if r and r > 0:
            st["ok"] += 1
            streak = 0
        else:
            st["fail"] += 1
            streak += 1
            if streak >= 50:
                try:
                    dev.close()
                except Exception:
                    pass
                dev = None
                streak = 0
        now = time.time()
        if now - st["last_flush"] >= 1.0:
            write_probe(st)
            st["last_flush"] = now
            st["peak_level"] = [0, 0]
            st["nz_period"] = 0
            st["trig_log"] = []


main()
