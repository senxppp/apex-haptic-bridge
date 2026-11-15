#!/usr/bin/env python3
"""usbip 服务端 v2：模拟 Flydigi APEX 6 Pro vendor HID（修正 RET_SUBMIT 48 字节帧）。

线协议要点（usbip-win2 源码 + Linux usbip_protocol.rst 实测校准）：
  * OP 层：OP_REQ/REP_DEVLIST(0x8005/0x0005)、OP_REQ/REP_IMPORT(0x8003/0x0003)
  * RET_SUBMIT = 48 字节头（basic 20 + ret_submit 20 + 8 padding，无 setup 回显）
    basic 的 devid/direction/ep 服务端应答置 0
  * CMD_SUBMIT = 48 字节头（含 setup），OUT 有 payload，IN 无 payload
  * number_of_packets 非同步 = -1(0xFFFFFFFF)

引擎侧设备：VID 0x37D7 PID 0x2502 page 0xFFA0 in/out 32B
  * 0x01 GetInfo → 应答 deviceType=0x96(Pro) connMode=0x01 features=0xFF
  * 0x53 MotorRoute 使能 → ACK 回显
  * 0x57 RealtimeMotor → 解码 8 子帧左/右握把 → k5 0x12 转发
"""
import argparse
import atexit
import ctypes
import gc
import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time as _t

if getattr(sys, "frozen", False):
    _BASE = os.path.dirname(sys.executable)
else:
    _BASE = os.path.dirname(os.path.abspath(__file__))

# 子进程用 `python -I` 启动（隔离模式 -I 隐含 -P），**脚本目录不会进 sys.path**，
# 本目录模块必须在显式加路径之后才能 import。12:15 实测踩过：try/except 把
# ModuleNotFoundError 吞了 → 力包络层静默不生效，日志里只有"没有 0x51 帧"。
if _BASE not in sys.path:
    sys.path.insert(0, _BASE)
try:
    import trig_force              # 音频包络 → 阻尼力度；缺失时主链路照常跑
except Exception as _e:
    trig_force = None
    print("[cfg] trig_force 不可用（力包络层关闭）: %r" % (_e,), flush=True)
try:
    import trig_rattle             # 音频包络 → mode2 抖动（头/身/锁/释放）
except Exception as _e:
    trig_rattle = None
    print("[cfg] trig_rattle 不可用（抖动 v2 关闭）: %r" % (_e,), flush=True)

# 写进程是 console 程序；由 windowed 的 app.exe 链路启动时必须抑制控制台窗口
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

TCP_NODELAY = socket.IPPROTO_TCP


def _nodelay(sock):
    """禁用 Nagle：usbip 小包协议 + Nagle/延迟 ACK 组合会产生 200ms+ 尖峰
    （正好命中引擎 250ms 的 ACK 等待窗口 → 会话死亡）"""
    try:
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except OSError:
        pass

VID, PID = 0x37D7, 0x2502
# 身份必须随机（2026-11-08 血泪教训）：固定身份会让引擎认出设备并尝试「恢复」
# 上次会话——恢复路径的 disable 校验永远失败（实时音频触觉恢复失败）。
# 随机身份 = 引擎视为全新设备 = 干净地新建会话 = 正常开播
BCD_DEVICE = 0x0100 + (int(_t.time()) % 0x100)
SERIAL = "APEX6_%d" % (int(_t.time()) % 100000)
MANUFACTURER, PRODUCT = "Flydigi", "Flydigi APEX 6"

HID_REPORT_DESC = bytes([
    0x06, 0xA0, 0xFF,
    0x09, 0x01,
    0xA1, 0x01,
    0x09, 0x01,
    0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x20, 0x81, 0x02,
    0x09, 0x02, 0x95, 0x20, 0x91, 0x02,
    0xC0,
])
# MI_00 XBOX360 gamepad 类专属描述符
XUSB_UNKNOWN_DESC = bytes([
    0x11, 0x21, 0x00, 0x01, 0x01, 0x25, 0x81, 0x14,
    0x00, 0x00, 0x00, 0x00, 0x13, 0x01, 0x08, 0x00, 0x00,
])

# MS OS 1.0: 0xEE 字符串 + ExtendedCompatID（CompatibleID=XUSB10 → xusb22.sys）
MS_OS_STRING = bytes([0x12, 0x03]) + "MSFT100".encode("utf-16-le") + bytes([0x01, 0x00])
EXT_COMPAT_ID = (
    struct.pack("<IHHB", 40, 0x0100, 4, 1) + bytes(7)
    + bytes([0, 0]) + b"XUSB10" + bytes(2) + bytes(8) + bytes(6)
)

# ---- BOS + MS OS 2.0 (Windows 10/11 标准路径) ----
# 官方结构（Microsoft OS 2.0 Descriptors spec）：
#   Set header 固定 10 字节: wLength=0x000A, type=0x0002, dwWindowsVersion, wTotalLength
#   （vendor code / altEnumCode 在 BOS platform capability 里，不在 set header 里！）
#   Compatible ID 描述符固定 20 字节。整套 = 10 + [8 + [8 + 20]] = 46
MS_VENDOR_CODE = 0x01
_set_hdr = struct.pack("<HHIH", 10, 0x0000, 0x06030000, 46)  # set header type = 0x0000（0x0002 是 function subset！）
_cfg_sub = struct.pack("<HHBBH", 36, 0x0001, 1, 0, 28)  # bConfigurationValue=1 必须匹配 config desc
_fn_sub = struct.pack("<HHBBH", 28, 0x0002, 0, 0, 20)
_compat = struct.pack("<HH", 20, 0x0003) + b"XUSB10" + bytes(2) + bytes(8)
MS_OS_20_SET = _set_hdr + _cfg_sub + _fn_sub + _compat
assert len(MS_OS_20_SET) == 46 and len(_compat) == 20, "MS OS 2.0 set length broken"
BOS_DESC = (
    struct.pack("<BBHB", 5, 0x0F, 33, 1)
    + struct.pack("<BBBB", 28, 0x10, 0x05, 0)
    + bytes([0xDF, 0x60, 0xDD, 0xD8, 0x89, 0x45, 0xC7, 0x4C,
             0x9C, 0xD2, 0x65, 0x9D, 0x9E, 0x64, 0x8A, 0x9F])
    + struct.pack("<IHBB", 0x06030000, len(MS_OS_20_SET), MS_VENDOR_CODE, 0)
)

DEVICE_DESC = struct.pack("<BBHBBBBHHHBBBB", 18, 0x01, 0x0200, 0, 0, 0, 0x40,
                          VID, PID, BCD_DEVICE, 1, 2, 3, 1)
STRINGS = {1: MANUFACTURER, 2: PRODUCT, 3: SERIAL}
def build_config_desc():
    ifc0 = struct.pack("<BBBBBBBBB", 9, 0x04, 0, 0, 2, 0xFF, 0x5D, 0x01, 0) + XUSB_UNKNOWN_DESC
    ep0 = struct.pack("<BBBBHB", 7, 0x05, 0x81, 0x03, 32, 4) + struct.pack("<BBBBHB", 7, 0x05, 0x01, 0x03, 32, 4)
    ifc1 = struct.pack("<BBBBBBBBB", 9, 0x04, 1, 0, 2, 0x03, 0x00, 0x00, 0)
    hid = struct.pack("<BBHBBBH", 9, 0x21, 0x0111, 0, 1, 0x22, len(HID_REPORT_DESC))
    ep1 = struct.pack("<BBBBHB", 7, 0x05, 0x82, 0x03, 32, 1) + struct.pack("<BBBBHB", 7, 0x05, 0x02, 0x03, 32, 1)
    total = 9 + len(ifc0) + len(ep0) + len(ifc1) + len(hid) + len(ep1)
    return struct.pack("<BBHBBBBB", 9, 0x02, total, 2, 1, 0, 0x80, 50) + ifc0 + ep0 + ifc1 + hid + ep1


CONFIG_DESC = build_config_desc()


def apex_frame(cmd, payload=b""):
    f = bytearray(32)
    f[0], f[1], f[2] = 0x5A, 0xA5, cmd
    p = payload[:26]
    f[3] = len(p) + 2
    f[4] = 0
    f[5:5 + len(p)] = p
    ck = 0
    for i in range(2, 31):
        ck = (ck + f[i]) & 0xFF
    f[31] = ck
    return bytes(f)


# 固件版本（用户指定）：SI 0.1.1.1 / RF 1.0.6.3 / 主控 9.0.6.8 / 接收器 0.5.0.9
FW_VERSIONS = bytes([0x00, 0x01, 0x01, 0x01,   # SI
                     0x01, 0x00, 0x06, 0x03,   # RF
                     0x09, 0x00, 0x07, 0x06,   # 主控
                     0x00, 0x05, 0x00, 0x09])  # 接收器


def getinfo_frame():
    p = bytearray(26)
    p[0] = 0x96
    p[1] = 0x01
    # 固件版本槽（RE 笔记 §0x01：非零在 (10,11)(14,15)(18,19)(22,23)，步进4，BCD nibble 编码）
    # 真机样例：9.0.4.4/0.1.0.9/0.1.4.2/1.0.5.0 → slot0=主控、slot3=RF；slot1/2=SI/屏幕（顺序待网页验证）
    p[10], p[11] = 0x90, 0x76   # 主控 9.0.7.6（官方最新，消更新提示）
    p[14], p[15] = 0x01, 0x11   # SI 0.1.1.1
    p[18], p[19] = 0x01, 0x00   # 屏幕 0.1.0.0
    p[22], p[23] = 0x10, 0x63   # RF 1.0.6.3
    p[24] = 0xFF
    return apex_frame(0x01, bytes(p))


