# frame_guard_core.py — 停帧即时检测与分级自愈（2026-11-15）
#
# 背景（实机实证）：
#   引擎输出挂起时「进程全活着、3241/18757 都通」，但 0x57 帧流停发，
#   `framesSent` 钉死、writeFailures 累积。旧的 auto_keepalive 靠 stats.log
#   （20s 才写一行、且仅帧数变化时写）的 mtime >45s 判死，实际恢复延迟
#   可达 45~65s。用户要的是「一停帧就立刻快速重连」。
#
# 关键发现：k5_probe.log 由 k5_writer 每收到一帧就累计、每秒落一行 JSON。
#   引擎只要还在发帧，去重 TTL(1s) 也会让归零帧每秒重发 → 探针持续刷新；
#   引擎一挂起，0x57 停发 → 探针立刻停更。所以它的 mtime 是天然的
#   **1 秒粒度震动链路心跳**，无需改动 server.exe、无需重启链路。
#
# 分级处置：停帧超阈值 → quick_cycle.py（实测 1.2s 救活，挂起计数清零）
#   → 4 秒后仍未恢复 → reload_chain.py（整套重载，约 3.5 分钟）。
# 保守护栏：链路未启动 / 正在整套重载 / 冷却期内，一律不动作。
import os
import socket
import subprocess
import sys
import time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 心跳源优先级：越靠前粒度越细。stats.log 仅 20s 写一行，降级时给 stall 加补偿。
SOURCES = (
    ("frame_hb", 0.0),        # 预留：未来 server 侧 0.4s 心跳（若存在则优先）
    ("k5_probe.log", 0.0),    # 事实来源：dongle 实际写每秒一行
    ("stats.log", 17.0),      # 降级兜底：仅帧数变化时写，阈值需 +17s
)


def heartbeat_ages(base, now=None):
    """纯读文件 mtime，返回 {name: age_seconds or None}。异常一律视为缺失。"""
    if now is None:
        now = time.time()
    out = {}
    for name, _ in SOURCES:
        try:
            out[name] = now - os.path.getmtime(os.path.join(base, name))
        except OSError:
            out[name] = None
    return out


def pick_source(ages):
    """选主心跳源：按**新鲜度**选，不是按表序存在性。

    旧实现返回表里第一个"存在"的文件，于是：`k5_probe.log` 是上一次运行留下的
    残骸（5 天前的 mtime）、`stats.log` 是这次运行刚写的——它仍然选前者，
    静默期（没有音频 → 没有写入 → 探针不落行）被判成"停帧 999s"，
    守护每 45s 摘挂一次设备，而链路其实完全正常。2026-11-20 实机复现。

    改成取 age 最小者：有写入时探针（0.1s 粒度）照旧胜出；没写入时自动降级到
    stats.log（20s 粒度，阈值补偿 17s），静默不再等于故障。全缺失 -> (None, inf, 0)。
    """
    best = None
    for name, extra in SOURCES:
        age = ages.get(name)
        if age is None:
            continue
        if best is None or age < best[1]:
            best = (name, age, extra)
    if best is None:
        return None, float("inf"), 0.0
    return best


