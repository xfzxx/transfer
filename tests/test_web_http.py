# -*- coding: utf-8 -*-
"""Web 档 HTTP 面判据：路由、404、SSE 真的在流。

**为什么用假桥**：真桥 `WebBridge.snapshot()` 会 `call_on_main` 投递到主线程再等，
测试里没人泵主循环就会 5 秒超时。这里要验的是 **HTTP 层**（路由对不对、头对不对、
流有没有真的一帧帧出去），不是装配 —— 那部分由 `test_bindings.py` 走真实全装配。
"""
import json
import socket
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import web_app


class FakeBridge(object):
    def __init__(self):
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        return {"title": "fake", "widgets": {}, "vars": {}, "n": self.calls}

    def ping(self):
        return {"app": "transfer", "port": 0}


@pytest.fixture()
def server():
    bridge = FakeBridge()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), web_app._make_handler(bridge))
    srv.daemon_threads = True
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield bridge, srv.server_address[1]
    srv.shutdown()
    srv.server_close()


def _get(port, path, timeout=5):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path),
                                    timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_state_endpoint_returns_snapshot(server):
    bridge, port = server
    code, body = _get(port, "/api/state")
    assert code == 200
    data = json.loads(body.decode("utf-8"))
    assert data["title"] == "fake"
    assert bridge.calls >= 1


def test_ping_can_identify_us(server):
    _, port = server
    code, body = _get(port, "/api/ping")
    assert code == 200
    assert json.loads(body.decode("utf-8"))["app"] == "transfer"


def test_unknown_path_is_404(server):
    _, port = server
    code, _ = _get(port, "/api/nope")
    assert code == 404


def test_static_path_traversal_is_blocked(server):
    """`/static/../web_app.py` 必须 404 —— 曾经这里能把后端源码读出去。"""
    _, port = server
    code, body = _get(port, "/static/../web_app.py")
    assert code == 404
    assert b"def main" not in body


def test_events_streams_a_data_frame(server):
    """SSE 必须真的吐 `data: {json}` 帧，且是 text/event-stream。"""
    _, port = server
    s = socket.create_connection(("127.0.0.1", port), timeout=5)
    try:
        s.sendall(b"GET /api/events HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n")
        buf = b""
        while b"data: " not in buf and len(buf) < 65536:
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    assert b"text/event-stream" in buf, buf[:200]
    assert b"data: " in buf, buf[:200]
    line = [ln for ln in buf.split(b"\n") if ln.startswith(b"data: ")][0]
    payload = json.loads(line[len(b"data: "):].decode("utf-8"))
    assert payload["title"] == "fake"
