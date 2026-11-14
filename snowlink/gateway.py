"""出口层：HTTP 服务端 / 正向 WS / 反向 WS / HTTP 上报 / WebUI。

一个进程里同时把这些跑起来，配置与 SnowLuma 的 `networks` 一一对应：

    httpServers -> POST /（动作）      wsServers -> 框架连进来的 WS
    wsClients   -> 我们连出去的 WS     httpClients -> 事件 POST 到指定地址

所有入口共用同一个 `Dispatcher`，所以从哪个入口发消息，行为完全一致。
"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import onebot as ob
from . import segments as seg_mod
from . import wsproto
from .config import BackendConfig, HttpClient, HttpServer, OneBotConfig, WsClient, WsServer
from .backend import ArkLinkBackend, DemoBackend


# ---------------------------------------------------------------- 事件中心
class EventHub:
    """事件分发 + 最近 N 条留档（WebUI 的实时日志、get_msg 的兜底）。"""

    def __init__(self, maxlen: int = 500) -> None:
        self._sinks: list[Any] = []
        self._lock = threading.Lock()
        self._recent: list[dict[str, Any]] = []
        self._maxlen = maxlen
        self.count = 0

    def add(self, sink: Any) -> None:
        with self._lock:
            self._sinks.append(sink)

    def remove(self, sink: Any) -> None:
        with self._lock:
            if sink in self._sinks:
                self._sinks.remove(sink)

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return self._recent[-limit:]

    def publish(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._recent.append(event)
            if len(self._recent) > self._maxlen:
                del self._recent[: len(self._recent) - self._maxlen]
            sinks = list(self._sinks)
            self.count += 1
        for sink in sinks:
            try:
                sink.deliver(event)
            except Exception:  # 单个接收方坏了不影响别人
                self.remove(sink)


class _WsSink:
    """一条已连上的 WebSocket，用于推事件。"""

    def __init__(self, conn: wsproto.WSConnection, *, name: str, role: str,
                 report_self: bool, message_format: str) -> None:
        self.conn = conn
        self.name = name
        self.role = "Event" if role.lower() == "event" else "Universal"
        self.report_self = report_self
        self.message_format = message_format
        self._lock = threading.Lock()
        self.closed = False

    def deliver(self, event: dict[str, Any]) -> None:
        if self.closed:
            raise wsproto.WSError("closed")
        if event.get("post_type") == "message_sent" and not self.report_self:
            return
        payload = dict(event)
        if self.message_format == "string" and isinstance(payload.get("message"), list):
            payload["message"] = seg_mod.render_cq(payload["message"])
        with self._lock:
            self.conn.send_text(json.dumps(payload, ensure_ascii=False))

    def close(self) -> None:
        self.closed = True
        self.conn.close()


# ---------------------------------------------------------------- HTTP 动作
def _extract_action(handler: BaseHTTPRequestHandler, body: bytes) -> tuple[str, dict[str, Any], str]:
    """兼容三种常见写法：JSON body / 表单 / GET 查询串。"""
    query = parse_qs(urlparse(handler.path).query)
    echo = str((query.get("echo") or [""])[0])
    if body:
        try:
            data = json.loads(body.decode("utf-8"))
        except ValueError:
            data = None
        if isinstance(data, dict):
            echo = str(data.get("echo") or echo)
            action = str(data.get("action") or "")
            params = data.get("params")
            if params is None:
                params = {k: v for k, v in data.items() if k not in ("action", "echo")}
            if not isinstance(params, dict):
                params = {}
            if action:
                return action, params, echo
    action = str((query.get("action") or [""])[0])
    params = {k: (v[0] if isinstance(v, list) and v else v) for k, v in query.items()
              if k not in ("action", "echo", "access_token")}
    return action, params, echo


def _get_header(headers: Any, name: str, default: str = "") -> str:
    """兼容两种头部容器：`email.message`（大小写不敏感）和我们的 dict（键已小写）。"""
    try:
        value = headers.get(name)
    except Exception:
        value = None
    if value is None:
        try:
            value = headers.get(name.lower())
        except Exception:
            value = None
    return str(value) if value is not None else default


def _auth_ok(headers: Any, query: dict[str, list[str]], token: str) -> bool:
    if not token:
        return True
    auth = _get_header(headers, "Authorization")
    if auth.lower().startswith("bearer ") and auth[7:].strip() == token:
        return True
    if (query.get("access_token") or [""])[0] == token:
        return True
    # 有些框架把 token 放在 Sec-WebSocket-Protocol 里（OneBot v11 允许）
    proto = _get_header(headers, "Sec-WebSocket-Protocol")
    return bool(proto) and proto.strip() == token


class _HttpMixin:
    """HTTP 服务端与 WebUI 共用的 JSON 小工具。"""

    server_version = "SnowLink/0.1"

    def _json(self, status: int, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:  # 静音：日志走我们自己的
        return


def make_http_handler(dispatcher: ob.Dispatcher, hub: EventHub, cfg: HttpServer,
                      log: Callable[[str], None]) -> type[BaseHTTPRequestHandler]:
    class Handler(_HttpMixin, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _authorized(self) -> bool:
            query = parse_qs(urlparse(self.path).query)
            if _auth_ok(self.headers, query, cfg.accessToken):
                return True
            self._json(HTTPStatus.UNAUTHORIZED, {"status": "failed", "retcode": 1401,
                                                 "data": None, "wording": "accessToken 不对"})
            return False

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            if "websocket" in str(self.headers.get("Upgrade") or "").lower() and cfg.enableWebSocket:
                self._upgrade()
                return
            action, params, echo = _extract_action(self, b"")
            if not action:
                self._json(HTTPStatus.OK, {"status": "ok", "retcode": 0,
                                           "data": {"app": "SnowLink"}, "wording": ""})
                return
            result = dispatcher.call(action, params)
            result["echo"] = echo
            self._json(HTTPStatus.OK, result)

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            action, params, echo = _extract_action(self, body)
            if not action:
                self._json(HTTPStatus.BAD_REQUEST, {"status": "failed", "retcode": 1400,
                                                    "data": None, "wording": "缺少 action"})
                return
            result = dispatcher.call(action, params)
            result["echo"] = echo
            self._json(HTTPStatus.OK, result)

        # ------------------------------------------------------------ WS 升级
        def _upgrade(self) -> None:
            try:
                response = wsproto.build_accept({k.lower(): v for k, v in self.headers.items()})
            except wsproto.WSError as exc:
                self._json(HTTPStatus.BAD_REQUEST, {"status": "failed", "retcode": 1400,
                                                    "data": None, "wording": str(exc)})
                return
            self.wfile.write(response)
            self.wfile.flush()
            self.close_connection = True
            sink = _WsSink(wsproto.WSConnection(self.connection, mask=False),
                           name=cfg.name, role="Universal",
                           report_self=cfg.reportSelfMessage, message_format=cfg.messageFormat)
            serve_ws_loop(sink, dispatcher, hub, log, label=f"http:{cfg.name}")

    return Handler


# ---------------------------------------------------------------- WS 循环
def serve_ws_loop(sink: _WsSink, dispatcher: ob.Dispatcher, hub: EventHub,
                  log: Callable[[str], None], *, label: str) -> None:
    hub.add(sink)
    log(f"[{label}] 框架已连接")
    try:
        sink.deliver(dispatcher.lifecycle("connect"))
        while True:
            text = sink.conn.recv_text()
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            action = str(data.get("action") or "")
            params = data.get("params") or {}
            echo = data.get("echo")
            result = dispatcher.call(action, params if isinstance(params, dict) else {})
            if echo is not None:
                result["echo"] = echo
            sink.conn.send_text(json.dumps(result, ensure_ascii=False))
    except (wsproto.WSError, OSError):
        pass
    finally:
        hub.remove(sink)
        sink.close()
        log(f"[{label}] 框架断开")


# ---------------------------------------------------------------- 正向 WS 服务端
class _WsServerThread(threading.Thread):
    def __init__(self, cfg: WsServer, dispatcher: ob.Dispatcher, hub: EventHub,
                 log: Callable[[str], None], stop: threading.Event) -> None:
        super().__init__(daemon=True, name=f"ws-server-{cfg.port}")
        self.cfg = cfg
        self.dispatcher = dispatcher
        self.hub = hub
        self.log = log
        self.stop_event = stop
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((cfg.host, cfg.port))
        self.sock.listen(8)
        self.sock.settimeout(1.0)

    def run(self) -> None:
        self.log(f"[ws:{self.cfg.name}] 监听 ws://{self.cfg.host}:{self.cfg.port}{self.cfg.path}")
        while not self.stop_event.is_set():
            try:
                conn, _addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve_conn, args=(conn,), daemon=True).start()
        try:
            self.sock.close()
        except OSError:
            pass

    def _serve_conn(self, conn: socket.socket) -> None:
        conn.settimeout(30)
        try:
            line, headers = wsproto.read_http_head(conn)
            parts = line.split()
            path = parts[1] if len(parts) > 1 else "/"
            query = parse_qs(urlparse(path).query)
            if not _auth_ok(headers, query, self.cfg.accessToken):
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
                conn.close()
                return
            if self.cfg.path not in ("", "/") and not path.startswith(self.cfg.path):
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                conn.close()
                return
            conn.sendall(wsproto.build_accept(headers))
            conn.settimeout(None)
            sink = _WsSink(wsproto.WSConnection(conn, mask=False), name=self.cfg.name,
                           role=self.cfg.role, report_self=self.cfg.reportSelfMessage,
                           message_format=self.cfg.messageFormat)
            serve_ws_loop(sink, self.dispatcher, self.hub, self.log, label=f"ws:{self.cfg.name}")
        except (wsproto.WSError, OSError, KeyError):
            try:
                conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------- 反向 WS 客户端
class _ReverseWsThread(threading.Thread):
    def __init__(self, cfg: WsClient, dispatcher: ob.Dispatcher, hub: EventHub,
                 log: Callable[[str], None], stop: threading.Event) -> None:
        super().__init__(daemon=True, name=f"ws-client-{cfg.name}")
        self.cfg = cfg
        self.dispatcher = dispatcher
        self.hub = hub
        self.log = log
        self.stop_event = stop

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._once()
            except (wsproto.WSError, OSError) as exc:
                self.log(f"[ws-reverse:{self.cfg.name}] 断开：{exc}")
            self.stop_event.wait(max(1, self.cfg.reconnectInterval))

    def _once(self) -> None:
        parts = urlparse(self.cfg.url)
        if parts.scheme not in ("ws", "http") or not parts.hostname:
            raise wsproto.WSError(f"wsClients[{self.cfg.name}].url 不是 ws:// 地址")
        host = parts.hostname
        port = parts.port or 80
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        conn = socket.create_connection((host, port), timeout=10)
        self.log(f"[ws-reverse:{self.cfg.name}] 连接 {self.cfg.url}")
        extra: dict[str, str] = {}
        if self.cfg.accessToken:
            extra["Authorization"] = f"Bearer {self.cfg.accessToken}"
        wsproto.client_handshake(conn, host, port, path, extra)
        conn.settimeout(None)
        sink = _WsSink(wsproto.WSConnection(conn, mask=True), name=self.cfg.name,
                       role="Universal", report_self=self.cfg.reportSelfMessage,
                       message_format=self.cfg.messageFormat)
        serve_ws_loop(sink, self.dispatcher, self.hub, self.log, label=f"ws-reverse:{self.cfg.name}")


# ---------------------------------------------------------------- HTTP 上报
class _HttpReportThread(threading.Thread):
    def __init__(self, cfg: HttpClient, hub: EventHub, log: Callable[[str], None],
                 stop: threading.Event) -> None:
        super().__init__(daemon=True, name=f"http-report-{cfg.name}")
        self.cfg = cfg
        self.hub = hub
        self.log = log
        self.stop_event = stop
        self.queue: "queue.Queue[dict[str, Any]]" = queue.Queue(maxsize=1000)
        self._last_error = 0.0

    def deliver(self, event: dict[str, Any]) -> None:
        if event.get("post_type") == "message_sent" and not self.cfg.reportSelfMessage:
            return
        try:
            self.queue.put_nowait(event)
        except queue.Full:
            pass

    def run(self) -> None:
        import http.client

        self.hub.add(self)
        parts = urlparse(self.cfg.url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            self.log(f"[http-report:{self.cfg.name}] url 不是 http(s) 地址，跳过")
            return
        while not self.stop_event.is_set():
            try:
                event = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            body = json.dumps(event, ensure_ascii=False).encode("utf-8")
            headers = {"Content-Type": "application/json; charset=utf-8"}
            if self.cfg.accessToken:
                headers["Authorization"] = f"Bearer {self.cfg.accessToken}"
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = cls(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                       timeout=self.cfg.timeout)
            try:
                conn.request("POST", parts.path or "/", body=body, headers=headers)
                conn.getresponse().read()
            except (OSError, http.client.HTTPException) as exc:
                now = time.time()
                if now - self._last_error > 60:
                    self._last_error = now
                    self.log(f"[http-report:{self.cfg.name}] 上报失败：{exc}")
            finally:
                conn.close()


# ---------------------------------------------------------------- 总装
class Gateway:
    """按配置把所有入口拉起来，并把后端事件转成 OneBot 事件广播出去。"""

    def __init__(self, cfg: OneBotConfig, backend_cfg: BackendConfig, *,
                 uin: str = "", log: Callable[[str], None] | None = None) -> None:
        self.cfg = cfg
        self.backend_cfg = backend_cfg
        self.uin = uin or backend_cfg.uin
        self._log = log or (lambda _m: None)
        self.stop_event = threading.Event()
        self.hub = EventHub()
        if backend_cfg.offline:
            self.backend: Any = DemoBackend()
        else:
            self.backend = ArkLinkBackend(backend_cfg.baseUrl, backend_cfg.token,
                                          timeout=backend_cfg.timeout)
        fmt = "array"
        if cfg.wsServers:
            fmt = cfg.wsServers[0].messageFormat
        elif cfg.httpServers:
            fmt = cfg.httpServers[0].messageFormat
        self.dispatcher = ob.Dispatcher(self.backend, self_id=self.uin, message_format=fmt)
        self._threads: list[threading.Thread] = []
        self._http_servers: list[ThreadingHTTPServer] = []
        self._started_at = 0.0
        self.log_lines: list[str] = []

    # ---------------------------------------------------------------- 日志
    def log(self, message: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} {message}"
        self.log_lines.append(line)
        if len(self.log_lines) > 500:
            del self.log_lines[: len(self.log_lines) - 500]
        self._log(line)

    # ---------------------------------------------------------------- 启停
    def start(self) -> None:
        self._started_at = time.time()
        for cfg in self.cfg.httpServers:
            self._start_http(cfg)
        for cfg in self.cfg.wsServers:
            try:
                thread = _WsServerThread(cfg, self.dispatcher, self.hub, self.log, self.stop_event)
            except OSError as exc:
                self.log(f"[ws:{cfg.name}] 端口 {cfg.port} 起不来：{exc}")
                continue
            thread.start()
            self._threads.append(thread)
        for cfg in self.cfg.wsClients:
            thread = _ReverseWsThread(cfg, self.dispatcher, self.hub, self.log, self.stop_event)
            thread.start()
            self._threads.append(thread)
        for cfg in self.cfg.httpClients:
            thread = _HttpReportThread(cfg, self.hub, self.log, self.stop_event)
            thread.start()
            self._threads.append(thread)
        threading.Thread(target=self._consume_backend, daemon=True, name="backend-consumer").start()
        threading.Thread(target=self._heartbeat, daemon=True, name="heartbeat").start()

    def _start_http(self, cfg: HttpServer) -> None:
        try:
            server = ThreadingHTTPServer((cfg.host, cfg.port),
                                         make_http_handler(self.dispatcher, self.hub, cfg, self.log))
            server.daemon_threads = True
            self._http_servers.append(server)
            threading.Thread(target=server.serve_forever, daemon=True,
                             name=f"http-{cfg.port}").start()
            self.log(f"[http:{cfg.name}] 监听 http://{cfg.host}:{cfg.port}{cfg.path}")
        except OSError as exc:
            self.log(f"[http:{cfg.name}] 端口 {cfg.port} 起不来：{exc}")

    def stop(self) -> None:
        self.stop_event.set()
        for server in self._http_servers:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
        self._http_servers.clear()

    # ---------------------------------------------------------------- 后端事件
    def _consume_backend(self) -> None:
        if isinstance(self.backend, DemoBackend):
            return
        self.log(f"[backend] 订阅 {self.backend_cfg.baseUrl}/api/v1/stream")
        while not self.stop_event.is_set():
            try:
                self.backend.stream(self._on_backend_message, self.stop_event)
            except Exception as exc:  # 兜底：线程不能死
                self.log(f"[backend] 订阅中断：{exc}")
            self.stop_event.wait(3)

    def _on_backend_message(self, payload: dict[str, Any]) -> None:
        event = self.dispatcher.event_from_message(payload)
        if event:
            self.hub.publish(event)

    def _heartbeat(self) -> None:
        interval = 15
        while not self.stop_event.wait(interval):
            self.hub.publish(self.dispatcher.heartbeat(interval))

    # ---------------------------------------------------------------- 发送（给 WebUI 用）
    def send_text(self, target: str, text: str, *, group: bool = False) -> dict[str, Any]:
        segments = [{"type": "text", "data": {"text": text}}]
        result = self.backend.send(target, segments, group=group)
        if isinstance(self.backend, DemoBackend) and result.ok:
            # 演示后端：把发出去的消息回显成一条收到的事件，链路可视
            self.hub.publish(self.dispatcher.event_sent(target, group, result.message_id, segments))
            self.hub.publish({
                "time": int(time.time()), "self_id": int(self.uin or 0), "post_type": "message",
                "message_type": "group" if group else "private",
                "message_id": result.message_id,
                "message": segments, "raw_message": text, "font": 0,
                "sender": {"user_id": 10001, "nickname": "示例好友", "card": ""},
                **({"group_id": int(target or 0)} if group else {"user_id": int(target or 0)}),
            })
        return {"ok": result.ok, "code": result.code, "msg": result.msg, "id": result.message_id}

    def stats(self) -> dict[str, Any]:
        return {
            "uin": self.uin,
            "uptime": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "backend": getattr(self.backend, "name", "unknown"),
            "offline": bool(self.backend_cfg.offline),
            "self_id": self.dispatcher.self_id,
            "nickname": self.dispatcher.nickname(),
            "events": self.hub.count,
            "actions": self.dispatcher.stats,
            "networks": {
                "http": [f"{c.host}:{c.port}" for c in self.cfg.httpServers],
                "ws": [f"{c.host}:{c.port}" for c in self.cfg.wsServers],
                "ws_reverse": [c.url for c in self.cfg.wsClients],
                "http_report": [c.url for c in self.cfg.httpClients],
            },
        }