class FrameState:
    """纯状态机（无 I/O，可离线完整回归）。

    observe() 返回决策：
      skip_no_link  链路没起（端口不通）——不动作，避免无意义 pnputil
      skip_busy     正在跑整套重载——不并发操作同一套设备
      healthy       心跳新鲜
      rearmed       帧流已稳定足够久，退避冷却复位
      wait_confirm  刚停帧，连续确认中（去抖）
      cooldown      冷却期内
      trigger       立即触发快速重连
    """

    def __init__(self, stall=4.0, cooldown=45.0, confirm=2,
                 max_cooldown=180.0, rearm_after=60.0):
        self.stall = stall
        self.base_cooldown = cooldown
        self.cooldown = cooldown
        self.max_cooldown = max_cooldown
        self.confirm = max(1, confirm)
        self.rearm_after = rearm_after
        self.last_trigger = -1e9
        self._stall_hits = 0
        self._healthy_since = None
        self.consecutive = 0

    def observe(self, now, age, stall, link_alive=True, busy=False):
        if not link_alive:
            self._stall_hits = 0
            self._healthy_since = None
            return "skip_no_link"
        if busy:
            self._stall_hits = 0
            self._healthy_since = None
            return "skip_busy"
        if age <= stall:
            self._stall_hits = 0
            if self._healthy_since is None:
                self._healthy_since = now
            if (now - self._healthy_since >= self.rearm_after
                    and (self.cooldown != self.base_cooldown or self.consecutive)):
                self.cooldown = self.base_cooldown
                self.consecutive = 0
                return "rearmed"
            return "healthy"
        self._healthy_since = None
        # 冷却优先于去抖：刚处置完的短期内再停帧，直接判冷却；
        # 冷却期本身已等了几十秒，结束时无需再重新去抖，可直接再触发。
        if now - self.last_trigger < self.cooldown:
            self._stall_hits = self.confirm
            return "cooldown"
        self._stall_hits += 1
        if self._stall_hits < self.confirm:
            return "wait_confirm"
        return "trigger"

    def on_trigger(self, now):
        """连续触发（多半是无音源静默期反复挂起）→ 指数退避冷却，避免刷屏/振荡。"""
        self.last_trigger = now
        self._stall_hits = 0
        self.consecutive += 1
        if self.consecutive >= 2:
            step = min(self.consecutive - 1, 2)
            self.cooldown = min(self.max_cooldown,
                                self.base_cooldown * (2 ** step))

    def on_stable(self):
        """外部确认帧流长期稳定后手工复位（可选）。"""
        self.cooldown = self.base_cooldown
        self.consecutive = 0


