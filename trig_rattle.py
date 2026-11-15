# trig_rattle.py — 「机枪抖动」v2：把飞智的 头/身/锁/释放 结构搬到 k5 的 mode 2
#
# 为什么要有这一层（2026-11-15 12:2x 结论）：
#   1. 扳机的**力**（mode 1 阻尼）只有手指按在行程中段才摸得到，放松搭着＝无感，
#      所以"顶手"不能当主通道；
#   2. mode 2「机枪」才是无论手指在哪都能感知的通道（空间站文案：机枪 = 震动强度 + 频率）；
#   3. 原来的 mode 2 只有"打一下"（固定 180ms），既没有头也没有身，更没有锁，
#      密集音乐会连成一串机关枪；飞智的扳机 DSP 是明确的四段结构。
#
# 飞智 trigger1「强劲冲击 / X-TRIGGER PUNCH」实测参数
# （来源：api-web.cdn.flydigi.com/realtime-audio/430/…/sekiro_430_2026092002.zip）：
#   gunHeadPunchDb   8     冲击头额外增益
#   gunHeadDecayMs   30    头衰减
#   gunHeadLockoutMs 70    头锁（这段时间不再起新头，防连打）
#   gunBodyHoldMs    150   身保持
#   gunBodyReleaseMs 120   身释放
#   composerKnee     0.5   软拐点
#   composerCeiling  0.8   上限
#   envelopeBands    1200-3k→80Hz(0.75) / 3-6k→110Hz(1.0) / 6-12k→130Hz(0.65)
#   outputGain       2.51 (+8dB)
# 对比 trigger2「柔和沉浸 / ACTION+MAGIC」：punch 仅 1.5dB、lockout 100ms、body 100ms、
#   强调 450–2500Hz、高频权重 0.25 —— 说明"强弱差异"几乎全来自头的增益与锁。
#
# k5 只有两个旋钮：p2=强度(55..200)、p3=频率(40..70)。映射：
#   包络(0..255) ──软拐点压缩──► p2
#   瞬态陡峭度 ──► p3（越陡越"尖"，对应飞智 6–12k 那一段的高载波）
#   头：p2 拉满 30ms → 回落身；锁 70ms 内不再起头
#   身：随包络连续变化，静音 120ms 后回到 mode 1 常态

DEFAULTS = {
    "tr_enable": 1,
    "tr_thr": 6,             # 进入抖动的包络门限（0..255）
    "tr_p2_base": 55,        # 最低强度（飞智"高速刹车"用的就是 55）
    "tr_p2_max": 200,        # 最高强度（飞智"换挡"用的就是 200）
    "tr_knee": 0.5,          # composer 软拐点
    "tr_ceiling": 0.8,       # composer 上限
    "tr_gain": 1.0,          # 包络→强度 斜率
    "tr_head_db": 8,         # 头冲增益（%），对应 gunHeadPunchDb
    "tr_head_ms": 30,        # 头衰减
    "tr_lockout_ms": 70,     # 头锁
    "tr_body_hold_ms": 150,  # 身保持
    "tr_release_ms": 120,    # 释放
    "tr_p3_min": 45,         # 最低频率档
    "tr_p3_max": 70,         # 最高频率档
    "tr_step": 12,           # p2 量化步长（减少写入）
    "tr_min_iv_ms": 140,     # 两次抖动写入最小间隔（预算留给握把）
    "tr_jump_bypass": 60,    # 超过这个跳变就无视间隔（只有"头"才够格，避免小摆动刷写）
    "tr_head_floor": 40,     # 起头的最低绝对电平（防止微弱声也被判成打击）
}

ACT_SET = "set"
ACT_REST = "rest"


def _clamp(x, lo, hi):
    return lo if x < lo else (hi if x > hi else x)


def compose(x, knee, ceiling):
    """飞智 composer 的软拐点：x∈[0,1] → [0,ceiling]，拐点以下线性、以上压缩。
    拐点处的连续性由 mid 保证，避免出现"台阶"。"""
    x = _clamp(float(x), 0.0, 1.0)
    knee = _clamp(float(knee), 0.02, 0.98)
    ceiling = _clamp(float(ceiling), 0.05, 1.0)
    if ceiling < knee:                     # 上限比拐点还低：退化成整体缩放
        return x * ceiling
    mid = knee + (ceiling - knee) * 0.5    # 拐点处的输出
    if x <= knee:
        return x * (mid / knee)
    return mid + (ceiling - mid) * ((x - knee) / (1.0 - knee))