# 0x04 GetDeviceUuid 应答 = [16B UUID golden][版本号 BCD]（真机格式：前16B是UUID，之后是版本）
UUID_GOLDEN = bytes([0x14, 0x20, 0xC5, 0x6E, 0xAA, 0x1E, 0x00, 0x00,
                     0x00, 0x00, 0x75, 0x20, 0x01, 0x00, 0x00, 0x00])


def versions_payload():
    # BCD nibble 编码：0x90 0x76 → "9.0.7.6"
    return (UUID_GOLDEN
            + bytes([0x90, 0x76])   # 主控 9.0.7.6
            + bytes([0x01, 0x11])   # SI 0.1.1.1
            + bytes([0x01, 0x00])   # 屏幕 0.1.0.0
            + bytes([0x10, 0x63])   # RF 1.0.6.3
            + bytes([0x05, 0x09]))  # 接收器 0.5.0.9  (=26B 恰好满帧)


# ---- 0xA1/A3/A4 配置协议（GPA6-xhaptics-engine-reverse.md 真机实测 golden）----
# A1 GetSlotStatus 应答：activeSlot=0, slotCrc16=[0x1689,0x4112,0x4112,0x4112], globalCrc16=0xB815
A1_SLOT_STATUS = bytes([0x00, 0x89, 0x16, 0x12, 0x41, 0x12, 0x41, 0x12, 0x41, 0x15, 0xB8])
# ramId=6 的 64 字节电机映射块（真机 6Pro 读取结果，与 slotCrc16[0]=0x1689 自洽）
GOLDEN_MOTOR64 = bytes.fromhex(
    "02" + "00" * 19 +
    "0000000140014000000000000002000000000000" +
    "00" * 20 +
    "00" * 4)
CFG_RAM = {}                                    # ramId → bytes（A4 写入存储，读回写一致）
CFG_WRITE = {"ram": None, "data": bytearray()}  # A4 写入事务状态


def apex_frame_v21(cmd, payload):
    """v2.1 三字节头帧：5A A5 cmd payload… sum（0xA3/0xA4 专用，无 len/pkgIdx）"""
    f = bytearray(32)
    f[0], f[1], f[2] = 0x5A, 0xA5, cmd
    p = payload[:28]
    f[3:3 + len(p)] = p
    ck = 0
    for i in range(2, 31):
        ck = (ck + f[i]) & 0xFF
    f[31] = ck
    return bytes(f)


def handle_a3_read(p3):
    """0xA3 读：payload=[area, ramId, chunkIdx, chunkSize]，应答同布局带数据段"""
    area, ram_id, cidx, csize = p3[0], p3[1], p3[2], p3[3]
    blob = bytes(CFG_RAM.get(ram_id, GOLDEN_MOTOR64 if ram_id == 6 else bytes(13)))
    total = len(blob)
    off = cidx * csize
    chunk = blob[off:off + csize]
    reply = bytes([area, 0x00, ram_id, 0]) + struct.pack("<H", total) + \
            bytes([cidx, len(chunk)]) + chunk
    with _STATS_LOCK:
        _STATS["a3"] = _STATS.get("a3", 0) + 1
    _dbg("[out] 0xA3 read ramId=%d chunk=%d size=%d -> %dB" %
         (ram_id, cidx, csize, len(chunk)))
    return apex_frame_v21(0xA3, reply)


def handle_a4_write(p4):
    """0xA4 写（FULL_BEGIN/CHUNK/COMMIT），全部回 status=0；COMMIT 回显 len+crc"""
    sub = p4[0]
    if sub == 0x00 and len(p4) >= 10:      # FULL_BEGIN: [0, ramId, len u32, crc u32]
        ram_id = p4[1]
        CFG_WRITE["ram"] = ram_id
        CFG_WRITE["data"] = bytearray()
        reply = bytes([0x00, 0x00, ram_id])
    elif sub == 0x01 and len(p4) >= 4:     # FULL_CHUNK: [1, off u16, chunkLen u8, data…]
        off = struct.unpack_from("<H", p4, 1)[0]
        clen = p4[3]
        chunk = bytes(p4[4:4 + clen])
        d = CFG_WRITE["data"]
        if len(d) < off + len(chunk):
            d.extend(bytes(off + len(chunk) - len(d)))
        d[off:off + len(chunk)] = chunk
        ram_id = CFG_WRITE.get("ram") or 0
        reply = bytes([0x01, 0x00, ram_id])
    elif sub == 0x02 and len(p4) >= 9:     # FULL_COMMIT: [2, len u32, crc u32]
        ram_id = CFG_WRITE.get("ram") or 0
        total = struct.unpack_from("<I", p4, 1)[0]
        crc = bytes(p4[5:9])
        CFG_RAM[ram_id] = bytes(CFG_WRITE["data"])
        # COMMIT 应答回显：len16@[4]、crc32@[6]（官方 acceptAck 校验点）
        reply = bytes([0x02, 0x00, ram_id, 0]) + \
                struct.pack("<H", total & 0xFFFF) + crc
        _dbg("[out] 0xA4 COMMIT ramId=%d len=%d -> 已存储" % (ram_id, total))
    else:
        reply = None
    if reply:
        return apex_frame_v21(0xA4, reply)
    return None


# ---- k5 扳机力反馈（0x51 SetForceTrigger，协议逆向自 ApexSenseBridge）----
# 帧格式：[0x03, 5A A5, 0x51, len, payload...]  payload=[apply, side, mode, start, p1, p2, p3, match]
# mode: 0=Normal 1=Race 2=RecoilRattle 3=SniperBreak 4=Lock 5=Vibration
# side: 1=左扳机 2=右扳机

def k5_force_trigger(side, mode, start=0, p1=1, p2=1, p3=1, match=0):
    """构造 k5 扳机力反馈帧（0x51）"""
    payload = bytes([1, side, mode, start, max(1, p1), max(1, p2), max(1, p3), match])
    f = bytearray(32)
    f[0], f[1], f[2], f[3], f[4] = 0x03, 0x5A, 0xA5, 0x51, len(payload)
    f[5:5+len(payload)] = payload
    ck = 0
    for i in range(2, 31):
        ck = (ck + f[i]) & 0xFF
    f[31] = ck
    return bytes(f)


def k5_trigger_clear(side):
    """恢复扳机正常（Normal 模式）"""
    return k5_force_trigger(side, 0)


def k5_damp_frame(side, force):
    """mode 1「赛车/阻尼」：p1 = 阻尼力度（1..255），start = 0。
    2026-11-15 12:07 实机标定：p1 从 1→8→32→120→255 手感明显分档，
    确认 p1 就是力旋钮（空间站 UI 里那一页只有"阻尼力度"和"阻尼起始位置"两个滑块）。"""
    force = max(1, min(255, int(force)))
    f = bytearray(32)
    f[0], f[1], f[2], f[3], f[4] = 0x03, 0x5A, 0xA5, 0x51, 8
    f[5:13] = (1, side, 1, 0, force, 0, 0, 0)
    ck = 0
    for i in range(2, 31):
        ck = (ck + f[i]) & 0xFF
    f[31] = ck
    return bytes(f)


def k5_trigger_rest(side):
    """常态轻阻力帧：payload 与 k5_writer.open_dev() 完全一致
    [apply=1, side, mode=1, start=0, p1=1, p2=0, p3=0, match=0]。

    不能走 k5_force_trigger：那里的 max(1, p2)/max(1, p3) 会把 0 抬成 1，
    与开机时设定的常态参数不同 → 回落后的「常态」比初始常态更硬/更顶手，
    用户表现为「音乐停了扳机还在震，过一会儿才安分」（11-15 11:2x 实测）。
    """
    f = bytearray(32)
    f[0], f[1], f[2], f[3], f[4] = 0x03, 0x5A, 0xA5, 0x51, 8
    f[5:13] = (1, side, 1, 0, 1, 0, 0, 0)
    ck = 0
    for i in range(2, 31):
        ck = (ck + f[i]) & 0xFF
    f[31] = ck
    return bytes(f)


# ---- ApexSenseBridge 振动算法移植（0x57 -> k5 0x12）----
# 参数由 motor_config.json 热加载（控制台程序可实时调节）
CONFIG_PATH = os.path.join(_BASE, "motor_config.json")

# ---- 界面通道（GUI 与设备层之间只走文件，不开 socket、不加锁竞争）----
# mute.flag：存在即静音。写线程 1s 查一次，比每帧 stat 便宜得多。
# cmd.txt  ：一次性命令，取走即删。zero=马达归零，buzz=强震约 2 秒。
MUTE_FLAG = os.path.join(_BASE, "mute.flag")
CMD_FILE = os.path.join(_BASE, "cmd.txt")


def _take_cmd():
    """取走一次性命令文件；没有就返回空串。"""
    try:
        if not os.path.exists(CMD_FILE):
            return ""
        with open(CMD_FILE, "r", encoding="utf-8", errors="replace") as f:
            s = f.read().strip()
        os.remove(CMD_FILE)
        return s
    except Exception:
        return ""
