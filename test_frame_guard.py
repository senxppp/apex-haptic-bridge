# test_frame_guard.py — 停帧即时自愈的离线回归（不启动引擎、不挂设备、不动真链路）
#
# 直接运行：python -I test_frame_guard.py
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import frame_guard_core as fg

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print("%s %s%s" % ("[PASS]" if cond else "[FAIL]", name,
                       ("  " + str(detail)) if detail else ""))


def test_sources():
    import tempfile
    d = tempfile.mkdtemp(prefix="fg_src_")
    now = 1000.0
    # 全缺失
    ages = fg.heartbeat_ages(d, now)
    check("无心跳文件时全部为 None", all(v is None for v in ages.values()), ages)
    check("全缺失 pick_source 返回 inf", fg.pick_source(ages)[1] == float("inf"))
    # 只有 stats.log
    p = os.path.join(d, "stats.log")
    open(p, "w").write("x")
    os.utime(p, (now - 30, now - 30))
    ages = fg.heartbeat_ages(d, now)
    src, age, extra = fg.pick_source(ages)
    check("降级到 stats.log", src == "stats.log" and abs(age - 30) < 0.01, (src, age, extra))
    check("stats.log 降级补偿 +17s", extra == 17.0)
    # probe 出现，优先级更高
    p2 = os.path.join(d, "k5_probe.log")
    open(p2, "w").write("{}")
    os.utime(p2, (now - 1, now - 1))
    src, age, extra = fg.pick_source(fg.heartbeat_ages(d, now))
    check("优先 k5_probe.log（1s 粒度）", src == "k5_probe.log" and abs(age - 1) < 0.01,
          (src, age))
    check("probe 源无阈值补偿", extra == 0.0)
    # 回归（2026-11-20 实机复现）：探针是上一次运行留下的残骸，stats 才是这次运行的。
    # 旧实现按表序返回第一个"存在"的文件 → 拿着 5 天前的 probe 判「停帧 999s」，
    # 静默期（没音频就没写入，探针不落行）被当成故障，守护每 45s 摘挂一次设备。
    os.utime(p2, (now - 999, now - 999))
    os.utime(p, (now - 5, now - 5))
    src, age, extra = fg.pick_source(fg.heartbeat_ages(d, now))
    check("残骸探针不压过新鲜 stats（静默≠停帧）",
          src == "stats.log" and abs(age - 5) < 0.01, (src, age))
    os.utime(p2, (now - 1, now - 1))
    # frame_hb 出现则最高优先
    p3 = os.path.join(d, "frame_hb")
    open(p3, "w").write("")
    os.utime(p3, (now - 0.4, now - 0.4))
    src, age, _ = fg.pick_source(fg.heartbeat_ages(d, now))
    check("预留 frame_hb 优先级最高", src == "frame_hb" and abs(age - 0.4) < 0.01,
          (src, age))
    for x in (p, p2, p3):
        os.remove(x)
    os.rmdir(d)


def test_state():
    s = fg.FrameState(stall=4, cooldown=45, confirm=2)
    check("心跳新鲜 → healthy", s.observe(0, 0.5, 4) == "healthy")
    check("刚停帧 → 连续确认去抖", s.observe(5, 5.0, 4) == "wait_confirm")
    check("第二次确认 → trigger", s.observe(5.5, 5.5, 4) == "trigger")
    s.on_trigger(5.5)
    check("触发后立刻进入冷却", s.observe(6, 20, 4) == "cooldown")
    check("冷却期内不重复触发", s.observe(20, 20, 4) == "cooldown")
    check("冷却结束且仍停帧 → 再触发", s.observe(51, 20, 4) == "trigger")
    s.on_trigger(51)
    check("连续触发后退避冷却翻倍", s.cooldown == 90, s.cooldown)
    s.observe(52, 0.1, 4)
    check("长期稳定后冷却复位", s.observe(52 + 61, 0.1, 4) == "rearmed")
    check("复位后 cooldown 回 45", s.cooldown == 45, s.cooldown)

    s2 = fg.FrameState(stall=4, cooldown=45, confirm=2)
    check("链路未启动 → 不动作", s2.observe(0, 999, 4, link_alive=False) == "skip_no_link")
    check("整套重载中 → 不并发", s2.observe(1, 999, 4, busy=True) == "skip_busy")
    check("护栏后计数器清零（不会攒出误触发）",
          s2.observe(2, 999, 4) == "wait_confirm")
    s2.observe(3, 0.2, 4)
    check("帧流恢复后计数器清零", s2.observe(4, 999, 4) == "wait_confirm")
    s3 = fg.FrameState(stall=4, cooldown=45, confirm=2, max_cooldown=100)
    for i in range(8):
        s3.observe(100 + i * 200, 999, 4)
        s3.on_trigger(100 + i * 200)
    check("退避有上限", s3.cooldown <= 100, s3.cooldown)


