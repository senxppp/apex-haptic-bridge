# tcp_proxy2.py — 3240→3241 代理：采样记录流量 + mtime 心跳（watchdog 用）
# 高频轮询(20B/32B)每秒最多记 1 条，其余全记；任何字节流动都刷新 proxy_traffic.log 的 mtime
import os
import socket
import threading
import time

LISTEN, TARGET = 3240, 3241
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "proxy_traffic.log")
lock = threading.Lock()
t0 = time.time()
_last_log = {"C>": 0.0, "S<": 0.0}


def log(tag, data):
    with lock:
        with open(LOG, "a", encoding="utf-8", errors="replace") as f:
            ts = time.time() - t0
            if len(data) <= 64:
                hexs = data.hex()
            else:
                hexs = data[:32].hex() + "..."
            f.write("[%.3f] %s %4d B: %s\n" % (ts, tag, len(data), hexs))


_last_touch = 0.0


def touch():
    global _last_touch
    now = time.time()
    if now - _last_touch < 1.0:
        return
    _last_touch = now
    try:
        os.utime(LOG)
    except OSError:
        pass


def pump(src, dst, tag):
    global _last_log
    try:
        while True:
            d = src.recv(65536)
            if not d:
                log(tag + "-EOF", b"")
                break
            touch()
            now = time.time()
            # 20B(XInput轮询)/32B(HID轮询) 采样：每方向每秒记1条
            if len(d) in (20, 32, 48, 68, 80):
                if now - _last_log[tag] >= 1.0:
                    _last_log[tag] = now
                    log(tag, d)
            else:
                log(tag, d)
            dst.sendall(d)
    except Exception as e:
        log(tag + "-ERR", str(e).encode())
    finally:
        try:
            dst.shutdown(socket.SHUT_WR)
        except Exception:
            pass


srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
srv.bind(("127.0.0.1", LISTEN))
srv.listen(2)
log("LISTEN", b"proxy 3240->3241")
while True:
    c, a = srv.accept()
    c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.connect(("127.0.0.1", TARGET))
    log("NEW-CONN", str(a).encode())
    threading.Thread(target=pump, args=(c, s, "C>"), daemon=True).start()
    threading.Thread(target=pump, args=(s, c, "S<"), daemon=True).start()