class Rattle:
    """mode 2 抖动状态机。update(now, level) → ('set', p2, p3) / ('rest',) / None。"""

    def __init__(self, cfg_get):
        self._cfg = cfg_get
        self.env = 0.0
        self._prev = 0.0
        self._head_until = 0.0
        self._body_until = 0.0
        self._lock_until = 0.0
        self._last_active = -1e9
        self._on = False
        self.last_p2 = None
        self.last_p3 = None
        self.heads = 0

    def _c(self):
        return self._cfg() if callable(self._cfg) else self._cfg

    def reset(self):
        self._on = False
        self.env = 0.0
        self._prev = 0.0
        self._head_until = 0.0
        self._body_until = 0.0
        self._lock_until = 0.0
        self._last_active = -1e9
        self.last_p2 = None
        self.last_p3 = None

    def _decay(self, now, dt):
        """包络衰减：按 tr_release_ms 线性回落到当前电平。"""
        rel = max(0.02, float(self._c().get("tr_release_ms", 120)) / 1000.0)
        return max(0.0, self.env - 255.0 * (dt / rel))

    def update(self, now, level, dt=0.008):
        c = self._c()
        L = float(abs(level))
        thr = float(c.get("tr_thr", 6))

        # 起振立即跟上（mode 2 本身有机械惯性，不需要再缓起）
        if L > self.env:
            self.env = L
        else:
            self.env = self._decay(now, dt)

        sharp = 0.0
        if L >= thr:
            self._last_active = now
            if L > self._prev:
                # 陡峭度：用 25 做分母，微弱起振不会被判成"尖"
                sharp = min(1.0, (L - self._prev) / max(25.0, L))
            floor = float(c.get("tr_head_floor", 40))
            if (L >= max(floor, thr * 2.0) and L > self._prev * 1.6 + 5.0
                    and now >= self._lock_until):
                head_s = float(c.get("tr_head_ms", 30)) / 1000.0
                body_s = float(c.get("tr_body_hold_ms", 150)) / 1000.0
                self._head_until = now + head_s
                # 身保持：即使声音已经掉下去，也要把这次冲击"拖住"150ms
                # （飞智 gunBodyHoldMs，否则单个打击只抖 30ms，听起来像卡顿）
                self._body_until = max(self._body_until, now + head_s + body_s)
                self._lock_until = now + float(c.get("tr_lockout_ms", 70)) / 1000.0
                self.heads += 1
        self._prev = L

        # 静音：超过释放时间才回落常态（避免节拍间隙反复起停）
        if L < thr:
            if not self._on:
                return None            # 静默且从未起振：一个字节都不发
            if (now > self._body_until
                    and now - self._last_active > float(c.get("tr_release_ms", 120)) / 1000.0):
                self._on = False
                self.env = 0.0
                self.last_p2 = None
                self.last_p3 = None
                return (ACT_REST,)
            # 身保持窗口内：继续按衰减包络出力（拖住这次冲击）

        p2_base = float(c.get("tr_p2_base", 55))
        p2_max = float(c.get("tr_p2_max", 200))
        y = compose((self.env / 255.0) * float(c.get("tr_gain", 1.0)),
                    c.get("tr_knee", 0.5), c.get("tr_ceiling", 0.8))
        body = p2_base + (p2_max - p2_base) * y
        if now < self._head_until:
            p2 = p2_max                                  # 头：直接拉满
        else:
            # 身：在包络值上再加一点头增益的残余（gunHeadPunchDb 的"余威"）
            p2 = min(p2_max, body * (1.0 + float(c.get("tr_head_db", 0)) / 200.0))

        if now < self._head_until:
            p3 = float(c.get("tr_p3_max", 70))       # 头：固定最高频率档，避免抖动
        else:
            p3 = float(c.get("tr_p3_min", 45)) + \
                (float(c.get("tr_p3_max", 70)) - float(c.get("tr_p3_min", 45))) * sharp

        step = max(1, int(c.get("tr_step", 10)))
        q2 = int(round(p2 / step)) * step
        q2 = int(_clamp(q2, p2_base, p2_max))
        q3 = int(round(p3 / 5.0)) * 5

        if self._on and q2 == self.last_p2 and q3 == self.last_p3:
            return None
        self._on = True
        self.last_p2, self.last_p3 = q2, q3
        return (ACT_SET, q2, q3)


class Gate:
    """抖动写入闸：同值不写、间隔内不写，但大幅跳变（头）放行。"""

    def __init__(self, cfg_get):
        self._cfg = cfg_get
        self.last_val = None
        self.last_at = 0.0
        self.writes = 0
        self.blocked = 0

    def _c(self):
        return self._cfg() if callable(self._cfg) else self._cfg

    def allow(self, now, val):
        c = self._c()
        if val == self.last_val:
            return False
        iv = max(20.0, float(c.get("tr_min_iv_ms", 110))) / 1000.0
        jump = 255 if self.last_val is None else abs(val - self.last_val)
        if now - self.last_at < iv and jump < float(c.get("tr_jump_bypass", 30)):
            self.blocked += 1
            return False
        self.last_val = val
        self.last_at = now
        self.writes += 1
        return True

    def reset(self):
        self.last_val = None
        self.last_at = 0.0
