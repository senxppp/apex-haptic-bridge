<img src="assets/icon-1024.png" width="104" height="104" alt="apex-haptic-bridge">

# apex-haptic-bridge

[![License: MIT](https://img.shields.io/badge/license-MIT-2f81f7?style=flat-square)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-2f81f7?style=flat-square)](requirements.txt)
[![Windows 11 x64](https://img.shields.io/badge/platform-Windows%2011%20x64-2f81f7?style=flat-square)](#兼容与实测)
[![160 offline tests](https://img.shields.io/badge/tests-157%20offline-3fb950?style=flat-square)](#测试)
[![USB/IP bridge](https://img.shields.io/badge/bridge-USB%2FIP-8957e5?style=flat-square)](#架构)

> 把一个「只认自家手柄」的音频力反馈引擎，接到第三方手柄的马达和扳机上。
> A USB/IP device impersonation bridge that translates a proprietary audio-haptics engine's output into another controller's motor/trigger frames.

个人项目的工程存档：**逆向一个私有 USB 协议，然后用 157 条离线回归把它钉住**。
仓库里只有我自己写的代码——不含任何厂商二进制、配置包或反编译产物，见文末[免责声明](#免责声明)。

**中文** · [English](#english)

预编译包（Windows x64）在 [Releases](https://github.com/senxppp/apex-haptic-bridge/releases)；
`app.ini` 里的路径出厂留空，首次运行前要先填 `usbip` / `engine` / `engine_dir`。

## English

**apex-haptic-bridge** is a USB/IP device-impersonation bridge: it lets a proprietary
audio-haptics engine — one that only talks to its own dongle — drive the motors and
force-feedback triggers of another controller.

The engine still does the hard part (audio capture, band weighting, transient detection,
head/body/lock timing). This project only does **device translation** plus the stability
work the engine does not do: attach a virtual controller, capture the engine's `0x57`
real-time frames, throttle them to a safe write rate, and re-emit them as HID frames the
local device accepts.

| Stage | Owner |
|---|---|
| Audio → haptic envelope | vendor engine (unchanged) |
| Engine → USB frames (`0x57`) | captured by the virtual device |
| Frames → third-party HID writes | this project |
| Freeze / hot-plug / reconnect recovery | this project |

**Status** — second generation; supersedes
[`gamepad-haptic-trigger-vibration-research`](https://github.com/senxppp/gamepad-haptic-trigger-vibration-research).
157 offline regression tests (4 more need reference baselines and are skipped); live-validated on one controller over USB.

**Compatibility** — Flydigi Apex 5 (k5) over USB, engine 5.0.2.8, Windows 11 x64. Wireless mode,
other controller models and other engine versions are untested: the `0x57` frame layout and the
`0x51` trigger-mode semantics move with the engine version, so run `test_vibe_chain.py` after any
upgrade — its byte-level comparison pinpoints exactly what changed.

**Notes on the Flydigi ecosystem** (observations, not vendor material)

- The engine does not tune per game: the `profiles/*.json` for Sekiro / Black Myth / Forza
  Horizon 5 are byte-identical (`grip=h2_s2`, `trigger=trigger1`). Perceived differences come
  from the audio itself — which is exactly why this project consumes audio only.
- A trigger exposes three physical quantities: damping, rattle, lock. Among `mode 0..5` only
  normal / racing-damping / machine-gun rattle / lock respond on this firmware; `mode 5`
  (vibration) is a no-op.
- Resistance "against the fingertip" exists only while the finger is inside the trigger travel,
  so a damping-only mapping feels faint. Rattle is the main channel here; damping is layered on.
- Audio is the only input: no game memory reads, no mods, no telemetry.

If this saved you time, a star is appreciated; issues are easiest to fix when you paste the
`判层=` lines from `stats.log`.


**What you install yourself**

- Windows 10/11 x64
- [`cezanne/usbip-win`](https://github.com/cezanne/usbip-win) — the
  `usbip.exe attach -r <host> -b <busid>` client, **not** Microsoft's `usbipd-win`;
  its driver may require test-signing (`bcdedit /set testsigning on`)
- Vendor engine **5.0.2.8** (pinned to this frame layout)
- Controller on USB (wireless untested); vendor background service stopped, otherwise it
  grabs the device
- `app.exe` launched as administrator

**Download** — prebuilt binaries in
[Releases](https://github.com/senxppp/apex-haptic-bridge/releases); `app.ini` ships with
empty paths, fill them in before the first run.

**License & disclaimer** — MIT for the code in this repository. No vendor binaries,
firmware, configs, or decompiled artifacts are included or redistributed. Not affiliated
with, authorized by, or endorsed by Flydigi or any other vendor; device identifiers and
frame layouts are interoperability observations. You must own the hardware and software
you use. Run at your own risk.

---


## 项目状态：第二代

本仓库是**当前版本**。上一代实现（自建音频引擎那套）留档在
[`gamepad-haptic-trigger-vibration-research`](https://github.com/senxppp/gamepad-haptic-trigger-vibration-research)，
其中 `docs/` 的协议方法论至今仍然有效，`haptic-assistant/` 已被本项目取代。

两代的分工差别在**谁来解析音频**：

| | 第一代 `haptic-assistant` | 第二代（本项目） |
|---|---|---|
| 音频解析 | 自己抓回环、自己提特征 | 交给厂商引擎（它做得比我们好） |
| 我们负责 | 全链路（抓音 → 特征 → 映射 → 输出） | 只做**设备间翻译** + **稳定性** |
| 接入方式 | 私有 HID 直发马达（`0xFFA0`） | USB/IP 虚拟设备，截引擎的 `0x57` 流 |
| 适用硬件 | 上一代手柄 + DS 模式 | 新一代手柄 dongle |
| 稳定性投入 | 基本没有 | 节流 / 停帧自愈 / 热插拔守护 / 157 条回归 |

换成"让厂商引擎解析"不是退让：它的频段权重、瞬态检测、头/身/锁时序是调过的，
自己重写一遍要花几个月还不如它。**把力气留在它不做的那部分**——翻译准、别卡死、别掉线。

同生态的另一个项目：[`ds-unlock`](https://github.com/senxppp/ds-unlock)（DS 模式解锁，解决"能不能用上扳机"）。

---

## 兼容与实测

| 维度 | 已验证 | 未验证 |
|---|---|---|
| 手柄 | 飞智 八爪鱼 5（k5），USB 有线 | 无线 / 接收器模式；6Pro 等其它型号 |
| 引擎 | xHaptics Engine **5.0.2.8**（x64） | 其它版本（帧布局可能变） |
| 系统 | Windows 11 x64 | Windows 10；缺 WebView2 时退回 Tk 面板 |
| 音源 | 系统回环：游戏、音乐、任意播放器 | 独占模式 / 虚拟声卡待测 |

为什么锁死 5.0.2.8：`0x57` 实时帧的 8 子帧布局、`0x51` 的扳机模式语义都随版本走。
换版本先跑 `test_vibe_chain.py`，字节级对照会直接告诉你哪一段变了。

### 关于飞智（Flydigi）这套生态，实测到的事实

这些是观察结果，用来解释「为什么某些手感调不出来」，不是从厂商拿到的技术资料，
也不含任何厂商文件：

- **引擎不为单个游戏调手感**。《只狼》《黑神话》《地平线 5》三份 `profiles/*.json`
  逐字节相同，默认档都是 `grip=h2_s2` / `trigger=trigger1`。所以「换个游戏手感就变」
  不来自引擎侧的游戏识别，而来自音频本身的差异——这也正是本项目只吃音频的原因。
- **扳机只有三种物理量**：阻尼（顶手）、抖动、锁死。`mode 0..5` 里实测可用的是
  常规 / 赛车阻尼 / 机枪抖动 / 锁；`mode 5`（震动）在本机固件上无响应，发了等于没发。
- **「顶手」摸不到是物理原因**：阻尼只在手指压在行程内时存在，松开就没了。
  所以纯阻尼方案体感很淡；本项目默认用抖动当主通道，阻尼只做叠加。
- **音频是唯一输入**：不读游戏内存、不打 mod、不接遥测。厂商自己的遥测最终也是
  转成同一路音频波形喂给引擎，绕开它没有收益。

帧布局、模式语义、参数范围见 [`docs/trigger-protocol.md`](docs/trigger-protocol.md)，
真机标定方法见 [`docs/real-device-calibration.md`](docs/real-device-calibration.md)。

## 它解决什么问题

飞智的 `xHaptics Engine` 从系统音频回环里算三通道包络（扳机 / 左握把 / 右握把），
再通过 USB 把结果发给它认识的手柄。它只认自己的设备——插别的牌子，什么都不发生。

这个项目在 USB/IP 上**虚拟出一台它认识的设备**，把自己摆在引擎和设备之间：

- 收下引擎的 `0x57` 实时帧（125 Hz，每帧 8 子帧），解析三通道
- 把包络翻译成目标手柄能执行的 HID 帧（`0x12` 电机电平、`0x51` 扳机模式）
- 同时解决一个更隐蔽的工程问题：目标手柄的 USB 写一次要 20 ms，一旦积压，
  引擎的输出就会挂起——从"有点卡"变成"彻底不动"

## 架构

```
游戏 / 音乐
   │  系统音频（WASAPI 回环）
   ▼
xHaptics Engine（第三方）── 0x57 帧：t / l / r 三通道包络，125 Hz
   │  USB（它以为在对着一支 APEX 6 Pro 说话）
   ▼
usbip_apex6.py（本项目）── 虚拟设备 VID_37D7&PID_2502
   ├─ decode_57()       三通道解析
   ├─ frame_guard_core  停帧判定（心跳 / 冷却 / 指数退避）
   ├─ trig_rattle       包络 → 扳机 mode 2 抖动（头 / 身 / 锁 / 释放）
   ├─ trig_force        包络 → 扳机 mode 1 阻尼力度
   └─ _writer 线程       相位基准节流（12.5 写/s 上限）
          │
          ▼
      k5_writer.py ── HID 输出报告 ──▶ 目标手柄 dongle ──▶ 马达 / 扳机电机
```

## 三个可复用的解法（与具体硬件无关）

这个仓库真正值得看的部分，跟"哪家手柄"没关系。任何"上游高频输出 + 下游慢速硬件"的项目都能直接用。

### 1. 相位基准调度，而不是"上次发送时间"

写一次硬件要 20 ms，而目标帧率是 12.5 帧/s（间隔 80 ms）。天真的写法是：

```python
time.sleep(0.08); send()
```

问题是 `sleep` 本身有误差、发送本身要 20 ms，**误差会累积**。第一版把"上次发送时刻"记在
`sleep` 之前，实测跑成了 **22.5 写/s**（设计值的 1.8 倍）——比不节流还糟。

正确做法是维护**计划时刻** `next_send_at += interval`，落后一个完整周期就重置而不追赶：

```python
# 关键：基准是"应该发的时刻"，不是"实际发完的时刻"
if now > next_send_at + interval:      # 落后太多 → 重置，不补发（补发只会雪上加霜）
    next_send_at = now + interval
else:
    next_send_at += interval
```

→ `usbip_apex6.py` 的 `_writer()`。实测稳定在 12.5 写/s，DPC 不再堆积。

### 2. 停帧自愈：1 秒心跳 → 1.2 秒快速重连 → 4 秒整套重载

引擎在**音源静默**时会挂起输出（不是按周期，是"没声音就不干活"）。
此时下游看到的是"帧数不再增长"，而"帧数不再增长"和"设备挂了"在外部表现完全一样。

判定不能只看"多久没数据"，要看两条独立证据：上游心跳（音频回环 RMS）与下游心跳（写入计数）。

```
k5_probe.log 心跳停更 ≥1s
   → quick_cycle()：只摘挂设备，1.2 s（保住引擎进程和会话）
   → 4 s 内恢复：结束
   → 未恢复：整套重载（约 3 分钟），带指数退避 + 冷却
```

→ `frame_guard_core.py`（纯状态机，可单测）+ `auto_keepalive.py`（守护进程）。
**恢复时间从 45~65 秒降到约 5 秒**，全程无人值守。

### 3. 热插拔守护：去抖、稳定期、冷却，以及"排除自己"

手柄拔插会触发重载，但重载本身会摘挂虚拟设备——如果不排除自己的 VID/PID，
守护会把自己的动作当成"用户插拔"，变成自我触发的无限重载。

```python
DEBOUNCE = 2.0      # 拔出后先等 2 秒（插拔抖动很常见）
STABLE   = 8.0      # 设备稳定 8 秒才动作
COOLDOWN = 240.0    # 4 分钟冷却，防连续插拔把链路刷爆
EXCLUDE  = {"VID_37D7&PID_2502"}   # 本项目虚拟出来的设备，绝不能算"用户插拔"
```

→ `hotplug_core.py` / `hotplug_reload.py`。28 条单测覆盖抖动、冷却、自我触发。

## 快速开始

```bash
# 1. 依赖（只需要 hidapi）
pip install -r requirements.txt

# 2. 配置路径（app.ini 在 .gitignore 里，不会被提交）
copy app.ini.example app.ini
#   填入 usbip.exe / 引擎路径

# 3. 起全链路（约 2 分钟：服务端 → 虚拟设备 → 引擎 → 流验证）
python -I reload_chain.py

# 4. 停
python -I stop_chain.py
```

Windows 批处理封装：`Start-全链路.bat` / `Stop-全链路.bat` / `Start-Hotplug.bat` / `Start-Keepalive.bat`。

## 扳机：三种物理量，只有两种能用

目标手柄的扳机是**力反馈电机**（会顶手），不是音频马达（不会振）。这个区别决定了所有参数：

| 模式 | 语义 | 参数 | 结论 |
|---|---|---|---|
| mode 0 | 常规 | 行程 min/max | 释放用 |
| mode 1 | 阻尼（赛车） | `p1` = 阻尼力度 1..255 | ✅ 实机标定：1/8/32/120/255 能分出轻重 |
| mode 2 | 机枪 | `p2` 强度 / `p3` 频率 | ✅ 主通道 |
| mode 5 | 扳机震动 | 起始位置/力度/频率… | ❌ 实机标定完全无响应，弃用 |

「顶手」（mode 1 阻尼随包络变力）实装了但**默认关**：阻尼只有手指按在行程中段才摸得到，
放松搭着完全无感，所以它不能当主通道，只能当"手放在扳机上"的附加层。

主通道是 mode 2 抖动，形状抄的是「头 / 身 / 锁 / 释放」四段结构：

```
包络 ──软拐点压缩(knee 0.5 / ceiling 0.8)──┬─→ p2 强度 55..200
                                          └─→ p3 频率 45..70（越尖越高）

头：p2 拉满 200，30 ms           ← 起振那一下的"脆"
锁：70 ms 内不再起头             ← 防密集场景连成机关枪
身：随包络连续衰减，保持 150 ms   ← 余震，不是"震一下"
释放：静音 220 ms 后回落 mode 1
```

实测单次冲击的写入序列（4 帧，3~5 写/s，余量留给握把）：

```
m2 p200  ← 头
m2 p144  ← 身
m2 p60   ← 尾
m1 p0,0  ← 释放
```

## 配置

两份，都是热加载（改完约 1 秒生效，不用重启）：

- `app.ini` — 本机路径（`usbip` / `engine` / `python` / `fakegame`）
- `motor_config.json` — 手感参数

常用旋钮：

| 键 | 现值 | 作用 |
|---|---|---|
| `tr_enable` | 1 | 抖动层总开关（0 = 退回旧的固定时长爆发） |
| `tr_p2_max` | 200 | 冲击最高强度（嫌猛 → 170） |
| `tr_head_ms` | 30 | 脆度（短 = 更"点"，长 = 更"闷"） |
| `tr_lockout_ms` | 70 | 连打抑制（大 = 更克制） |
| `tr_release_ms` | 220 | 余震长短（大 = 更"黏"） |
| `tf_enable` | 0 | 阻尼层（"顶手"）开关 |
| `mute.flag` | — | 存在即静音（界面「暂停震动」写它），服务端每秒检查 |
| `cmd.txt` | — | 一次性命令：`zero` / `buzz`，取走即删 |
| `k5_min_iv` | 0.08 | 硬件写入最小间隔（12.5 写/s） |

## 测试

不需要硬件、不需要引擎，全部离线：

```bash
for t in test_*.py; do python -I "$t"; done
```

| 套件 | 条数 | 覆盖 |
|---|---|---|
| `test_vibe_chain.py` | 19（+4 SKIP） | 协议帧构造、0x57 解析、stats 格式串 |
| `test_hotplug.py` | 28 | 去抖 / 稳定期 / 冷却 / 自我触发排除 |
| `test_frame_guard.py` | 32 | 停帧判定、来源选择、退避 |
| `test_trigger_fallback.py` | 18 | 扳机回落（残响帧 / 队列插队 / 参数下限） |
| `test_trig_force.py` | 27 | 包络 → 力、量化、写闸 |
| `test_trig_rattle.py` | 25 | 头/身/锁时序、预算 |
| `test_trig_wire.py` | 8 | **接线**：喂真 0x57 帧，断言"打击后不许立刻 rest" |
| `test_drift.py` | 9 | **引用完整性**：所有被 spawn / `python -I` 拉起的脚本是否真在仓库里 |
| **合计** | **166** | 157 条业务回归 + 9 条仓库自洽 |

> `test_vibe_chain.py` 里有 4 条需要历史版本源码做逐字节对照（"永久不震"bug 复现、
> 节流相位回归、三类帧的零回归）。那两份基准含本机绝对路径，不随仓库发布，
> 因此开源版把这 4 条标成 **SKIP 并打印原因**，不计入 PASS。早期版本写的是
> `old is None or ...` 这样的恒真断言，缺基准也会打印 PASS，等于把"没验证"
> 伪装成"验证过"，现已改掉。

最后那套是踩坑换来的：渲染器刚把 mode 2 设上，`on_out` 里一条旧的"爆发到时回落"分支
在下一帧就把它改回 mode 1，现象是日志里只剩一串 `m1 p0,0`、扳机完全不抖。
接线测试专门防这个。

## 已知限制

- **Windows only**。依赖 `pnputil`、PowerShell、`CREATE_NO_WINDOW`。
- 需要目标手柄的 **dongle 与 HID 协议**，仓库里不含设备描述符。
- 引擎的"静默挂起"是黑盒行为，自愈是经验解，不是根治。
- 抓取音频做回环时，`k5_min_iv` 不要放宽——DPC 一点积压就会雪崩。

## 目录

```
usbip_apex6.py       USB/IP 虚拟设备 + 0x57 解析 + 写入节流（核心）
k5_writer.py         HID 写进程（独立 GIL 隔离，单点崩溃不影响服务端）
frame_guard_core.py  停帧状态机（纯逻辑，可单测）
auto_keepalive.py    停帧自愈守护
quick_cycle.py       设备快速重连（独立进程，由守护/排障调用）
hotplug_core.py      热插拔判定（纯逻辑）
hotplug_reload.py    热插拔守护进程
trig_force.py        包络 → 扳机阻尼力度
trig_rattle.py       包络 → 扳机抖动（头/身/锁/释放）
app.py               GUI + 编排（打包入口）
panel_web.py         控制台界面（WebView2 + HTML/CSS/JS）
ui/                  界面资源：index.html / style.css / app.js
reload_chain.py      一键重载（约 3 分钟）
stop_chain.py        一键干净停止
watchdog.py          引擎进程守护
tcp_proxy2.py        端口转接
docs/                设计文档
test_drift.py        引用完整性自检（被 spawn 的脚本必须真存在）
fakegame/            假游戏桩（让引擎认为有游戏在跑）
```

## 如果它对你有用

这类「逆向 + 稳定性」的项目，Star 是最直接的反馈；用上了、或者被某段实现帮到，
都欢迎点一个。遇到问题请开 issue，把 `stats.log` 里带「判层=」的那几行贴上来，
基本能一步定位到是引擎、桥、还是驱动那一层。

## 免责声明

- 本项目**不包含**任何厂商的二进制、配置文件、固件或反编译产物；
  仓库里的所有代码都是独立实现的。
- 用到的设备标识符（VID/PID）、帧布局、模式语义，是**为互操作性**而做的观察结果，
  不是从厂商处获得的技术资料。
- 你需要自己合法拥有相关软件与硬件。作者不对使用后果、保修失效或账号风险负责。
- 与飞智（Flydigi）及其他厂商**没有任何隶属或背书关系**。

## 致谢

设备虚拟化的思路参考了开源项目 VIIPER（GPLv3）的公开设计讨论。
本项目**未包含也未链接**其源码——只借用了"相位基准调度"这一条设计思想，
代码是独立写的，因此本项目采用 MIT。