CFG = {
    "pcm_gain": 150,     # 100..200，quiet 音放大倍率
    "threshold": 3.0,    # int8 单位 RMS 门限（偏心转子保阈值）
    "strength": 100,     # %，全局强度
    "boost_x": 2.0,      # 转子抬升倍率
    "boost_add": 55,     # 转子非零地板
    "hold_ms": 140,      # 转子保持时间
    "trig_threshold": 12,    # 扳机通道 t 激活门限（i8 有符号值）
    "trig_hold_ms": 300,     # t 归零后扳机效果保持时长
    "trig_burst_ms": 180,    # RecoilRattle 爆发时长（官方 afterTrigger 哲学）
    "trig_push": 1,          # 持续顶手开关（mode 1 Race，官方刹车阻力参数）
    "rt_mode": "idle",       # 右扳机：idle=闲置 / mirror=镜像左扳机（重启生效）
    "config_proto": 1,       # 0xA1/A3/A4 应答开关。1=开启（16:4x 破案：引擎 A3 ACK 超时=全部冻结
                             # 的根因，正确应答=治愈）。请求=5字节头(u16 len)，应答=3字节头(28B 恰满 32B 帧)。
    "debug_print": 0,        # 0=热路径静默（⚠ 16:xx 冻结元凶：print→重定向管道→PowerShell 写文件，
                             #   管道背压堵死 urb 线程→引擎写超时弃权。旧稳定时代=控制台直打无此问题）
    "k5_min_iv": 0.08,       # k5 写入最小间隔秒。实锤：k5 USB（有线）驱动每次写卡内核 ~20ms（DPC），
                             # 27写/s 时引擎写超时挂起=冻结根因。0.08=12.5写/s（转子跟不上更快包络，无损）
    "k5_hard_iv": 0.028,     # 两帧写入之间的绝对最小间隔（秒）。DPC 实测 ~20ms，
                             # 28ms 留了余量：令牌桶可以攒额度，但不能背靠背连写。
    "k5_jump_bypass": 70,    # 电平跳变超过这个值就抢发一帧（打击起手），欠账随后偿还。
    "k5_slow_write_ms": 5,   # 写进程 stdin 管道写耗时超过此值 = 子进程已堆积（DPC 卡住），
                             # 额外拉开一个 min_iv，避免尖刺后连续投喂（借鉴 VIIPER isoOutCompletionDeadline）
    "trig_burst": 1,         # 扳机爆发层（mode 2 鼓点打击）。释放路径已修（流断 2s 自动清零），
                             # 震荡根因=释放住帧路径里的结构性 bug，修复后爆发可安全开启。
    # ---- 扳机力包络层（tf_*）：把 0x57 的 t 通道渲染成"阻尼力度" ----
    # 物理依据：k5 的扳机是力反馈电机（会顶手），不是音频马达（不会振）。
    # 飞智那套 60/80/130Hz 载波对我们没意义，只有它的**包络**有意义。
    # 开启后 mode 2 机枪爆发自动让位（两者同时开会互相抢扳机）；mode 5 实测无响应，不用。
    # 独立实现见 trig_force.py；离线回归 test_trig_force.py。
    "tf_enable": 0,          # 1=启用（默认关）
    "tf_base": 1,            # 静默基线阻尼 1..255（1=最轻，实测已是最小档）
    "tf_max": 140,           # 峰值阻尼上限
    "tf_gain": 1.0,          # 包络→力 斜率
    "tf_attack_ms": 15,      # 上升时间常数
    "tf_release_ms": 180,    # 下降时间常数（比上升慢，才有"惯性"）
    "tf_punch_db": 6,        # 瞬态额外加力（%）
    "tf_punch_ms": 90,       # 瞬态加力保持
    "tf_step": 8,            # 力档位量化步长
    "tf_min_iv_ms": 95,      # 力写入最小间隔（≈10 写/s，给握把留预算）
    "tf_idle_release_s": 0,  # >0：静默这么久后彻底释放（mode 0）
    # ---- 机枪抖动 v2（tr_*）：把飞智 trigger1 的 头/身/锁/释放 结构搬到 mode 2 ----
    # 参数来源：sekiro_430_2026092002.zip → profiles/trigger1.json（X-TRIGGER PUNCH）
    # 详见 trig_rattle.py 文件头；离线回归 test_trig_rattle.py。
    "tr_enable": 1,          # 1=启用（开启后旧的固定时长爆发自动让位）
    "tr_thr": 6,             # 进入抖动的包络门限
    "tr_p2_base": 55,        # 最低强度
    "tr_p2_max": 200,        # 最高强度
    "tr_knee": 0.5,          # 软拐点
    "tr_ceiling": 0.8,       # 上限
    "tr_gain": 1.0,          # 包络→强度
    "tr_head_db": 8,         # 头冲
    "tr_head_ms": 30,        # 头衰减
    "tr_lockout_ms": 70,     # 头锁
    "tr_body_hold_ms": 150,  # 身保持
    "tr_release_ms": 120,    # 释放
    "tr_p3_min": 45,         # 频率低档
    "tr_p3_max": 70,         # 频率高档
    "tr_step": 12,           # p2 量化
    "tr_min_iv_ms": 140,     # 写入最小间隔
    "tr_jump_bypass": 60,    # 大幅跳变放行
    "tr_head_floor": 40,     # 起头绝对门限
}


def _dbg(msg):
    """热路径调试打印（默认关闭）。诊断期临时开：motor_config.json 加 "debug_print": 1"""
    if CFG.get("debug_print"):
        print(msg, flush=True)


_DIAG_LOG = os.path.join(_BASE, "diag.log")


def _diag(msg):
    """关键帧旁路观察器：直接 append 文件（OS 级释放 GIL，微秒级），不经过 print 管道。
    只记 0x53/0xA3/0xA4/0xA1 等稀有帧——抓会话暂停/失败瞬间的协议交换。"""
    try:
        with open(_DIAG_LOG, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (_t.strftime("%H:%M:%S"), msg))
    except Exception:
        pass


def load_cfg():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            CFG.update(json.load(f))
    except Exception:
        pass


def _cfg_watcher():
    while True:
        load_cfg()
        _t.sleep(1.0)


_STATS = {"n": 0, "nz": 0, "ml": 0, "mr": 0, "kw": 0, "cfg": "", "maxlat": 0.0, "slow": 0}
_STATS_LOCK = threading.Lock()


def flow_verdict(d_nz, d_wq, d_wfail, d_dedup):
    """三种「不震」判别法自动化：只看本周期的增量，给出故障所在层。

    层 1 引擎没出帧 / 全零帧；层 2 桥把帧全去重挡下；层 3 dongle 写失败。
    引擎完全静默时本函数不会被调用（stats.log 只在帧数变化时写，mtime 是
    auto_keepalive 的死亡判据，静默期绝不能补写）。
    """
    if d_nz <= 0:
        return "引擎有帧但全零(音源未点火)"
    if d_wfail > 0 and d_wq == 0:
        return "dongle写失败(第3层)"
    if d_wq == 0 and d_dedup > 0:
        return "全被去重挡下(第2层)"
    if d_wq > 0:
        return "正常转发"
    return "无有效帧(检查引擎)"


def stats_line(snap, verdict, d, silence):
    """渲染 stats.log 单行。抽成纯函数是为了离线回归能验证格式串参数对齐
    （写日志处有 except: pass，格式错会静默丢掉分类统计）。"""
    return ("[%s] frames=%d nonzero=%d maxL=%d maxR=%d k5w=%d | "
            "t_nz=%d t_max=%d l_max=%d r_max=%d | "
            "urb8ms=%d maxlat=%sms gc=%sms q=%d qmax=%d(%s) a3=%d hw=%sms(%d) dw=%sms(%d) | "
            "判层=%s 本周期:投喂=%d 失败=%d 去重=%d 节流=%d 慢写=%d 相位重置=%d 静默=%.1fs" % (
                _t.strftime("%H:%M:%S"), snap["n"], snap["nz"], snap["ml"], snap["mr"], snap["kw"],
                snap.get("tnz", 0), snap.get("tmax", 0), snap.get("lmax", 0), snap.get("rmax", 0),
                snap.get("slow", 0), snap.get("maxlat", 0), snap.get("gcpause", 0),
                snap.get("q", 0), snap.get("qmax", 0), snap.get("qcmd", ""), snap.get("a3", 0),
                snap.get("hw", 0), snap.get("hwn", 0), snap.get("dw", 0), snap.get("dwn", 0),
                verdict, d.get("_wq", 0), d.get("_wfail", 0), d.get("_dedup_hits", 0),
                d.get("_throt", 0), d.get("_sloww", 0), d.get("_phase", 0), silence))


def _stats_watcher():
    # GC 手动化：自动 GC 的 gen2 全堆收集可达 50-200ms，会让 urb 循环里的
    # 引擎写超时（250ms 预算）→ abandoned → 挂起 → 会话死。改在低频线程收集。
    gc.collect()  # 启动时清一次
    gc.freeze()    # 冻结现有对象（启动时的启动数据不参与后续回收）
    gc.disable()   # urb 循环永不被 GC 停顿
    # 只在帧数变化时写：mtime 新鲜度=流活性信号（auto_keepalive 依赖它检测死亡）。
    # ⚠ 绝不能在静默期也写 stats.log —— 会把保活的死亡判据抹掉。
    last_n = -1
    last_nz = 0
    last_flow = {}
    last_change_ts = _t.time()
    while True:
        _t.sleep(20)
        _g0 = _t.time()
        gc.collect()  # 低频手动回收（gc 持 GIL，期间 urb 线程全停——间隔越长风险越低）
        _gms = (_t.time() - _g0) * 1000.0
        with _STATS_LOCK:
            _STATS["gcpause"] = round(max(_STATS.get("gcpause", 0), _gms), 1)
        with _STATS_LOCK:
            snap = dict(_STATS)
        # 分类累计计数（P3）：把 HANDOFF 的「三种不震判别法」变成可读结论
        k = getattr(K5, "_inst", None)
        flow = {}
        if k is not None:
            for key in ("_dedup_hits", "_wq", "_wfail", "_throt", "_sloww", "_phase"):
                flow[key] = getattr(k, key, 0)
        if snap["n"] != last_n:
            now = _t.time()
            silence = now - last_change_ts
            last_change_ts = now
            d = {key: flow.get(key, 0) - last_flow.get(key, 0) for key in flow}
            last_flow = dict(flow)
            d_nz = snap["nz"] - last_nz
            last_nz = snap["nz"]
            last_n = snap["n"]
            verdict = flow_verdict(d_nz, d.get("_wq", 0),
                                  d.get("_wfail", 0), d.get("_dedup_hits", 0))
            try:
                with open(os.path.join(_BASE, "stats.log"), "a", encoding="utf-8") as f:
                    f.write(stats_line(snap, verdict, d, silence) + "\n")
            except Exception:
                pass


