"""出口层集成：HTTP 动作 / 正向 WS / 反向 WS / HTTP 上报。

这些用例把服务真起在 127.0.0.1 的临时端口上（port=0），
所以它们验证的是「框架连上来能不能用」，而不是内部函数。
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from snowlink import wsproto
from snowlink.config import BackendConfig, HttpClient, HttpServer, OneBotConfig, WsClient, WsServer
from snowlink.gateway import Gateway


def wait_until(fn, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if fn():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture()
def gateway():
    cfg = OneBotConfig(
        httpServers=[HttpServer(accessToken="http-token", port=0)],
        wsServers=[WsServer(accessToken="ws-token", port=0)],
    )
    gw = Gateway(cfg, BackendConfig(offline=True), uin="10000", log=lambda _m: None)
    gw.start()
    yield gw
    gw.stop()


def http_port(gw: Gateway) -> int:
    return gw._http_servers[0].server_address[1]


def ws_port(gw: Gateway) -> int:
    for thread in gw._threads:
        sock = getattr(thread, "sock", None)
        if isinstance(sock, socket.socket) and sock.getsockname()[1]:
            return int(sock.getsockname()[1])
    raise AssertionError("没有 WS 服务端线程")


def call_http(port: int, body: dict, token: str = "http-token") -> tuple[int, dict]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    conn.request("POST", "/", json.dumps(body).encode("utf-8"), headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    return resp.status, json.loads(raw or b"{}")


# ---------------------------------------------------------------- HTTP
def test_http_action_ok(gateway) -> None:
    status, data = call_http(http_port(gateway), {"action": "get_version_info", "echo": "e1"})
    assert status == 200
    assert data["retcode"] == 0
    assert data["data"]["app_name"] == "SnowLink"
    assert data["echo"] == "e1"


def test_http_rejects_bad_token(gateway) -> None:
    status, data = call_http(http_port(gateway), {"action": "get_login_info"}, token="wrong")
    assert status == 401
    assert data["retcode"] == 1401


def test_http_send_goes_to_backend(gateway) -> None:
    status, data = call_http(http_port(gateway), {
        "action": "send_private_msg",
        "params": {"user_id": 10001, "message": "hello"},
    })
    assert status == 200 and data["retcode"] == 0
    assert gateway.backend.sent[0]["text"] == "hello"


def test_http_unsupported_action_returns_1404(gateway) -> None:
    _status, data = call_http(http_port(gateway), {"action": "get_group_member_list",
                                                   "params": {"group_id": 1}})
    assert data["retcode"] == 1404 and data["wording"]


def test_http_get_action(gateway) -> None:
    conn = http.client.HTTPConnection("127.0.0.1", http_port(gateway), timeout=5)
    conn.request("GET", "/?action=get_login_info&access_token=http-token")
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    assert data["retcode"] == 0


# ---------------------------------------------------------------- 正向 WS
def test_ws_server_roundtrip(gateway) -> None:
    port = ws_port(gateway)
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    wsproto.client_handshake(sock, "127.0.0.1", port, "/", {"Authorization": "Bearer ws-token"})
    conn = wsproto.WSConnection(sock, mask=True)

    lifecycle = json.loads(conn.recv_text())
    assert lifecycle["post_type"] == "meta_event"
    assert lifecycle["meta_event_type"] == "lifecycle"

    conn.send_text(json.dumps({"action": "get_version_info", "echo": 7}))
    result = json.loads(conn.recv_text())
    assert result["retcode"] == 0
    assert result["echo"] == 7

    # 事件推送：发布一条消息，应立刻收到
    gateway.hub.publish({
        "time": 1, "self_id": 10000, "post_type": "message", "message_type": "private",
        "message_id": "x1", "message": [], "raw_message": "push", "font": 0,
        "sender": {"user_id": 1, "nickname": "n", "card": ""}, "user_id": 1,
    })
    pushed = json.loads(conn.recv_text())
    assert pushed["raw_message"] == "push"

    conn.close()


def test_ws_server_rejects_bad_token(gateway) -> None:
    port = ws_port(gateway)
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(
        b"GET / HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    head = sock.recv(200).decode("latin-1")
    sock.close()
    assert "401" in head


# ---------------------------------------------------------------- 反向 WS
class _FakeWsServer(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.received: list[dict] = []
        self.ready = threading.Event()

    def run(self) -> None:
        conn, _addr = self.sock.accept()
        conn.settimeout(5)
        _line, headers = wsproto.read_http_head(conn)
        conn.sendall(wsproto.build_accept(headers))
        ws = wsproto.WSConnection(conn, mask=False)
        self.ready.set()
        try:
            while True:
                self.received.append(json.loads(ws.recv_text()))
        except (wsproto.WSError, OSError, ValueError):
            pass
        finally:
            ws.close()


def test_reverse_ws_connects_and_receives_lifecycle() -> None:
    server = _FakeWsServer()
    server.start()
    cfg = OneBotConfig(
        httpServers=[],
        wsServers=[],
        wsClients=[WsClient(name="rev", url=f"ws://127.0.0.1:{server.port}/")],
    )
    gw = Gateway(cfg, BackendConfig(offline=True), uin="10000", log=lambda _m: None)
    gw.start()
    try:
        assert server.ready.wait(5)
        assert wait_until(lambda: any(x.get("post_type") == "meta_event" for x in server.received))
    finally:
        gw.stop()


# ---------------------------------------------------------------- HTTP 上报
class _Capture:
    def __init__(self) -> None:
        self.items: list[dict] = []

        capture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                try:
                    capture.items.append(json.loads(body))
                except ValueError:
                    pass
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_http_report_posts_events() -> None:
    capture = _Capture()
    cfg = OneBotConfig(
        httpServers=[],
        wsServers=[],
        httpClients=[HttpClient(name="report", url=f"http://127.0.0.1:{capture.port}/push")],
    )
    gw = Gateway(cfg, BackendConfig(offline=True), uin="10000", log=lambda _m: None)
    gw.start()
    try:
        assert wait_until(lambda: len(gw.hub._sinks) >= 1)
        gw.hub.publish({"post_type": "message", "message_type": "private", "raw_message": "上报"})
        assert wait_until(lambda: capture.items), "没有收到上报"
        assert capture.items[0]["raw_message"] == "上报"
    finally:
        gw.stop()
        capture.close()


def test_echo_only_when_requested(gateway) -> None:
    """请求没带 echo，响应里就不该有 echo 键（与 SnowLuma 一致）。"""
    port = http_port(gateway)
    _status, data = call_http(port, {"action": "get_login_info"})
    assert "echo" not in data
    _status, data = call_http(port, {"action": "get_login_info", "echo": "abc"})
    assert data["echo"] == "abc"
