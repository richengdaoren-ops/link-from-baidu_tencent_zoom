#!/usr/bin/env python3
"""零依赖（仅标准库）的 Chrome DevTools Protocol 客户端 —— Linux 版的浏览器驱动层。

替代 macOS 版依赖的 localhost:3456 CDP 代理：直接连 Chrome 自己的
--remote-debugging-port（默认 9333），用 WebSocket 发 CDP 命令。
不依赖 Hermes/Agent 的 browser 工具（在无 root 的 Ubuntu VM 上它常因
Chrome 沙箱起不来而报笼统的 "Chromium browser is missing"）。

两种用法：

1) 作为库：
    from cdp_client import Browser
    b = Browser(port=9333)
    tab = b.open("https://meeting.tencent.com/cw/xxxx")
    s = b.session(tab)
    s.evaluate("document.title")
    s.cookie_header(["https://meeting.tencent.com/"])

   以及 legacy_call()：模拟旧代理的 /targets /new /navigate /eval /close
   接口，让 download_my_recordings.py 等老脚本不改业务逻辑即可在 Linux 跑。

2) 命令行：
    python3 cdp_client.py status
    python3 cdp_client.py tabs
    python3 cdp_client.py open  <url>
    python3 cdp_client.py eval  <url子串|targetId> '<js>'
    python3 cdp_client.py shot  <url子串|targetId> out.png
    python3 cdp_client.py cookies <url子串|targetId> [--names]   # 只打印 cookie 名，不打印值
    python3 cdp_client.py close <url子串|targetId>
"""
import base64
import json
import os
import random
import socket
import ssl
import struct
import sys
import time
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).parent))
import linux_env  # noqa: E402


class CDPError(RuntimeError):
    pass


# ── 极简 WebSocket（RFC 6455，仅客户端、无扩展） ─────────────────────────

class WebSocket:
    def __init__(self, url, timeout=30):
        u = urlparse(url)
        host = u.hostname
        port = u.port or (443 if u.scheme == "wss" else 80)
        path = u.path + (f"?{u.query}" if u.query else "")
        sock = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket handshake failed: connection closed")
            resp += chunk
        head, _, rest = resp.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0]:
            raise CDPError(f"WebSocket handshake failed: {head[:200]!r}")
        self.sock = sock
        self.buf = rest
        self.timeout = timeout

    def _recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(max(65536, n - len(self.buf)))
            if not chunk:
                raise CDPError("WebSocket closed")
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    def send(self, text):
        payload = text.encode("utf-8")
        header = bytearray([0x81])
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack(">H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv(self, timeout=None):
        self.sock.settimeout(timeout if timeout is not None else self.timeout)
        message = b""
        while True:
            b1, b2 = self._recv_exact(2)
            fin, opcode = b1 & 0x80, b1 & 0x0F
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._recv_exact(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._recv_exact(8))[0]
            if b2 & 0x80:
                mask = self._recv_exact(4)
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(self._recv_exact(n)))
            else:
                data = self._recv_exact(n)
            if opcode == 0x8:
                raise CDPError("WebSocket closed by peer")
            if opcode == 0x9:  # ping -> pong
                self.sock.sendall(bytes([0x8A, 0x80]) + os.urandom(4))
                continue
            if opcode == 0xA:
                continue
            message += data
            if fin:
                return message.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.sendall(bytes([0x88, 0x80]) + os.urandom(4))
        except OSError:
            pass
        self.sock.close()


# ── CDP 会话 ──────────────────────────────────────────────────────────

