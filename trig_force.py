# trig_force.py — 把 0x57 音频包络渲染成「阻尼力度」（力反馈电机的正确语法）
#
# 为什么走这条路（2026-11-15 12:0x 标定结论）：
#   1. mode 1 的 p1 = 阻尼力度（1..255），实测能分出轻重 → 这是唯一的连续力通道；
#   2. mode 5（扳机震动）在这支 k5 上完全不响应 → "持续震动"这条第三类路走不通；
#   3. k5 的扳机是力反馈电机，不是音频马达：飞智那套 60/80/130Hz 载波对我们没意义，
#      但它的**包络**有意义（什么时候用力、用多大力），执行机构换成阻尼即可。
#
# 所以渲染规则只有三句：
#   静默   → 基线阻尼（tf_base，很轻）
#   持续音 → 阻尼随包络抬升（快攻慢放，attack 15ms / release 180ms）
#   瞬态   → 短促加力顶一下（tf_punch_ms），而不是打一发"机枪"抖动
#
# 写入预算：k5 上限 12.5 写/s（DPC 实锤）。力值做档位量化 + 最小间隔，
# 稳态几乎不写；只有包络真正变化时才写一帧 0x51。

DEFAULTS = {
    "tf_enable": 0,        # 1=启用（默认关，等实机手感验收）
    "tf_base": 1,          # 静默基线阻尼（1..255，1=最轻）
    "tf_max": 140,         # 峰值阻尼上限
    "tf_gain": 1.0,        # 包络→力 的斜率
    "tf_attack_ms": 15,    # 上升时间常数（电机有惯性，别照抄 DSP 的 1ms）
    "tf_release_ms": 180,  # 下降时间常数（比 attack 慢，才像"有惯性地在转/在顶"）
    "tf_punch_db": 6,      # 瞬态额外加力（百分比点数，相对当前力）
    "tf_punch_ms": 90,     # 瞬态加力保持时长
    "tf_step": 8,          # 力档位量化步长（越小越细腻、写越多）
    "tf_min_iv_ms": 95,    # 两次力写入的最小间隔（≈10 写/s，留预算给握把）
    "tf_idle_release_s": 0,# >0：静默超过这么多秒后把阻尼放到 0（mode 0 释放）
}

EPS = 1e-9


class ForceRenderer:
    """包络状态机。step(now, level) → 目标阻尼力度（1..255）。纯计算，可离线测。"""

    def __init__(self, cfg_get):
        self._cfg = cfg_get
        self.env = 0.0          # 平滑后的包络（0..255）
        self.force = 0.0        # 当前输出力（量化前）
        self._prev = 0.0        # 上一帧的原始电平（瞬态检测用）
        self._punch_until = 0.0
        self._last_active = 0.0
        self._t = None

    def _c(self):
        return self._cfg() if callable(self._cfg) else self._cfg

    def step(self, now, level):
        c = self._c()
        x = float(abs(level))
        if x > 0:
            self._last_active = now
        dt = 0.0 if self._t is None else max(0.0, now - self._t)
        self._t = now
        if dt > EPS:
            if x > self.env:
                tau = max(1.0, c.get("tf_attack_ms", 15)) / 1000.0
            else:
                tau = max(1.0, c.get("tf_release_ms", 180)) / 1000.0
            # 一阶低通；dt 远大于 tau 时直接到位（避免除零与过冲）
            a = 1.0 - pow(2.718281828, -dt / tau)
            self.env += (x - self.env) * a

        # ---- 瞬态：电平突然抬头 → 短促加力 ----
        punch = 0.0
        if x >= 8 and x > self._prev * 1.8 + 4:
            self._punch_until = now + c.get("tf_punch_ms", 90) / 1000.0
        if now < self._punch_until:
            punch = self.env * max(0.0, c.get("tf_punch_db", 6)) / 100.0
        self._prev = x

        base = float(c.get("tf_base", 1))
        gain = float(c.get("tf_gain", 1.0))
        cap = float(c.get("tf_max", 140))
        f = base + (self.env * gain) + punch

        # 静默放行：可选，把常态阻力彻底去掉（mode 0）
        rel_s = float(c.get("tf_idle_release_s", 0))
        if rel_s > 0 and (now - self._last_active) > rel_s:
            f = 0.0

        self.force = max(0.0, min(cap, f))
        return self.force

    def quantize(self, force):
        """按档位量化，减少 0x51 写入次数（电机感觉不出一个档位内的差别）。
        注意：force>0 时最小档是 step 本身，不能量化成 0——0 在协议里是"释放"，
        会把常态阻尼整个掉没。"""
        step = max(1, int(self._c().get("tf_step", 8)))
        q = int(round(force / step)) * step
        if q < 1 and force > 0:
            q = step
        return max(0, min(255, q))


class WriteGate:
    """写入闸：只在力值真的变了、且距离上次写入够久时才放行一帧。"""

    def __init__(self, cfg_get):
        self._cfg = cfg_get
        self.last_val = None
        self.last_at = 0.0
        self.writes = 0
        self.blocked = 0

    def _c(self):
        return self._cfg() if callable(self._cfg) else self._cfg

    def allow(self, now, val):
        if val is None:
            return False
        if self.last_val == val:
            return False
        c = self._c()
        iv = max(20.0, float(c.get("tf_min_iv_ms", 95))) / 1000.0
        # 大幅跳变（瞬态冲击）允许突破间隔限制：否则 120ms 的闸门会把 attack 磨平，
        # "顶一下"变成"顶晚了"。小幅抖动仍然被闸门压住，保证写入预算。
        jump = abs(val - self.last_val) if self.last_val is not None else 255
        if now - self.last_at < iv and jump < float(c.get("tf_jump_bypass", 24)):
            self.blocked += 1
            return False
        self.last_val = val
        self.last_at = now
        self.writes += 1
        return True

    def reset(self):
        self.last_val = None
        self.last_at = 0.0