def _level_from_rms(rms):
    """RMS(0..127) -> k5 马达 0..255：门限 -> 软膝增益 -> 强度缩放"""
    if rms < CFG["threshold"]:
        return 0
    x = rms / 127.0
    g = min(200, max(100, CFG["pcm_gain"]))
    # scaleApex6Pcm 归一化：out = 127*g*x/(100+(g-100)*x)，满幅 x=1 → 127
    out127 = 127.0 * g * x / (100.0 + (g - 100) * x)
    out127 = min(127.0, out127 * CFG["strength"] / 100.0)
    return min(255, int(round(out127 * 255.0 / 127.0)))


def decode_57(payload):
    # 实测布局（无 flags 字节！）：24B = 8子帧×3B（trigger,left,right），第 25B 是填充
    # 线上编码 i8+128（0x80=0）。帧结构：[5A A5 57 len pkgCount][24样本+填充][checksum]
    body = payload[:24]
    tv, lv, rv = [], [], []
    for i in range(8):
        base = i * 3
        if base + 2 >= len(body):
            break
        tv.append(body[base] - 128)      # 扳机力反馈通道（新增）
        lv.append(body[base + 1] - 128)  # 有符号：i8+128 线上编码
        rv.append(body[base + 2] - 128)
    if not lv:
        return 0, 0, 0

    def rms(vals):
        return (sum(v * v for v in vals) / len(vals)) ** 0.5

    def lv1(vals):
        """整包 RMS 给"稳"，最近两子帧给"快"。

        一个 0x57 包 = 8 子帧 = 64ms 音频。只取整包 RMS 时，打击的起手会被
        早先那几帧静音摊薄，听感上就是"慢半拍"。这里再算一次最后 16ms 的 RMS，
        只有它明显更大（起手瞬间）才采纳—— sustain 段不受影响，不会被系统性抬高。
        """
        a = _level_from_rms(rms(vals))
        if len(vals) >= 2:
            b = _level_from_rms(rms(vals[-2:]))
            if b > a * 1.15:
                return b
        return a

    return lv1(tv), lv1(lv), lv1(rv)


