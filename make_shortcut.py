# make_shortcut.py — 生成 Windows 快捷方式（.lnk），不依赖 COM
#
# 为什么不用 WScript.Shell / IShellLink：本机安全策略禁止 COM 实例化。
# 为什么不能只写 LinkInfo：Win11 的外壳要求 .lnk 带 LinkTargetIDList，
#   缺了它会直接返回 SE_ERR_ACCESSDENIED（错误码 5），双击毫无反应。
# 做法：用 shell32.ILCreateFromPathW 让系统自己算出目标路径的 PIDL，
#   把那段字节原样写进 .lnk 的 LinkTargetIDList —— 纯 C API，无 COM。
#
# 实测（2026-11-15）：ShellExecuteW 返回 42，双击正常。
#
# 用法：
#   python make_shortcut.py <目标exe> [快捷方式路径] [工作目录]
import ctypes
import os
import struct
import sys

shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shell32.ILCreateFromPathW.restype = ctypes.c_void_p
shell32.ILCreateFromPathW.argtypes = [ctypes.c_wchar_p]
shell32.ILFree.argtypes = [ctypes.c_void_p]
shell32.ShellExecuteW.restype = ctypes.c_void_p


def _idlist(path):
    """目标路径的 PIDL 原始字节（含结尾 2 字节 0）。"""
    pidl = shell32.ILCreateFromPathW(path)
    if not pidl:
        raise RuntimeError("ILCreateFromPath 失败: %s" % path)
    buf = ctypes.cast(pidl, ctypes.POINTER(ctypes.c_uint8))
    i = 0
    while True:
        cb = buf[i] | (buf[i + 1] << 8)
        if cb == 0:
            i += 2
            break
        i += cb
    raw = bytes(bytearray(buf[k] for k in range(i)))
    shell32.ILFree(ctypes.c_void_p(pidl))
    return raw


def _ustr(s):
    """IsUnicode 时长度单位是 UTF-16 字符数，不是字节数。"""
    return struct.pack("<H", len(s)) + s.encode("utf-16-le")


def make_lnk(lnk_path, target, workdir, description=""):
    target = str(target).replace("/", "\\")
    workdir = str(workdir).replace("/", "\\")
    if not os.path.isfile(target):
        raise SystemExit("目标不存在: %s" % target)

    il = _idlist(target)

    flags = 0x01 | 0x02 | 0x10 | 0x80      # IDList | LinkInfo | WorkingDir | IsUnicode
    h = bytearray(76)
    struct.pack_into("<I", h, 0x00, 0x4C)
    h[0x04:0x14] = bytes.fromhex("0114020000000000C000000000000046")
    struct.pack_into("<I", h, 0x14, flags)
    struct.pack_into("<I", h, 0x18, 0x20)          # FILE_ATTRIBUTE_ARCHIVE
    struct.pack_into("<i", h, 0x3C, 1)             # SW_SHOWNORMAL

    head_idlist = struct.pack("<H", len(il)) + il

    volid = struct.pack("<IIII", 0x11, 3, 0, 0x10) + b"\x00"   # 空标签，Size=17
    base = target.encode("mbcs", "replace") + b"\x00"
    suffix = b"\x00"
    hs = 0x1C
    vo = hs
    bo = vo + len(volid)
    so = bo + len(base)
    total = so + len(suffix)
    linkinfo = (struct.pack("<IIIIIII", total, hs, 0x01, vo, bo, 0, so)
                + volid + base + suffix)

    stringdata = _ustr(workdir)
    extra = struct.pack("<I", 0)                   # TerminalBlock

    data = bytes(h) + head_idlist + linkinfo + stringdata + extra
    with open(lnk_path, "wb") as f:
        f.write(data)
    return len(data)


def verify(lnk_path):
    """回读自检 + 让外壳解析一次（返回值 >32 才算能被双击执行）。"""
    data = open(lnk_path, "rb").read()
    assert struct.unpack_from("<I", data, 0)[0] == 0x4C, "HeaderSize 不对"
    flags = struct.unpack_from("<I", data, 0x14)[0]
    assert flags & 0x01, "缺 IDList 标志（Win11 会拒绝）"
    assert flags & 0x80, "缺 IsUnicode 标志"
    ctypes.set_last_error(0)
    r = int(shell32.ShellExecuteW(None, "open", lnk_path, None, None, 1))
    return r


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(2)
    target = sys.argv[1]
    lnk = sys.argv[2] if len(sys.argv) > 2 else os.path.join(
        os.path.dirname(target), os.path.splitext(os.path.basename(target))[0] + ".lnk")
    wd = sys.argv[3] if len(sys.argv) > 3 else os.path.dirname(target)
    n = make_lnk(lnk, target, wd)
    r = verify(lnk)
    print("已写入 %s（%d 字节）" % (lnk, n))
    print("外壳解析结果: %d %s" % (r, "OK" if r > 32 else "失败（<=32 是错误码）"))