class FakeGuard(fg.Guard):
    """替换全部 I/O；时钟由虚拟序列驱动。"""

    def __init__(self, ages_seq, alive=True, busy=False, recovered_after_fire=True, **kw):
        super().__init__(base=".", logger=lambda m: None, settle=0, **kw)
        self.ages_seq = list(ages_seq)
        self.alive_val = alive
        self.busy_val = busy
        self.quick_calls = 0
        self.full_calls = 0
        self.recovered_after_fire = recovered_after_fire
        self._fired = False

    def ages(self):
        if self.ages_seq:
            a = self.ages_seq.pop(0)
        else:
            a = self.ages_seq_last if hasattr(self, "ages_seq_last") else {"k5_probe.log": 999.0}
        self.ages_seq_last = a
        return a

    def alive(self):
        return self.alive_val

    def busy(self):
        return self.busy_val

    def quick_cycle(self):
        self.quick_calls += 1
        self._fired = True
        return True

    def full_reload(self):
        self.full_calls += 1
        return True

    def sleep(self, sec):
        pass


def test_guard():
    stalled = {"k5_probe.log": 999.0}
    fresh = {"k5_probe.log": 0.3}
    # 停帧 → 触发 → 快速重连后恢复（不升级）
    g = FakeGuard([stalled, stalled, fresh], dry_run=False)
    d, src, age, stall, _ = g.step()
    check("Guard 首帧停帧去抖", d == "wait_confirm", d)
    d, _, _, _, _ = g.step()
    check("Guard 第二帧判定 trigger", d == "trigger", d)
    g.fire(src, age, stall)
    check("触发后只跑快速重连", g.quick_calls == 1 and g.full_calls == 0,
          (g.quick_calls, g.full_calls))
    check("恢复后不升级整套重载", g.recovered == 1, g.recovered)

    # 快速重连后仍停帧 → 升级整套重载
    g2 = FakeGuard([], dry_run=False)
    g2.ages = lambda: {"k5_probe.log": 999.0}
    g2.fire("k5_probe.log", 999.0, 4.0)
    check("未恢复时升级整套重载", g2.full_calls == 1 and g2.escalations == 1,
          (g2.full_calls, g2.escalations))

    # dry-run 不执行真命令（用真 Guard，动作是 shell）
    g3 = fg.Guard(base=".", dry_run=True, logger=lambda m: None, settle=0)
    g3.sleep = lambda s: None
    g3.fire("k5_probe.log", 999.0, 4.0)
    check("dry-run 不调用真实 quick_cycle", g3.triggers == 1)

    # 链路没起 → 绝不动作
    g4 = FakeGuard([stalled] * 6, alive=False)
    g4.step()
    d, _, _, _, _ = g4.step()
    check("链路未启动不触发", d == "skip_no_link", d)
    check("链路未启动零处置", g4.quick_calls == 0)

    # 正在整套重载 → 不并发
    g5 = FakeGuard([stalled] * 6, busy=True)
    g5.step()
    d, _, _, _, _ = g5.step()
    check("重载中不并发", d == "skip_busy", d)

    # stats.log 降级时阈值自动加 17s，不会把 20s 写周期误判成停帧
    g6 = FakeGuard([{"stats.log": 12.0}] * 3)
    d, _, _, stall, _ = g6.step()
    check("stats.log 降级阈值 = 4+17=21s", stall == 21.0, stall)
    check("12s 的 stats.log 不算停帧", d == "healthy", d)


def main():
    test_sources()
    test_state()
    test_guard()
    bad = [r for r in RESULTS if not r[1]]
    print("\n=== %d/%d PASS ===" % (len(RESULTS) - len(bad), len(RESULTS)))
    for r in bad:
        print("  FAIL:", r[0], r[2])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