class K5:
    # k5 偏心转子补偿：6Pro 握把是音圈（小幅值有感），k5 转子需要高强度+持续才起转
    # 去重 TTL：即便写进程/设备状态与主进程失步，最多等这么久也会强制重发一帧，
    # 避免"设备已归零但主进程以为还在震"的永久不震。重发只在下次调用时发生，
    # 不额外起定时器，写入率最多 +1/s，不影响 12.5/s 的 DPC 节流总开关。
    _RUMBLE_TTL = 1.0
    _TRIG_TTL = 3.0
    _SLOW_WRITE_MS = 5        # stdin 管道写耗时门限（默认 5ms，CFG 可覆盖）
    _inst = None              # _stats_watcher 读取分类计数的唯一入口

    def __init__(self):
        self.dev = None            # 已废弃：hid 写移入独立进程 k5_writer.py
        self._proc = None          # k5 写进程（独立 GIL，dongle 慢写不再阻塞 urb 线程）
        self._last = None
        self._last_sent_at = 0.0        # 去重时间戳（TTL 到期强制重发，防状态失步后永久不震）
        self._last_nz = (0.0, 0, 0)
        self.writes = 0
        self.boosted_max = 0
        self._pend = None           # 待写值（写线程消费）
        self._pend_lock = threading.Lock()
        self._trig_q = []           # 0x51 扳机帧队列（写线程优先消费）
        self._last_trig = None      # 扳机命令去重
        self._last_trig_at = 0.0
        self._trig_burst_ts = 0.0   # 最近一次 mode 2 爆发时刻（独立回落兜底用）
        self._trig_mode = 1         # 事实来源 = k5_writer 连接时设好的常态轻阻力(mode 1)
        self._fail_streak = 0
        self._last_send = 0.0       # 兼容保留（旧节流基准，已不再参与节拍）
        self._next_send_at = 0.0    # 兼容保留（已由令牌桶取代）
        # 令牌桶（15:0x 改）：固定 80ms 间隔会把"打击的起手"整整压后一拍。
        # 桶容量 1.6 允许空闲后立刻补两帧，欠账靠后续补水偿还，
        # 长期速率仍是 1/min_iv（12.5 帧/s），不会漂到危险区。
        self._tokens = 1.0
        self._tok_at = 0.0
        self._tok_cap = 1.6
        self._last_write_at = 0.0
        self._sent_level = (0, 0)
        self._level_now = (0, 0)    # 当前马达电平（安全归零监控用）
        self._last_rumble_ts = 0.0
        # 界面通道：静音状态 + 两次文件查询的时间戳（做缓存，别每帧 stat）
        self._mute = False
        self._flag_t = [0.0, 0.0]
        # 扳机力包络（音频波形 → 阻尼力度）
        self._tf = (trig_force.ForceRenderer(lambda: CFG) if trig_force else None)
        self._tfg = (trig_force.WriteGate(lambda: CFG) if trig_force else None)
        self._tf_last = 0.0
        # 机枪抖动 v2（音频包络 → mode 2 头/身/锁/释放）
        self._tr = (trig_rattle.Rattle(lambda: CFG) if trig_rattle else None)
        self._trg = (trig_rattle.Gate(lambda: CFG) if trig_rattle else None)
        self._tr_last = 0.0
        self._k5_min_iv = 0.08      # k5 写入最小间隔（秒），CFG["k5_min_iv"] 热加载覆盖：
                                    # k5 USB（有线）驱动每次写卡内核 ~20ms（DPC 实锤），
                                    # 写频率越高引擎写超时越多=冻结根因
        # 分类累计计数（单写线程/单 urb 线程，读侧只做快照，无需加锁）
        self._dedup_hits = 0        # 转子帧被去重挡下
        self._wq = 0                # 成功投喂给写进程
        self._wfail = 0             # 管道写失败
        self._throt = 0             # 节流等待次数
        self._sloww = 0             # 慢写（管道背压）次数
        self._phase = 0             # 落后超周期、相位重置次数
        K5._inst = self
        self._spawn_writer()
        print("[k5] 写进程已启动（独立 GIL 隔离）", flush=True)
        threading.Thread(target=self._writer, daemon=True).start()

    def _spawn_writer(self):
        # 写进程重启 = k5_writer.open_dev() 会把设备重新初始化（清转子 + 双扳机设
        # 常态 mode 1）。主进程必须同步清掉去重状态，否则旧 key 仍命中去重、
        # 新值永不入队 → 永久不震 / 扳机卡死。手柄关机再开机、设备重连、
        # 连续写失败自动重启都会走到这里（保活每 45s 可能触发一次设备重连）。
        self._last = None
        self._last_sent_at = 0.0
        self._last_trig = None
        self._last_trig_at = 0.0
        self._trig_burst_ts = 0.0
        self._last_nz = (0.0, 0, 0)
        self._trig_mode = 1
        self._level_now = (0, 0)
        self._last_rumble_ts = _t.time()
        _tf = getattr(self, "_tfg", None)
        if _tf is not None:
            _tf.reset()               # 写进程重启=设备已重init，力帧要重发
        _trg = getattr(self, "_trg", None)
        if _trg is not None:
            _trg.reset()
        _tr = getattr(self, "_tr", None)
        if _tr is not None:
            _tr.reset()
        self._last_send = 0.0
        self._next_send_at = 0.0    # 写进程重启 = 新链路，节流相位从头开始
        self._fail_streak = 0
        with self._pend_lock:
            self._pend = None
            self._trig_q.clear()
        try:
            if self._proc is not None:
                try:
                    self._proc.kill()
                except Exception:
                    pass
                try:
                    self._proc.wait(timeout=1)
                except Exception:
                    pass
            # 冻结模式（PyInstaller exe）：k5_writer.exe 与主程序同目录；
            # 脚本模式：python -I k5_writer.py
            if getattr(sys, "frozen", False):
                cmd = [os.path.join(_BASE, "k5_writer.exe")]
            else:
                cmd = [sys.executable, "-I", os.path.join(_BASE, "k5_writer.py")]
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, bufsize=0,
                creationflags=NO_WINDOW)
        except Exception as e:
            print("[k5] 写进程启动失败:", e, flush=True)
            self._proc = None

    def _k5_send(self, pend):
        """帧发往写进程（stdin 管道，微秒级返回；dongle 慢写发生在子进程）"""
        if self._proc is None or self._proc.poll() is not None:
            self._spawn_writer()
        if self._proc is None or self._proc.stdin is None:
            return 0
        try:
            self._proc.stdin.write(bytes([len(pend)]) + bytes(pend))
            self._proc.stdin.flush()
            return len(pend)
        except Exception:
            return 0

    def _poll_flags(self):
        """静音与一次性命令。放在写线程里，天然串行，不会与 on_out 抢状态。

        静音时要做两件事，少一件都"停不干净"：
          1) 马达电平写零（0x12 是电平保持型，不写零会一直转）
          2) 扳机从 mode 2 回落常态（否则冲击态会保持到下次切换）
        """
        now = _t.time()
        # 防御：离线回归用 K5.__new__ 造对象，不走 __init__，没有这些字段
        ft = getattr(self, "_flag_t", None)
        if ft is None:
            ft = self._flag_t = [0.0, 0.0]
        if now - ft[0] >= 1.0:                    # 静音标记：1s 一次
            ft[0] = now
            self._mute = os.path.exists(MUTE_FLAG)
        if getattr(self, "_mute", False):
            if self._level_now != (0, 0):
                self._level_now = (0, 0)
                self._last = None
                self._last_sent_at = 0.0
                with self._pend_lock:
                    self._pend = [0x03, 0x5A, 0xA5, 0x12, 0x04, 0, 0]
            if self._trig_mode == 2:
                self.trigger_rest()
        if now - ft[1] >= 0.5:                    # 命令文件：0.5s 一次
            ft[1] = now
            cmd = _take_cmd()
            if cmd == "zero":
                self._level_now = (0, 0)
                self._last = None
                self._last_sent_at = 0.0
                with self._pend_lock:
                    self._pend = [0x03, 0x5A, 0xA5, 0x12, 0x04, 0, 0]
                _dbg("[io] cmd zero")
            elif cmd == "buzz":
                # 强震靠"电平保持 + 2 秒安全归零"自然收尾，不额外计时
                self._level_now = (255, 255)
                self._last = (255, 255)
                self._last_rumble_ts = now
                with self._pend_lock:
                    self._pend = [0x03, 0x5A, 0xA5, 0x12, 0x04, 255, 255]
                _dbg("[io] cmd buzz")

    def _writer(self):
        """独立写线程：帧经 stdin 管道发往 k5 写进程 + 连续失败自动重启进程 + 流超时自动归零"""
        while True:
            _t.sleep(0.01)
            self._poll_flags()
            # 安全归零：2 秒没收到新的 rumble 指令且马达非零 → 立刻归零
            # （0x12 是电平保持型，流断掉没人写零马达会永远转）
            if self._level_now != (0, 0) and _t.time() - self._last_rumble_ts > 2.0:
                self._level_now = (0, 0)
                self._last = None
                self._last_sent_at = 0.0
                with self._pend_lock:
                    self._pend = [0x03, 0x5A, 0xA5, 0x12, 0x04, 0, 0]
            # 扳机常态化设计：恒定轻阻力（k5_writer 连接时设置一次），不再动态切换。
            # 旧的"流断清扳机"已删——常态化阻力就是要它一直在（17:2x 用户决定）
            # （k5 侧扳机心跳已撤：22:5x 用户澄清——需要"输入数据"的是模拟的八爪鱼6
            #   （引擎看到控制器有输入才保持活跃），已改在 bulk_in ep1 用 LT 扳机模拟量注入）
            #
            # ⚠ 但 mode 2「鼓点爆发」是瞬时打击效果，不该永久保持。它的回落原本
            # 写在 0x57 帧处理里（Apex6Virtual），必须等引擎发下一帧才会执行；
            # 一旦引擎静默期挂起、0x57 停发，扳机就停在爆发态一直震。
            # 这里补一条不依赖上游帧的独立兜底：爆发超过保持时长、或上游流断 2s，
            # 立即回落到常态 mode 1（不是 mode 0 释放，符合"常态阻力一直在"）。
            if self._trig_mode == 2 and self._trig_burst_ts > 0:
                _now = _t.time()
                _keep = CFG.get("trig_burst_ms", 180) / 1000.0 + 0.5
                _rotor_idle = (_now - self._last_nz[0]) if self._last_nz[0] else 0.0
                if (_now - self._trig_burst_ts > _keep
                        or _now - self._last_rumble_ts > 2.0
                        or _rotor_idle > 1.5):
                    _back = [k5_trigger_rest(1)]
                    if CFG.get("rt_mode") == "mirror":
                        _back.append(k5_trigger_rest(2))
                    with self._pend_lock:
                        # 队列里可能还压着未发出的爆发帧，必须先清掉，
                        # 否则「先回落、再补一发爆发」会把扳机又设回 mode 2。
                        self._trig_q[:] = [f for f in self._trig_q
                                           if not (len(f) > 7 and f[3] == 0x51
                                                   and f[7] == 2)]
                        self._trig_q[0:0] = _back
                    self._trig_mode = 1
                    self._trig_burst_ts = 0.0
                    self._last_trig = None   # 放行后续真正的状态切换命令
            self._tf_tick()
            self._tr_tick()
            with self._pend_lock:
                pend = self._pend
                self._pend = None
                # 0x51 扳机帧优先：状态切换类命令要立即生效（频率低，不挤占 0x12 流）
                if pend is None and self._trig_q:
                    pend = self._trig_q.pop(0)
            if pend is None:
                continue
            # ---- DPC 节流（令牌桶版）----
            # 旧版是硬性「每 80ms 一帧」：音源起手时，新电平最多要等满一个周期
            # 才发出去，听感就是"慢半拍"。改成令牌桶后：
            #   · 长期速率仍是 12.5 帧/s（1/min_iv），不碰 DPC 危险区
            #   · 空闲攒下的令牌允许"立刻补一帧"，起手延迟从 ≤80ms 降到 ≤硬间隔
            #   · 大跳变（打击）额外给一次抢发资格，欠账在后面几格里偿还
            #   · 硬间隔（默认 28ms）永远生效，防两帧背靠背把驱动卡死
            # 防御：离线回归用 K5.__new__ 造对象（不走 __init__），缺字段会让
            # 写线程第一帧就抛 AttributeError 而死——三套用例同时挂就是这么来的。
            if not hasattr(self, "_tokens"):
                self._tokens, self._tok_at = 1.0, 0.0
                self._tok_cap, self._last_write_at = 1.6, 0.0
                self._sent_level = (0, 0)
            min_iv = CFG.get("k5_min_iv", self._k5_min_iv)
            hard_iv = CFG.get("k5_hard_iv", 0.028)
            now = _t.time()
            if self._tok_at <= 0:
                self._tok_at = now
            self._tokens = min(self._tok_cap,
                               self._tokens + (now - self._tok_at) / max(min_iv, 0.02))
            self._tok_at = now
            jump = 0
            if len(pend) > 6 and pend[3] == 0x12:
                jump = max(abs(pend[5] - self._sent_level[0]),
                           abs(pend[6] - self._sent_level[1]))
            wait = hard_iv - (now - self._last_write_at)
            allow_burst = jump >= CFG.get("k5_jump_bypass", 70)
            if not allow_burst:
                wait = max(wait, (1.0 - self._tokens) * min_iv)
            if wait > 0:
                _t.sleep(min(wait, 0.12))
                self._throt += 1
            # 欠账（抢发用掉的额度）允许为负，但最多欠一格
            self._tokens = max(-1.0, self._tokens - 1.0)
            t0 = _t.time()
            n = self._k5_send(pend)
            cost = _t.time() - t0
            if n and n > 0:
                self.writes += 1
                self._wq += 1
                self._fail_streak = 0
                self._last_write_at = _t.time()
                if len(pend) > 6 and pend[3] == 0x12:
                    self._sent_level = (pend[5], pend[6])
                slow_s = CFG.get("k5_slow_write_ms", self._SLOW_WRITE_MS) / 1000.0
                if cost > slow_s:
                    # 管道背压 = 写进程那边已被 DPC 卡住，没收一格额度别急着投喂
                    self._tokens -= 0.5
                    self._sloww += 1
            else:
                self._wfail += 1
                self._tokens = max(self._tokens, 0.6)   # 失败快速重试，但别打满
                self._fail_streak += 1
                # 0x51 是状态切换帧，去重 key 已记录，丢一帧就永远停在错误模式
                # （扳机卡死）。失败时放回队首重发；0x12 电平帧下一帧会覆盖，无需留。
                if len(pend) > 3 and pend[3] == 0x51 and len(self._trig_q) < 8:
                    with self._pend_lock:
                        self._trig_q.insert(0, pend)
            if self._fail_streak >= 50:
                # 写进程僵死：重启
                print("[k5] 写入连续失败，重启写进程", flush=True)
                self._spawn_writer()
                self._fail_streak = 0

    def rumble(self, low, high):
        if self._proc is None:
            return
        low, high = low & 0xFF, high & 0xFF
        now = _t.time()
        if low == 0 and high == 0:
            # 短保持：节拍间隙不归零，转子不停转
            if self._last_nz[0] > 0 and now - self._last_nz[0] < CFG["hold_ms"] / 1000.0:
                low, high = self._last_nz[1], self._last_nz[2]
        else:
            self._last_nz = (now, low, high)
        # 转子抬升：非零即加地板 + 线性放大
        if low:
            low = min(255, int(low * CFG["boost_x"] + CFG["boost_add"]))
        if high:
            high = min(255, int(high * CFG["boost_x"] + CFG["boost_add"]))
        self.boosted_max = max(self.boosted_max, low, high)
        self._last_rumble_ts = now  # 流活性信号（去重前刷新，防误归零）
        # TTL 去重：写进程/设备状态失步时最多 TTL 秒就会强制重发一帧，自愈恢复震动
        if (low, high) == self._last and (now - self._last_sent_at) < self._RUMBLE_TTL:
            self._dedup_hits = getattr(self, "_dedup_hits", 0) + 1
            return
        self._last = (low, high)
        self._last_sent_at = now
        self._level_now = (low, high)
        with self._pend_lock:
            self._pend = [0x03, 0x5A, 0xA5, 0x12, 0x04, low, high]  # 非阻塞入队

    def trigger(self, side, mode, start=0, p1=1, p2=1, p3=1, match=0):
        """0x51 SetForceTrigger：state-change 才发（效果在 k5 端保持，无需逐帧刷）
        官方语义（forza_trigger RE）：mode 1=持续阻力(顶手) [0,1,0,0]，
        mode 2=打击抖动 params[起始位置,1,强度,次级]；强度域 55~200"""
        if self._proc is None:
            return
        key = (side, mode, start, p1, p2, p3, match)
        now = _t.time()
        if key == self._last_trig and (now - self._last_trig_at) < self._TRIG_TTL:
            return
        self._last_trig = key
        self._last_trig_at = now
        self._trig_mode = mode
        # mode 2 是瞬时打击效果，记录时刻供 _writer 的独立回落兜底使用；
        # 没有它，一旦引擎挂起、0x57 停发，扳机会永久停在爆发态。
        self._trig_burst_ts = now if mode == 2 else 0.0
        frames = [k5_force_trigger(side, mode, start, p1, p2, p3, match)]
        if CFG.get("rt_mode") == "mirror" and side == 1:
            frames.append(k5_force_trigger(2, mode, start, p1, p2, p3, match))
        with self._pend_lock:
            self._trig_q.extend(frames)
        # 注意：不再重置 rumble 去重（_trig_q 独立队列，0x12 帧不会被顶掉）
        # （旧版重置导致 rumble 去重失效 → dongle 写入率翻倍 → GIL 阻塞 urb 线程）

    def trigger_rest(self):
        """回落常态轻阻力（左右扳机），与 open_dev() 参数完全一致。
        直接入队不走 trigger()：避免 max(1,·) 把 p2/p3 抬成 1（手感偏硬），
        也顺便把去重状态清掉，让后续真正的状态切换不被挡。"""
        if self._proc is None:
            return
        frames = [k5_trigger_rest(1)]
        if CFG.get("rt_mode") == "mirror":
            frames.append(k5_trigger_rest(2))
        with self._pend_lock:
            # mirror 下一条爆发会生成左右两帧；右帧可能还压在队列里。
            # 若只往队首插回落，发送顺序会变成「回落→回落→残留爆发」，
            # 右扳机最终停在 mode 2（11-15 11:34 实测踩到）。必须先清残留。
            self._trig_q[:] = [f for f in self._trig_q
                               if not (len(f) > 7 and f[3] == 0x51
                                       and f[7] == 2)]
            self._trig_q[0:0] = frames
        self._trig_mode = 1
        self._trig_burst_ts = 0.0
        self._last_trig = None
        self._last_trig_at = 0.0

    # ---- 扳机力包络：音频波形 → 阻尼力度 -------------------------------
    # 为什么不用 0x57 直接写扳机波形：k5 的扳机是力反馈电机（行程/阻尼），
    # 固件按"力"执行，不按"波形"执行；60/80/130Hz 载波在电机上等于噪声。
    # 所以取包络、丢载波：env → 阻尼力度（mode 1 p1）。
    def set_damp(self, force, now=None):
        """把阻尼力度写到扳机。force<=0 表示释放（mode 0）。只在力值变化时调用。"""
        if self._proc is None:
            return
        force = int(max(0, min(255, force)))
        sides = (1, 2) if CFG.get("rt_mode") == "mirror" else (1,)
        frames = [(k5_force_trigger(s, 0) if force <= 0 else k5_damp_frame(s, force))
                  for s in sides]
        self._trig_mode = 1 if force > 0 else 0
        self._trig_burst_ts = 0.0        # 力包络模式下不保留爆发状态
        self._last_trig = None
        with self._pend_lock:
            # 队列只保留最近几帧力值：力是"当前状态"，旧值排队只会造成手感滞后。
            self._trig_q.extend(frames)
            if len(self._trig_q) > 4:
                del self._trig_q[:-4]

    def set_rattle(self, p2, p3):
        """下发机枪抖动强度/频率（mode 2）。由 trig_rattle 的头/身状态机驱动。"""
        if self._proc is None:
            return
        p2 = int(max(1, min(255, p2)))
        p3 = int(max(1, min(255, p3)))
        sides = (1, 2) if CFG.get("rt_mode") == "mirror" else (1,)
        frames = [k5_force_trigger(s, 2, 0, 1, p2, p3) for s in sides]
        self._trig_mode = 2
        self._trig_burst_ts = 0.0        # 不走旧的固定时长兜底，由 _tr_tick 管
        self._last_trig = None
        with self._pend_lock:
            self._trig_q.extend(frames)
            if len(self._trig_q) > 4:
                del self._trig_q[:-4]

    def _tr_tick(self):
        """引擎停帧时抖动必须自己收（0x57 一停 on_out 就不再被调用）。"""
        tr = getattr(self, "_tr", None)
        if tr is None or not CFG.get("tr_enable"):
            return
        now = _t.time()
        idle = now - getattr(self, "_tr_last", 0.0)
        if idle > float(CFG.get("tr_release_ms", 120)) / 1000.0 + 0.35:
            if self._trig_mode == 2:
                self.trigger_rest()
            tr.reset()

    def _tf_tick(self):
        """引擎停帧时也要让力回落（0x57 一停，on_out 就不再被调用）。"""
        tf = getattr(self, "_tf", None)      # getattr：离线回归用 __new__ 造对象
        gate = getattr(self, "_tfg", None)
        if tf is None or gate is None or not CFG.get("tf_enable"):
            return
        now = _t.time()
        if now - getattr(self, "_tf_last", 0.0) < 0.30:
            return
        f = tf.step(now, 0)              # 当作静默：走 release 衰减
        q = tf.quantize(f)
        if gate.allow(now, q):
            self.set_damp(q, now)