def port_open(port, host="127.0.0.1", timeout=0.3):
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((host, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def link_alive():
    """链路整体是否起来了：usbip 服务端 3241 + 引擎 API 18757。
    注意：引擎「输出挂起」时这两个端口仍通，所以它们只用于判断
    「链路是否启动」，不用于判断「是否停帧」（那由心跳负责）。"""
    return port_open(3241) and port_open(18757)


class Guard:
    """把状态机接到真实文件、端口与处置脚本上。动作可被测试替换。"""

    def __init__(self, base, stall=4.0, cooldown=45.0, poll=0.5,
                 confirm=2, dry_run=False, logger=None,
                 force_stall=0.0, settle=4.0):
        self.base = base
        self.poll = poll
        self.dry_run = dry_run
        self.force_stall = force_stall       # 实机验收：启动后前 N 秒伪造停帧
        self.settle = settle                 # 快速重连后等待帧流恢复的秒数
        self.state = FrameState(stall=stall, cooldown=cooldown, confirm=confirm)
        self.log = logger or (lambda msg: None)
        self._busy_cache = (0.0, False)
        self._started = time.time()
        self._alive_touch = 0.0
        self.triggers = 0
        self.recovered = 0
        self.escalations = 0

    # ---- I/O 层（测试可覆盖） ----
    def ages(self):
        return heartbeat_ages(self.base)

    def alive(self):
        return link_alive()

    def busy(self):
        """整套重载编排是否正在跑（只在触发决策附近低频调用，带 2s 缓存）。"""
        now = time.time()
        if now - self._busy_cache[0] < 2.0:
            return self._busy_cache[1]
        found = False
        try:
            cmd = ("Get-CimInstance Win32_Process -Filter \"Name like '%python%'\" | "
                   "Where-Object { $_.CommandLine -like '*reload_chain.py*' } | "
                   "Select-Object -First 1 -ExpandProperty ProcessId")
            r = subprocess.run(["powershell", "-NoProfile", "-Command", cmd],
                               capture_output=True, text=True, timeout=12,
                               creationflags=NO_WINDOW)
            found = bool((r.stdout or "").strip())
        except Exception:
            found = False
        self._busy_cache = (now, found)
        return found

    def quick_cycle(self):
        if self.dry_run:
            self.log("[dry-run] 将执行 quick_cycle.py（设备快速重连）")
            return True
        try:
            r = subprocess.run([sys.executable, "-I", "quick_cycle.py"],
                               cwd=self.base, capture_output=True, timeout=90,
                               creationflags=NO_WINDOW)
            self.log("quick_cycle 退出码=%d" % r.returncode)
            return r.returncode == 0
        except Exception as e:
            self.log("quick_cycle 异常: %r" % e)
            return False

    def full_reload(self):
        if self.dry_run:
            self.log("[dry-run] 将执行 reload_chain.py（整套重载）")
            return True
        try:
            r = subprocess.run([sys.executable, "-I", "reload_chain.py"],
                               cwd=self.base, capture_output=True, timeout=360,
                               creationflags=NO_WINDOW)
            self.log("reload_chain 退出码=%d" % r.returncode)
            return r.returncode == 0
        except Exception as e:
            self.log("reload_chain 异常: %r" % e)
            return False

    def sleep(self, sec):
        time.sleep(sec)

    def touch_alive(self):
        """每秒 touch guard.alive：让 app.exe 的内联保活知道外部守护在场并让位，
        避免两条路径同时对同一套设备做 quick_cycle。文件不存在时惰性创建。"""
        now = time.time()
        if now - self._alive_touch < 1.0:
            return
        try:
            p = os.path.join(self.base, "guard.alive")
            if not os.path.isfile(p):
                open(p, "w").close()
            os.utime(p, None)
            self._alive_touch = now
        except Exception:
            pass

    # ---- 主循环 ----
    def step(self, now=None):
        now = time.time() if now is None else now
        ages = self.ages()
        src, age, extra = pick_source(ages)
        stall = self.state.stall + extra
        if self.force_stall and now - self._started < self.force_stall:
            age = 999.0  # 仅验收用：人为制造停帧，走完整触发链
        # busy 查询要起一次 PowerShell，只在疑似停帧时才做；帧流正常时热路径零子进程
        busy = self.busy() if age > stall else False
        decision = self.state.observe(now, age, stall,
                                      link_alive=self.alive(), busy=busy)
        return decision, src, age, stall, ages

    def fire(self, src, age, stall):
        self.triggers += 1
        self.state.on_trigger(time.time())
        self.log("[guard] 停帧判定：源=%s age=%.1fs 阈值=%.1fs → 立即设备快速重连"
                 % (src, age, stall))
        ok = self.quick_cycle()
        self.sleep(self.settle)
        _, age2, _ = pick_source(self.ages())
        if not self.force_stall and age2 <= stall + 2:
            self.recovered += 1
            self.log("[guard] 帧流已恢复（心跳 age=%.1fs），无需整套重载" % age2)
            return True
        if self.force_stall:
            self.log("[guard] 验收注入模式：跳过真实性恢复判定，不升级整套重载")
            return True
        self.escalations += 1
        self.log("[guard] 快速重连后仍无帧（age=%.1fs）→ 升级整套重载" % age2)
        self.full_reload()
        return False

    def run(self, stop=None, max_seconds=None):
        t0 = time.time()
        last_report = 0.0
        self.log("[guard] 启动：stall=%.1fs cooldown=%.1fs poll=%.2fs confirm=%d dry_run=%s"
                 % (self.state.stall, self.state.cooldown, self.poll,
                    self.state.confirm, self.dry_run))
        while True:
            if stop is not None and stop.is_set():
                break
            if max_seconds is not None and time.time() - t0 >= max_seconds:
                break
            self.touch_alive()
            try:
                d, src, age, stall, _ = self.step()
                if d == "trigger":
                    self.fire(src, age, stall)
                elif d == "rearmed":
                    self.log("[guard] 帧流稳定，冷却已复位为 %.0fs" % self.state.base_cooldown)
                elif d == "cooldown" and time.time() - last_report > 20:
                    last_report = time.time()
                    self.log("[guard] 停帧持续（age=%.1fs），冷却中 %.0fs"
                             % (age, self.state.cooldown - (time.time() - self.state.last_trigger)))
            except Exception as e:
                self.log("[guard] err: %r" % e)
            self.sleep(self.poll)
        self.log("[guard] 退出：累计触发 %d 次 / 快速恢复 %d 次 / 升级重载 %d 次"
                 % (self.triggers, self.recovered, self.escalations))