class Session:
    def __init__(self, ws_url, target_id=""):
        self.ws = WebSocket(ws_url)
        self.target_id = target_id
        self.next_id = 0
        self.events = []  # 未消费的事件 (method, params)

    def send(self, method, params=None, timeout=60):
        self.next_id += 1
        mid = self.next_id
        self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise CDPError(f"CDP timeout: {method}")
            try:
                msg = json.loads(self.ws.recv(timeout=left))
            except socket.timeout:
                raise CDPError(f"CDP timeout: {method}")
            if msg.get("id") == mid:
                if "error" in msg:
                    raise CDPError(f"{method}: {msg['error']}")
                return msg.get("result", {})
            if "method" in msg:
                self.events.append((msg["method"], msg.get("params", {})))

    def pump(self, seconds):
        """在 seconds 内持续收事件，存入 self.events。"""
        deadline = time.time() + seconds
        while True:
            left = deadline - time.time()
            if left <= 0:
                return
            try:
                msg = json.loads(self.ws.recv(timeout=left))
            except (socket.timeout, TimeoutError):
                return
            if "method" in msg:
                self.events.append((msg["method"], msg.get("params", {})))

    def evaluate(self, expression, await_promise=True, timeout=60):
        r = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": await_promise,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if r.get("exceptionDetails"):
            detail = r["exceptionDetails"]
            text = (detail.get("exception") or {}).get("description") or detail.get("text")
            raise CDPError(f"JS exception: {str(text)[:300]}")
        return (r.get("result") or {}).get("value")

    def navigate(self, url, wait=True, timeout=30):
        self.send("Page.enable")
        self.send("Page.navigate", {"url": url})
        if not wait:
            return
        deadline = time.time() + timeout
        time.sleep(0.5)
        while time.time() < deadline:
            try:
                if self.evaluate("document.readyState", timeout=10) == "complete":
                    return
            except CDPError:
                pass
            time.sleep(0.5)

    def wait_for(self, js_condition, timeout=30, interval=1.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if self.evaluate(js_condition, timeout=10):
                    return True
            except CDPError:
                pass
            time.sleep(interval)
        return False

    def cookies(self, urls):
        return self.send("Network.getCookies", {"urls": list(urls)}).get("cookies", [])

    def cookie_header(self, urls):
        """包含 HttpOnly 的完整 Cookie 头（document.cookie 读不到 HttpOnly）。"""
        seen = {}
        for c in self.cookies(urls):
            seen.setdefault(c["name"], c["value"])
        return "; ".join(f"{k}={v}" for k, v in seen.items())

    def user_agent(self):
        try:
            return linux_env.clean_ua(self.evaluate("navigator.userAgent"))
        except CDPError:
            return linux_env.FALLBACK_UA

    def screenshot(self, path, full_page=False):
        params = {"format": "png"}
        if full_page:
            params["captureBeyondViewport"] = True
        data = self.send("Page.captureScreenshot", params)["data"]
        Path(path).write_bytes(base64.b64decode(data))
        return str(path)

    def close(self):
        self.ws.close()


class Browser:
    def __init__(self, host=None, port=None):
        self.host = host or linux_env.CDP_HOST
        self.port = int(port or linux_env.CDP_PORT)
        self._sessions = {}

    def _http(self, path, method="GET", timeout=10):
        req = Request(f"http://{self.host}:{self.port}{path}", method=method)
        # 调试端口在本机，绕过 HTTP(S)_PROXY
        from urllib.request import build_opener, ProxyHandler

        opener = build_opener(ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw

    def alive(self):
        try:
            return self._http("/json/version", timeout=3)
        except Exception:
            return None

    def require(self):
        info = self.alive()
        if not info:
            raise SystemExit(
                f"连不上 Chrome 调试端口 {self.host}:{self.port}。先运行 scripts/start_chrome.sh"
                f"{' --headful --port ' + str(self.port) if self.port == linux_env.ZOOM_CDP_PORT else ''}"
            )
        return info

    def targets(self):
        return [t for t in self._http("/json/list") if t.get("type") == "page"]

    def find(self, key):
        """按 targetId 或 URL 子串找页面。"""
        for t in self.targets():
            if t.get("id") == key:
                return t
        for t in self.targets():
            if key and key in t.get("url", ""):
                return t
        return None

    def open(self, url="about:blank"):
        q = quote(url, safe=":/?&=%#@+,;~")
        try:
            t = self._http(f"/json/new?{q}", method="PUT")  # Chrome ≥111 要求 PUT
        except Exception:
            t = self._http(f"/json/new?{q}")
        if not isinstance(t, dict) or "id" not in t:
            raise CDPError(f"open tab failed: {t}")
        return t

    def session(self, target):
        if isinstance(target, str):
            t = self.find(target)
            if not t:
                raise CDPError(f"no such tab: {target}")
            target = t
        tid = target["id"]
        s = self._sessions.get(tid)
        if s is None:
            s = Session(target["webSocketDebuggerUrl"], tid)
            self._sessions[tid] = s
        return s

    def close_tab(self, target_id):
        s = self._sessions.pop(target_id, None)
        if s:
            s.close()
        try:
            self._http(f"/json/close/{target_id}")
        except Exception:
            pass

    def get_or_open(self, url_substring, url):
        t = self.find(url_substring)
        if t:
            return t
        t = self.open(url)
        time.sleep(3)
        return t


# ── 兼容旧 localhost:3456 代理接口 ──────────────────────────────────

_default_browser = None


def default_browser(port=None):
    global _default_browser
    if _default_browser is None or (port and _default_browser.port != int(port)):
        _default_browser = Browser(port=port)
        _default_browser.require()
    return _default_browser


def legacy_call(method, path, body=None, port=None, timeout=30):
    """模拟旧代理：/targets /new?url= /navigate?target=&url= /eval?target= /close?target="""
    b = default_browser(port)
    u = urlparse(path)
    q = dict(parse_qsl(u.query, keep_blank_values=True))
    if u.path == "/targets":
        return [{"targetId": t["id"], "type": t["type"], "url": t.get("url", ""), "title": t.get("title", "")} for t in b.targets()]
    if u.path == "/new":
        t = b.open(q.get("url", "about:blank"))
        return {"targetId": t["id"]}
    if u.path == "/navigate":
        b.session(q["target"]).navigate(q["url"], wait=False)
        return {"ok": True}
    if u.path == "/eval":
        js = body.decode("utf-8") if isinstance(body, bytes) else str(body or "")
        return {"value": b.session(q["target"]).evaluate(js, timeout=timeout)}
    if u.path == "/close":
        b.close_tab(q["target"])
        return {"ok": True}
    raise CDPError(f"unsupported legacy path: {path}")


# ── CLI ───────────────────────────────────────────────────────────────

def _cli():
    import argparse

    ap = argparse.ArgumentParser(description="Linux CDP helper")
    ap.add_argument("--port", type=int, default=linux_env.CDP_PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("tabs")
    p = sub.add_parser("open"); p.add_argument("url")
    p = sub.add_parser("eval"); p.add_argument("tab"); p.add_argument("js")
    p = sub.add_parser("shot"); p.add_argument("tab"); p.add_argument("out"); p.add_argument("--full", action="store_true")
    p = sub.add_parser("cookies"); p.add_argument("tab"); p.add_argument("--url", action="append")
    p = sub.add_parser("close"); p.add_argument("tab")
    a = ap.parse_args()

    b = Browser(port=a.port)
    info = b.require()
    if a.cmd == "status":
        print(json.dumps({"browser": info.get("Browser"), "ua": info.get("User-Agent"), "tabs": len(b.targets())}, ensure_ascii=False))
    elif a.cmd == "tabs":
        for t in b.targets():
            print(f"{t['id']}  {t.get('url','')[:100]}  {t.get('title','')[:40]}")
    elif a.cmd == "open":
        t = b.open(a.url)
        print(t["id"])
    elif a.cmd == "eval":
        v = b.session(a.tab).evaluate(a.js)
        print(v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2))
    elif a.cmd == "shot":
        print(b.session(a.tab).screenshot(a.out, a.full))
    elif a.cmd == "cookies":
        s = b.session(a.tab)
        urls = a.url or [s.evaluate("location.href")]
        names = sorted({c["name"] for c in s.cookies(urls)})
        print(json.dumps(names, ensure_ascii=False))  # 只输出名字，值是敏感信息
    elif a.cmd == "close":
        t = b.find(a.tab)
        if t:
            b.close_tab(t["id"])


if __name__ == "__main__":
    _cli()