class Apex6Virtual:
    def __init__(self, busid="1-1", k5=None):
        self.busid = busid
        self.k5 = k5
        self.configured = False
        self.in_queue = []
        self.ql = threading.Lock()
        self.reports = 0

    def push_in(self, frame):
        # FIFO：内核按序缓存报告给引擎的读取，顺序天然正确
        with self.ql:
            self.in_queue.append(frame)
            # 队列堆积诊断：depth>2 = 有应答没被读走（0x07 echo 等毒帧嫌疑）
            with _STATS_LOCK:
                d = len(self.in_queue)
                _STATS["q"] = d
                if d > _STATS.get("qmax", 0):
                    _STATS["qmax"] = d
                    _STATS["qcmd"] = "0x%02X" % (frame[2] if len(frame) > 2 else -1)

    def pop_in(self):
        with self.ql:
            return self.in_queue.pop(0) if self.in_queue else None

    def peek_in(self):
        with self.ql:
            return self.in_queue[0] if self.in_queue else None

    def on_out(self, data, ep=None):
        # ep1 = XInput 手柄端点：游戏/应用的震动指令直通 k5（无需音乐！）
        if ep == 1 and len(data) >= 6 and data[0] == 0x00 and data[1] == 0x08:
            # xusb22 震动报文 [00 08 LT RT ...]：字节 3/4 = 左/右马达 0-255
            lt, rt = data[3], data[4]
            if self.k5:
                # 转子补偿后直写（XInput 游戏震动 → k5 原生路径，无音频依赖）
                lo = min(255, int(lt * CFG["boost_x"] + CFG["boost_add"])) if lt else 0
                hi = min(255, int(rt * CFG["boost_x"] + CFG["boost_add"])) if rt else 0
                self.k5.rumble(lo, hi)
                self._xrumble = getattr(self, "_xrumble", 0) + 1
            return
        if len(data) == 33 and data[0] in (0x00, 0x03):
            data = data[1:]
        if len(data) < 5 or data[0] != 0x5A or data[1] != 0xA5:
            _dbg("[out] non-frame %dB: %s" % (len(data), data[:16].hex()))
            return
        cmd, ln = data[2], data[3]
        payload = data[5:3 + ln]
        self.reports += 1
        # ⚠ 绝不能清空应答队列（2026-11-14 逆向定案）：引擎的 0x53 同步 ACK 等待
        # deadline 为 1500ms/500ms（逆向常量 0x5DC/0x1F4），且引擎每 0.7s 发一次
        # GetInfo(0x01)——GetInfo 也在 NEEDS_REPLY 里，任何 purge 都会删掉尚未被
        # 读走的 0x53 ACK → ACK 超时 → realtime mode disable failed → 会话死。
        # FIFO + IN 轮询即时排水 = 队列最多堆 1-2 条，1.5s deadline 足够消化。
        # （旧注释关于 WebHID 双读抢应答的问题由内核广播机制天然规避）
        if cmd == 0x07:
            _dbg("[out #%d] 0x07 version -> echo back" % self.reports)
            self.push_in(bytes(data[:32]))
        elif cmd == 0x01:
            _dbg("[out #%d] GetInfo -> queue features=0xFF reply" % self.reports)
            self.push_in(getinfo_frame())
        elif cmd == 0x04:
            # 真机格式：应答 = [16B UUID golden][版本号 BCD]（引擎反复轮询 0x04 说明
            # 旧应答（纯版本号无 UUID）从不被接受 → 网页固件页回退默认值+更新提示）
            _dbg("[out #%d] 0x04 version -> UUID golden + versions" % self.reports)
            self.push_in(apex_frame(0x04, versions_payload()))
        elif cmd == 0x53:
            # 真机 k5 实测：0x53 应答 = 完整帧原样回显（含 len 和全部 payload）！
            # 伪造 len=3/[00] 的 ACK 与请求不匹配 → 引擎 route-sync 永远失败
            _diag("0x53 REQ payload=%s -> 全帧回显" % payload.hex())
            self.push_in(bytes(data[:32]))
        elif cmd == 0x57:
            # ⚠ 绝不能让异常穿透：decode_57 一旦抛错会杀死整个 usbip 连接
            # （2026-11-14 定案根因：返回 2 值 vs 调用方解包 3 值 →
            #  "controller rejected 100 consecutive realtime output frames"）
            try:
                t, l, r = decode_57(payload)
            except Exception as e:
                print("[out #%d] 0x57 decode err: %r payload=%s" %
                      (self.reports, e, payload[:12].hex()), flush=True)
                t, l, r = 0, 0, 0
            with _STATS_LOCK:
                _STATS["n"] += 1
                if l or r or t:
                    _STATS["nz"] += 1
                    _STATS["ml"] = max(_STATS["ml"], l, t)
                    _STATS["mr"] = max(_STATS["mr"], r)
                # 三通道统计（纯内存，绝不文件 I/O——urb 线程里任何 >8ms 阻塞都会
                # 触发引擎"写入失败"累积→弃权实时流。2026-11-14 冻结根因）
                if t:
                    _STATS["tnz"] = _STATS.get("tnz", 0) + 1
                _STATS["tmax"] = max(_STATS.get("tmax", 0), abs(t))
                _STATS["lmax"] = max(_STATS.get("lmax", 0), l)
                _STATS["rmax"] = max(_STATS.get("rmax", 0), r)
                _STATS["kw"] = self.k5.writes if self.k5 else -1
            if self.k5 and not getattr(self.k5, "_mute", False):
                # ---- 扳机 v4：常态化轻阻力底座 + 鼓点短暂爆发（爆发后回落底座，永不释放）----
                # 事实来源 = k5._trig_mode。写入只发生在：强度档位变化、爆发→底座回落。
                # 无音乐时保持底座阻力（永不 mode 0）——用户要求"常态+节奏时一起动"。
                now = _t.time()
                tm = self.k5._trig_mode
                # 转子静默 = 音乐确实停了。停音后引擎的扳机通道仍会间歇输出
                # 高于门限的残响（11-15 11:29 实测：25s 内 +37 帧），每一个
                # 都会重新发 mode 2 并刷新回落窗口，导致扳机被残响「续命」
                # 一直震。握把已静默 1 秒就不应再起新的爆发。
                _rotor_idle = (now - self.k5._last_nz[0]) if self.k5._last_nz[0] else 0.0
                # 力包络模式优先：mode 2 机枪爆发让位（两者同开会互相抢扳机）
                _tf = getattr(self.k5, "_tf", None)
                _tg = getattr(self.k5, "_tfg", None)
                if CFG.get("tf_enable") and _tf is not None and _tg is not None:
                    self.k5._tf_last = now
                    _q = _tf.quantize(_tf.step(now, t))
                    if _tg.allow(now, _q):
                        self.k5.set_damp(_q, now)
                # 机枪抖动 v2：头/身/锁/释放，包络直接驱动 mode 2
                _tr = getattr(self.k5, "_tr", None)
                _trg = getattr(self.k5, "_trg", None)
                if CFG.get("tr_enable") and _tr is not None and _trg is not None:
                    self.k5._tr_last = now
                    _dt = now - getattr(self, "_tr_dt", now)
                    self._tr_dt = now
                    _act = _tr.update(now, t, max(0.004, min(0.05, _dt)))
                    if _act:
                        if _act[0] == "set":
                            if _trg.allow(now, _act[1]):
                                self.k5.set_rattle(_act[1], _act[2])
                        else:
                            self.k5.trigger_rest()
                            _trg.reset()
                _hit = bool(CFG.get("trig_burst") and not CFG.get("tf_enable")
                            and not CFG.get("tr_enable") and t
                            and abs(t) >= CFG["trig_threshold"]
                            and _rotor_idle < 1.0)
                if _hit:
                    if tm != 2:
                        raw = min(200, max(55, abs(t) * 4))
                        intens = min((55, 90, 130, 170, 200), key=lambda v: abs(v - raw))
                        praw = max(40, min(70, abs(t) * 2))
                        p3v = min((40, 50, 60, 70), key=lambda v: abs(v - praw))
                        self.k5.trigger(1, 2, 0, 1, intens, p3v)   # 鼓点爆发
                    self._trig_burst_until = now + CFG["trig_burst_ms"] / 1000.0
                    self._trig_state = "burst"
                # ⚠ 旧的"爆发到时回落"只服务老逻辑。tr/tf 模式开着时必须让位，
                # 否则渲染器刚把 mode 2 设上，下一帧就被这里改回 mode 1（12:30 实测踩到：
                # 扳机只剩一串 m1 帧、完全没有抖动）。
                elif (not CFG.get("tr_enable") and not CFG.get("tf_enable")
                      and tm == 2 and (now > getattr(self, "_trig_burst_until", 0)
                                       or _rotor_idle > 1.0)):
                    self.k5.trigger_rest()   # 爆发毕→回落常态轻阻力（与 open_dev 参数一致）
                    self._trig_state = "push"
                # tm==1：常态保持，零写入
                # ---- 转子映射：纯握把通道（t 不再漏入转子——2026-11-14 用户反馈"炸了"）----
                lo = max(l, 0)
                hi = max(r, 0)
                self.k5.rumble(lo, hi)
        elif cmd in (0xA1, 0xA3, 0xA4):
            # 配置协议。帧格式（实测往返确认）：
            #   请求 = 5A A5 cmd [len u8] [payload len B] …零填充到 32B（sum@[31]）
            #     实测 A3：5aa5a3 [06] [00 06 00 14 00 00] → len=6, payload=[area0,ramId6,cidx0,size20,0,0]
            #   应答 = 5A A5 cmd [payload 28B] [sum]（3 字节头，28+3+1=32 恰好满帧！）
            # 16:10 事故=请求解析错→拒答→引擎 A3 ACK 超时→"HID write suspended"→0x57 停发
            # = 用户所见全部冻结（引擎诊断快照 link.lastError 铁证）。
            if CFG.get("config_proto"):
                if cmd == 0xA1:
                    _diag("0xA1 REQ -> slot status reply")
                    self.push_in(apex_frame(0xA1, A1_SLOT_STATUS))
                elif cmd in (0xA3, 0xA4):
                    ln = data[3]
                    p = data[4:4 + ln] if 0 < ln <= 27 else data[4:31]
                    if cmd == 0xA3:
                        if len(p) >= 4 and p[0] == 0x00:
                            _diag("0xA3 REQ %s -> reply" % p.hex())
                            self.push_in(handle_a3_read(p))
                        else:
                            _diag("0xA3 未知布局: %s" % data[:16].hex())
                    else:
                        reply = handle_a4_write(p) if p else None
                        if reply:
                            _diag("0xA4 REQ %s -> reply" % p.hex())
                            self.push_in(reply)
                        else:
                            _diag("0xA4 未知子命令: %s" % data[:16].hex())
            # 关闭时静默忽略（=运行数日证明稳定的旧行为）
        else:
            # 未知命令（0x02/0xFC 等）：不回应答，避免垃圾帧
            _dbg("[out #%d] ignore unknown cmd=0x%02X" %
                 (self.reports, cmd))

    def control(self, setup, out_data):
        bmRequestType, bRequest, wValue, wIndex, wLength = struct.unpack("<BBHHH", setup)
        dtype, dindex = (wValue >> 8) & 0xFF, wValue & 0xFF
        _dbg("[ctl] type=0x%02X req=0x%02X val=0x%04X idx=0x%04X len=%d" % (
            bmRequestType, bRequest, wValue, wIndex, wLength))
        if bmRequestType & 0x80:
            if bRequest == 0x06:
                if dtype == 0x01:
                    return 0, DEVICE_DESC[:wLength]
                if dtype == 0x02:
                    return 0, CONFIG_DESC[:wLength]
                if dtype == 0x0F:
                    return 0, BOS_DESC[:wLength]
                if dtype == 0x03:
                    if dindex == 0:
                        return 0, struct.pack("<BBH", 4, 0x03, 0x0409)[:wLength]
                    if dindex == 0xEE:
                        return 0, MS_OS_STRING[:wLength]
                    return 0, string_desc(STRINGS.get(dindex, ""))[:wLength]
                if dtype == 0x22:
                    return 0, HID_REPORT_DESC[:wLength]
                if dtype == 0x21:
                    if wIndex == 0:
                        return 0, XUSB_UNKNOWN_DESC[:wLength]
                    return 0, CONFIG_DESC[58:67][:wLength]
                return -1, b""
            if bRequest == 0x00:
                return 0, b"\x00\x00"[:wLength]
            # MS OS vendor request: bmRequestType=0xC0, bRequest=vendor code
            # wIndex=0x0007 → MS OS 2.0 descriptor set；wIndex=0x0004 → 1.0 Extended Compat ID
            if bmRequestType == 0xC0 and bRequest == MS_VENDOR_CODE:
                if wIndex == 0x0007:
                    print("[ctl] -> MS OS 2.0 descriptor set (%d B, want %d)" % (len(MS_OS_20_SET), wLength), flush=True)
                    return 0, MS_OS_20_SET[:wLength]
                print("[ctl] -> MS OS 1.0 Extended Compat ID XUSB10 (%d B, want %d)" % (len(EXT_COMPAT_ID), wLength), flush=True)
                return 0, EXT_COMPAT_ID[:wLength]
            if bRequest == 0x01:
                f = self.pop_in() or bytes(32)
                return 0, f[:wLength]
            if bRequest == 0x08:
                return 0, bytes([1 if self.configured else 0])
            return -1, b""
        if bRequest == 0x09:
            self.configured = (wValue & 0xFF) != 0
            print("[usb] SET_CONFIGURATION ->", self.configured, flush=True)
            return 0, b""
        if bRequest == 0x0B or bRequest == 0x0A:
            return 0, b""
        if bRequest == 0x01:
            self.on_out(out_data)
            return 0, b""
        return 0, b""

    def bulk_out(self, ep, data):
        self.on_out(data, ep=ep)
        return 0, len(data)

    def bulk_in(self, ep, length):
        if ep == 0x01:  # MI_00 XInput IN (0x81)：xusb22 需要 20B Xbox360 格式报告
            # 虚拟 6Pro 输入数据注入（22:5x 用户澄清：引擎需要看到控制器有输入数据
            # 才保持活跃——没输入数据引擎就"不管"这台设备，扳机阻力等不恢复）。
            # 用 LT 扳机模拟量代替按键：每 0.5s 半扣一次（按键位=零，不干扰应用）。
            pressed = (int(_t.time() * 2) % 2) == 0
            rep = bytearray(20)
            rep[0], rep[1] = 0x00, 0x14
            if pressed:
                rep[4] = 0x28   # LT 半按（扳机模拟量，按键位全零）
            return 0, bytes(rep)
        f = self.pop_in()
        if f is None:
            f = bytearray(32)
            f[0], f[1] = 0x5A, 0xA5
        return 0, f[:length] if length < 32 else f


