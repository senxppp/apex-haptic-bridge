"""延迟测量：播 8 连击点击音，量「音频起声 → dongle 写入」。

原理：k5_probe.log 每 0.1s 一行，其中 `writes` 是累计写入次数。10ms 轮询该文件，
计数变化的那一瞬就是一次真实写入；再与音频里已知的点击时刻相减，得到端到端延迟。
精度受限于轮询间隔（~10ms）与引擎自身的帧结构（0x57 一包 = 64ms 音频）。

用法：
    python measure_latency.py                     # 用默认 _click_8x.wav
    python measure_latency.py --iv 0.03           # 先把 k5_min_iv 热改成 0.03 再测
    python measure_latency.py --rounds 3

前置：链路必须在跑（app.exe / server.exe / k5_writer.exe），且不是在静音状态。
"""
import argparse
import json
import os
import statistics
import sys
import threading
import time
import winsound

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = os.path.join(BASE, "八爪鱼k5", "k5_probe.log")
CFG = os.path.join(BASE, "八爪鱼k5", "motor_config.json")
WAV = os.path.join(BASE, "_click_8x.wav")
CLICKS = [1.0 + i * 0.6 for i in range(8)]


def set_iv(v):
    d = json.load(open(CFG, encoding="utf-8"))
    d["k5_min_iv"] = v
    tmp = CFG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, CFG)
    time.sleep(3.0)          # 等 server 的热加载 watcher 生效


def read_probe():
    try:
        with open(PROBE, "r", encoding="utf-8", errors="replace") as f:
            return json.loads(f.readlines()[-1])
    except Exception:
        return None


def one_round(seconds=6.6):
    samples = []
    t0 = time.time()
    th = threading.Thread(target=lambda: winsound.PlaySound(WAV, winsound.SND_FILENAME))
    th.start()
    while time.time() - t0 < seconds:
        d = read_probe()
        if d:
            samples.append((time.time() - t0, d["writes"], d["fail"], d["level"][0]))
        time.sleep(0.008)
    th.join()
    winsound.PlaySound(None, winsound.SND_PURGE)

    events, last = [], None
    for t, w, fail, lv in samples:
        if last is None or w > last:
            events.append((t, lv))
        last = w
    lv = [e[1] for e in events] or [0]
    # 旧式 max(30, 中位×2) 在"衰减帧也很响"时会算出 >255 的门限 → 一个都匹配不上，
    # 整轮返回空（2026-11-21 实测 3 轮里 2 轮空）。封顶 200 既不会漏掉起手，
    # 也仍然高于大部分衰减帧。
    thresh = max(30, min(200, statistics.median(lv) * 2))
    lats = []
    for tc in CLICKS:
        cand = [e for e in events if tc - 0.05 <= e[0] <= tc + 0.65 and e[1] >= thresh]
        if cand:
            lats.append((cand[0][0] - tc) * 1000)
    rate = (samples[-1][1] - samples[0][1]) / (samples[-1][0] - samples[0][0])
    return lats, rate, samples[-1][2], lv


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iv", type=float, default=None, help="先热改 k5_min_iv")
    ap.add_argument("--rounds", type=int, default=2)
    a = ap.parse_args()

    if not os.path.exists(WAV):
        sys.exit("缺少 %s（8 连击点击音）" % WAV)
    if a.iv is not None:
        set_iv(a.iv)
        print("k5_min_iv 已设为 %.3f" % a.iv)

    all_lat, last = [], None
    for i in range(a.rounds):
        lats, rate, fails, lv = one_round()
        all_lat += lats
        last = (rate, fails, lv)
        print("第 %d 轮：延迟 %s ms | 写频 %.1f/s | 失败 %d | level 中位 %d 最大 %d"
              % (i + 1, [round(x) for x in sorted(lats)], rate, fails,
                 statistics.median(lv), max(lv)))
    if all_lat:
        all_lat.sort()
        print("\n端到端延迟：中位 %.0f ms  最小 %.0f  最大 %.0f  样本 %d"
              % (statistics.median(all_lat), all_lat[0], all_lat[-1], len(all_lat)))
    else:
        print("没有测到有效写入，检查链路是否在跑 / 是否静音")


if __name__ == "__main__":
    main()