def string_desc(s):
    raw = s.encode("utf-16-le")
    return bytes([len(raw) + 2, 0x03]) + raw


def usb_device_struct(busid):
    b = bytearray(312)
    p = b"/sys/devices/platform/vhci_hcd.0/usb1/" + busid.encode()[:64]
    b[0:len(p)] = p
    bid = busid.encode()[:31]
    b[256:256 + len(bid)] = bid
    struct.pack_into(">II", b, 288, 1, 2)
    struct.pack_into(">I", b, 296, 3)
    struct.pack_into(">HHH", b, 300, VID, PID, BCD_DEVICE)
    b[310], b[311] = 0x01, 0x01
    return bytes(b)


class UsbIpServer:
    def __init__(self, dev, port):
        self.dev = dev
        self.port = port
        self._send_lock = threading.Lock()  # 多线程共享 conn，写必须串行

    def serve(self):
        # TCP 层（从 v3 备份原样恢复）：2026-11-08 移除 flusher 时误删了这两个方法，
        # 导致服务端一启动就 AttributeError: no attribute 'serve'。此层不含任何 flusher 逻辑。
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", self.port))
        s.listen(4)
        print("[usbip] listening 127.0.0.1:%d busid=%s" % (self.port, self.dev.busid), flush=True)
        while True:
            conn, _ = s.accept()
            _nodelay(conn)
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn):
        print("[usbip] client connected", flush=True)
        try:
            while True:
                hdr = self._recv(conn, 8)
                if not hdr:
                    break
                ver, cmd, status = struct.unpack(">HHI", hdr)
                if cmd == 0x8005:
                    self._devlist(conn, ver)
                elif cmd == 0x8003:
                    busid = self._recv(conn, 32).split(b"\x00")[0].decode()
                    self._import(conn, ver, busid)
                    self._urb_loop(conn)
                    break
                else:
                    print("[usbip] unknown cmd 0x%04X" % cmd, flush=True)
                    break
        except Exception as e:
            print("[usbip] client error:", e, flush=True)
        finally:
            conn.close()

    def _devlist(self, conn, ver):
        body = struct.pack(">HHI", ver, 0x0005, 0) + struct.pack(">I", 1)
        body += usb_device_struct(self.dev.busid)
        body += struct.pack(">I", 1) + struct.pack(">BBBB", 0x03, 0, 0, 0)
        conn.sendall(body)

    def _import(self, conn, ver, busid):
        body = struct.pack(">HHI", ver, 0x0003, 0) + usb_device_struct(busid)
        conn.sendall(body)
        print("[usbip] IMPORT %s -> %d B" % (busid, len(body)), flush=True)

    def _recv(self, conn, n):
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf += chunk
        return buf

    def _urb_loop(self, conn):
        seq_log = []
        while True:
            # 到达延迟测量：hdr 等待=客户端(内核VHCI)提交 urb 的延迟；data 等待=
            # 第二段(TCP 分段/Nagle×延迟ACK 死锁)的延迟。引擎写超时=这两段的和。
            _t0h = _t.time()
            hdr = self._recv(conn, 48)
            _hw = (_t.time() - _t0h) * 1000.0
            if _hw > 8.0:
                with _STATS_LOCK:
                    _STATS["hw"] = round(max(_STATS.get("hw", 0), _hw), 1)
                    _STATS["hwn"] = _STATS.get("hwn", 0) + 1
            if not hdr:
                print("[usbip] URB connection closed", flush=True)
                return
            (command, seqnum, devid, direction, ep,
             tflags, tlen, sframe, npkts, interval) = struct.unpack(">IIIIIiIIII", hdr[:40])
            setup = hdr[40:48]
            _t0d = _t.time()
            data = self._recv(conn, tlen) if (direction == 0 and tlen > 0) else b""
            if direction == 0 and tlen > 0:
                _dw = (_t.time() - _t0d) * 1000.0
                if _dw > 8.0:
                    with _STATS_LOCK:
                        _STATS["dw"] = round(max(_STATS.get("dw", 0), _dw), 1)
                        _STATS["dwn"] = _STATS.get("dwn", 0) + 1

            if command == 0x00000002:
                with self._send_lock:
                    conn.sendall(struct.pack(">IIIIIiiiii8x", 3, seqnum, 0, 0, 0, 0, 0, 0, -1, 0))
                continue
            if command != 0x00000001:
                print("[usbip] unknown URB cmd=0x%X" % command, flush=True)
                continue

            status, out = 0, b""
            _t0 = _t.time()
            if ep == 0:
                status, out = self.dev.control(setup, data)
            elif direction == 0:
                status, n = self.dev.bulk_out(ep, data)
                out = b""
            else:
                # 即时应答（golden 行为）：引擎的 HID 读超时只有 250ms，
                # 挂起/延迟应答会触发 "hid timed out after 250ms" + 写挂起
                status, out = self.dev.bulk_in(ep, tlen)

            actual = len(out)
            # 延迟观测：>8ms 记入 _STATS（引擎写入间隔预算 8ms，超了就是"USB 写入失败"）
            _lat = (_t.time() - _t0) * 1000.0
            if _lat > 8.0:
                with _STATS_LOCK:
                    _STATS["maxlat"] = round(max(_STATS["maxlat"], _lat), 1)
                    _STATS["slow"] += 1
            ret = struct.pack(">IIIIIiiiii8x", 0x00000003, seqnum, 0, 0, 0,
                              status, actual if direction == 1 else 0, 0, -1, 0)
            with self._send_lock:
                conn.sendall(ret + (out if direction == 1 else b""))
            seq_log.append((seqnum, ep, direction, actual))
            if len(seq_log) >= 200:
                _dbg("[urb] " + " ".join("s%d:e%d%s%d" % (s, e, "I" if d else "O", a)
                                          for s, e, d, a in seq_log))
                seq_log = []


def main():
    # 服务端日志统一 UTF-8（原来随系统 locale 写 GBK，发布包里 srv_out.log 中文乱码）
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    load_cfg()
    # P2：系统定时器粒度提到 1ms（Windows 默认 ~15.6ms，k5_min_iv=80ms 时抖动
    # 占比近两成）。sleep 精度直接决定 DPC 节流节拍是否稳定。进程退出时系统会
    # 自动回收该请求，atexit 仍显式配对还原（timeBeginPeriod 规范要求）。
    try:
        _winmm = ctypes.WinDLL("winmm")
        if _winmm.timeBeginPeriod(1) == 0:
            atexit.register(_winmm.timeEndPeriod, 1)
    except Exception:
        pass
    threading.Thread(target=_cfg_watcher, daemon=True).start()
    threading.Thread(target=_stats_watcher, daemon=True).start()
    ap = argparse.ArgumentParser()
    # 服务端在 3241；tcp_proxy2.py 监听 3240 转发到 3241，usbip attach -t 3240。
    # 旧默认值 3240 会和代理抢同一监听端口（Windows SO_REUSEADDR 下连接归属不确定）
    ap.add_argument("--port", type=int, default=3241)
    ap.add_argument("--busid", default="1-1")
    args = ap.parse_args()

    k5 = K5()
    dev = Apex6Virtual(busid=args.busid, k5=k5)
    print("=== Virtual Flydigi APEX 6 Pro v2 ===")
    print("  VID 0x%04X PID 0x%04X page 0xFFA0 in/out 32B  GetInfo features=0xFF" % (VID, PID))
    UsbIpServer(dev, args.port).serve()


if __name__ == "__main__":
    main()
