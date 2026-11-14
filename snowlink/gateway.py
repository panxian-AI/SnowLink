"""出口层：HTTP 服务端 / 正向 WS / 反向 WS / HTTP 上报。

行为按 SnowLuma 的适配器逐条对齐（读的是它发行包里的 network 层）：

HTTP
  * 路径即动作：`/{base}/{action}`，`{base}` 之外一律 404 + retcode 1404；
  * 鉴权只认 `Authorization: Bearer <token>` 与 `?access_token=`，失败 401 + 1401；
  * GET 无动作 → `{status:"ok", retcode:0, data:{online:true}}`；
  * GET 查询串每个值先按 JSON 解析（`?group_id=123` 得到数字），POST 支持
    `application/x-www-form-urlencoded` 与 JSON，JSON 坏了回 `bad request: invalid json`；
  * `echo` 只在请求带的时候回填；`Content-Length` 超 64MB 回 413 + 1413。

WS
  * 路径决定角色：`/api` → Api、`/event` → Event、其余 Universal；
  * 连上就给 Event/Universal 推 `lifecycle(connect)`、`lifecycle(enable)`、`heartbeat`；
  * 30 秒一次 ping，连续 3 个周期没有任何动静就掐掉半开连接；
  * Event-only 连接发动作会被忽略（不回包，与它一致）；
  * 关闭时给 Event/Universal 推 `lifecycle(disable)`。

上报
  * `User-Agent: OneBot`、`X-Self-ID`，有 token 时用 token 作密钥算 `X-Signature: sha1=...`；
  * 响应是 JSON 时当作 quick operation 执行（reply / delete / ban）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import queue
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from . import events as ev
from . import onebot as ob
from . import segments as seg_mod
from . import wsproto
from .backend import ArkLinkBackend, BackendError, DemoBackend
from .config import (BackendConfig, HttpClient, HttpServer, OneBotConfig,
                     WsClient, WsServer)

MAX_BODY = 64 * 1024 * 1024
HEARTBEAT_INTERVAL = 30.0
HEARTBEAT_DEAD_AFTER = HEARTBEAT_INTERVAL * 3


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

    def broadcast_frame(self, frame: str, roles: tuple[str, ...] = ("Event", "Universal")) -> None:
        with self._lock:
            sinks = list(self._sinks)
        for sink in sinks:
            if getattr(sink, "role", "") in roles:
                try:
                    sink.send_raw(frame)
                except Exception:
                    self.remove(sink)


# ---------------------------------------------------------------- WS 出口
class _WsSink:
    """一条已连上的 WebSocket，用于推事件 / 发响应。"""

    def __init__(self, conn: wsproto.WSConnection, *, name: str, role: str,
                 report_self: bool, message_format: str) -> None:
        self.conn = conn
        self.name = name
        self.role = role
        self.report_self = report_self
        self.message_format = message_format
        self._lock = threading.Lock()
        self.closed = False

    def deliver(self, event: dict[str, Any]) -> None:
        if self.closed:
            raise wsproto.WSError("closed")
        if self.role not in ("Event", "Universal"):
            return
        if event.get("post_type") == "message_sent" and not self.report_self:
            return
        payload = ev.shape(event, self.message_format)
        self.send_raw(json.dumps(payload, ensure_ascii=False))

    def send_raw(self, frame: str) -> None:
        with self._lock:
            if self.closed:
                raise wsproto.WSError("closed")
            self.conn.send_text(frame)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.conn.close(code, reason)
        except (wsproto.WSError, OSError):
            pass


def _heartbeat_thread(sink: _WsSink, stop: threading.Event) -> None:
    """30 秒一次 ping 保活；对端死掉由 socket 超时（90s）发现。"""
    while not stop.wait(HEARTBEAT_INTERVAL):
        if sink.closed:
            return
        try:
            sink.conn.ping()
        except (wsproto.WSError, OSError):
            sink.close(1001, "ping failed")
            return


def serve_ws_loop(sink: _WsSink, dispatcher: ob.Dispatcher, hub: EventHub,
                  log: Callable[[str], None], *, label: str,
                  bootstrap: bool = True) -> None:
    """一条 WS 连接的生命周期：握手之后的事件推送 + 动作处理。"""
    stop = threading.Event()
    hub.add(sink)
    threading.Thread(target=_heartbeat_thread, args=(sink, stop), daemon=True).start()
    log(f"[{label}] 框架已连接（role={sink.role}）")
    try:
        if bootstrap and sink.role in ("Event", "Universal"):
            sink.deliver(dispatcher.lifecycle("connect"))
            sink.deliver(dispatcher.lifecycle("enable"))
            sink.deliver(dispatcher.heartbeat())
        while not sink.closed:
            text = sink.conn.recv_text()
            if text is None:
                break
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if not isinstance(data, dict):
                continue
            action = str(data.get("action") or "")
            params = data.get("params")
            if not isinstance(params, dict):
                params = {k: v for k, v in data.items() if k not in ("action", "echo")}
            echo = data.get("echo")
            if sink.role not in ("Api", "Universal"):
                # Event-only 连接不该发动作；与 SnowLuma 一致：忽略，不回包
                log(f"[{label}] 忽略来自 Event-only 连接的动作 {action}")
                continue
            if not action:
                continue
            result = dispatcher.call(action, params)
            if echo is not None:
                result["echo"] = echo
            sink.send_raw(json.dumps(result, ensure_ascii=False))
    except (wsproto.WSError, OSError):
        pass
    finally:
        stop.set()
        hub.remove(sink)
        if sink.role in ("Event", "Universal"):
            try:
                sink.deliver(dispatcher.lifecycle("disable"))
            except Exception:
                pass
        sink.close()
        log(f"[{label}] 框架断开")


# ---------------------------------------------------------------- HTTP 动作
def _auth_ok(headers: Any, query: dict[str, list[str]], token: str) -> bool:
    if not token:
        return True
    auth = str(_header(headers, "Authorization"))
    if auth == f"Bearer {token}":
        return True
    return (query.get("access_token") or [""])[0] == token


def _header(headers: Any, name: str, default: str = "") -> str:
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


def _normalize_path(value: str) -> str:
    path = (value or "/").strip() or "/"
    if path == "/":
        return "/"
    return path[:-1] if path.endswith("/") else path


def _json_value(raw: str) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw


class _HttpMixin:
    server_version = "SnowLink"

    def _json(self, status: int, data: Any) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def make_http_handler(dispatcher: ob.Dispatcher, hub: EventHub, cfg: HttpServer,
                      log: Callable[[str], None]) -> type[BaseHTTPRequestHandler]:
    base = _normalize_path(cfg.path)

    class Handler(_HttpMixin, BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        # ------------------------------------------------------------ 工具
        def _failure(self, status: int, retcode: int, wording: str) -> None:
            self._json(status, {"status": "failed", "retcode": retcode,
                                "data": None, "wording": wording})

        def _resolve(self) -> str | None:
            """路径 -> 动作名；路径不在本服务上则回 404 并返回 None。"""
            incoming = urlparse(self.path).path
            if incoming in (base, base + "/"):
                return ""
            prefix = base if base.endswith("/") else base + "/"
            if incoming.startswith(prefix):
                return incoming[len(prefix):]
            self._failure(HTTPStatus.NOT_FOUND, 1404, "not found")
            return None

        def _check_auth(self, query: dict[str, list[str]]) -> bool:
            if _auth_ok(self.headers, query, cfg.accessToken):
                return True
            self._failure(HTTPStatus.UNAUTHORIZED, 1401, "unauthorized")
            return False

        # ------------------------------------------------------------ 方法
        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(HTTPStatus.NO_CONTENT)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            action = self._resolve()
            if action is None:
                return
            if not self._check_auth(query):
                return
            if "websocket" in _header(self.headers, "Upgrade").lower() and cfg.enableWebSocket:
                self._upgrade(query)
                return
            if not action:
                self._json(HTTPStatus.OK, {"status": "ok", "retcode": 0,
                                           "data": {"online": True}})
                return
            params: dict[str, Any] = {}
            for key, values in query.items():
                if key in ("access_token",):
                    continue
                params[key] = _json_value(values[-1]) if values else ""
            result = dispatcher.call(action, params)
            self._json(HTTPStatus.OK, result)

        def do_POST(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            action = self._resolve()
            if action is None:
                return
            if not self._check_auth(query):
                return

            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                self._failure(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 1413,
                              "request body too large")
                return
            body = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            params: dict[str, Any] = {}
            echo: Any = None
            content_type = str(self.headers.get("Content-Type") or "")

            if body.strip():
                if "application/x-www-form-urlencoded" in content_type:
                    params, echo, action = self._parse_form(body, action)
                elif "application/json" in content_type or not content_type:
                    try:
                        parsed_body = json.loads(body)
                    except ValueError:
                        if content_type:
                            self._failure(HTTPStatus.BAD_REQUEST, 1400,
                                          "bad request: invalid json")
                            return
                        params, echo, action = self._parse_form(body, action)
                        parsed_body = None
                    if parsed_body is not None:
                        if not isinstance(parsed_body, dict) or isinstance(parsed_body, list):
                            self._failure(HTTPStatus.BAD_REQUEST, 1400,
                                          "bad request: invalid json")
                            return
                        if parsed_body.get("action") and not action:
                            action = str(parsed_body["action"])
                        nested = parsed_body.get("params")
                        if isinstance(nested, dict):
                            params = dict(nested)
                        else:
                            params = {k: v for k, v in parsed_body.items()
                                      if k not in ("action", "echo")}
                        echo = parsed_body.get("echo")
                else:
                    self._failure(HTTPStatus.BAD_REQUEST, 1400,
                                  f"bad request: unsupported content-type: {content_type}")
                    return

            if not action:
                self._failure(HTTPStatus.BAD_REQUEST, 1400, "bad request: missing action")
                return
            result = dispatcher.call(action, params)
            if echo is not None:
                result["echo"] = echo
            self._json(HTTPStatus.OK, result)

        def do_PUT(self) -> None:  # noqa: N802
            self._failure(HTTPStatus.METHOD_NOT_ALLOWED, 1400, "method not allowed")

        do_DELETE = do_PUT  # type: ignore[assignment]
        do_PATCH = do_PUT  # type: ignore[assignment]

        def _parse_form(self, body: str, action: str) -> tuple[dict[str, Any], Any, str]:
            params: dict[str, Any] = {}
            for key, values in parse_qs(body, keep_blank_values=True).items():
                params[key] = _json_value(values[-1]) if values else ""
            echo = params.pop("echo", None)
            if not action and params.get("action"):
                action = str(params.pop("action"))
            else:
                params.pop("action", None)
            return params, echo, action

        # ------------------------------------------------------------ WS 升级
        def _upgrade(self, query: dict[str, list[str]]) -> None:
            path = urlparse(self.path).path
            role = _resolve_role(path, cfg)
            if role is None:
                self._failure(HTTPStatus.NOT_FOUND, 1404, "not found")
                return
            try:
                response = wsproto.build_accept({k.lower(): v for k, v in self.headers.items()})
            except wsproto.WSError as exc:
                self._failure(HTTPStatus.BAD_REQUEST, 1400, str(exc))
                return
            self.wfile.write(response)
            self.wfile.flush()
            self.close_connection = True
            sink = _WsSink(wsproto.WSConnection(self.connection, mask=False),
                           name=cfg.name, role=role,
                           report_self=cfg.reportSelfMessage,
                           message_format=cfg.messageFormat)
            serve_ws_loop(sink, dispatcher, hub, log, label=f"http:{cfg.name}")

    return Handler


def _resolve_role(path: str, cfg: Any) -> str | None:
    """WS 角色：与 SnowLuma 的 `resolveForwardRole` 同规则。"""
    base = _normalize_path(getattr(cfg, "path", "/"))
    incoming = _normalize_path(path)
    role = getattr(cfg, "role", None)
    if role is None:
        if incoming.endswith("/api"):
            return "Api"
        if incoming.endswith("/event"):
            return "Event"
        return "Universal"
    if incoming == base:
        return role
    if role != "Universal" or base != "/":
        return None
    if incoming == "/api":
        return "Api"
    if incoming == "/event":
        return "Event"
    return None


# ---------------------------------------------------------------- 正向 WS
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
        self.sock.listen(16)
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
            parsed = urlparse(path)
            query = parse_qs(parsed.query, keep_blank_values=True)
            role = _resolve_role(parsed.path, self.cfg)
            if role is None:
                conn.sendall(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                conn.close()
                return
            if not _auth_ok(headers, query, self.cfg.accessToken):
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
                conn.close()
                return
            conn.sendall(wsproto.build_accept(headers))
            conn.settimeout(HEARTBEAT_DEAD_AFTER)
            sink = _WsSink(wsproto.WSConnection(conn, mask=False), name=self.cfg.name,
                           role=role, report_self=self.cfg.reportSelfMessage,
                           message_format=self.cfg.messageFormat)
            serve_ws_loop(sink, self.dispatcher, self.hub, self.log, label=f"ws:{self.cfg.name}")
        except (wsproto.WSError, OSError, KeyError):
            try:
                conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------- 反向 WS
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
            self.stop_event.wait(max(1.0, float(self.cfg.reconnectInterval or 5)))

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
        extra: dict[str, str] = {"X-Self-ID": str(self.dispatcher.self_id),
                                 "X-Client-Role": "Universal"}
        if self.cfg.accessToken:
            extra["Authorization"] = f"Bearer {self.cfg.accessToken}"
        wsproto.client_handshake(conn, host, port, path, extra)
        conn.settimeout(HEARTBEAT_DEAD_AFTER)
        sink = _WsSink(wsproto.WSConnection(conn, mask=True), name=self.cfg.name,
                       role="Universal", report_self=self.cfg.reportSelfMessage,
                       message_format=self.cfg.messageFormat)
        serve_ws_loop(sink, self.dispatcher, self.hub, self.log,
                      label=f"ws-reverse:{self.cfg.name}")


# ---------------------------------------------------------------- HTTP 上报
class _HttpReportThread(threading.Thread):
    def __init__(self, cfg: HttpClient, hub: EventHub, log: Callable[[str], None],
                 stop: threading.Event, self_id: Callable[[], int]) -> None:
        super().__init__(daemon=True, name=f"http-report-{cfg.name}")
        self.cfg = cfg
        self.hub = hub
        self.log = log
        self.stop_event = stop
        self.self_id = self_id
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
            payload = ev.shape(event, self.cfg.messageFormat)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                "User-Agent": "OneBot",
                "X-Self-ID": str(self.self_id()),
            }
            if self.cfg.accessToken:
                digest = hmac.new(self.cfg.accessToken.encode("utf-8"), body, hashlib.sha1)
                headers["X-Signature"] = "sha1=" + digest.hexdigest()
            cls = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
            conn = cls(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
                       timeout=self.cfg.timeout)
            try:
                conn.request("POST", parts.path or "/", body=body, headers=headers)
                response = conn.getresponse()
                raw = response.read()
                if response.status >= 400:
                    raise OSError(f"HTTP {response.status}")
                if "application/json" in str(response.getheader("Content-Type") or ""):
                    self._quick_operation(event, raw)
            except (OSError, http.client.HTTPException) as exc:
                now = time.time()
                if now - self._last_error > 60:
                    self._last_error = now
                    self.log(f"[http-report:{self.cfg.name}] 上报失败：{exc}")
            finally:
                conn.close()

    def _quick_operation(self, event: dict[str, Any], raw: bytes) -> None:
        """响应体是 JSON 就当作快速操作执行（reply / delete / ban）。"""
        try:
            operation = json.loads(raw.decode("utf-8"))
        except ValueError:
            return
        if not isinstance(operation, dict) or event.get("post_type") != "message":
            return
        try:
            reply = operation.get("reply")
            if reply:
                auto_escape = bool(operation.get("auto_escape"))
                params: dict[str, Any] = {"message": reply, "auto_escape": auto_escape}
                if event.get("message_type") == "group":
                    params["group_id"] = event.get("group_id")
                    if operation.get("at_sender") is not False and event.get("user_id"):
                        at = {"type": "at", "data": {"qq": str(event["user_id"])}}
                        if isinstance(reply, str):
                            params["message"] = [at, {"type": "text", "data": {"text": reply}}]
                            params["auto_escape"] = False
                        elif isinstance(reply, list):
                            params["message"] = [at, *reply]
                    self.dispatcher_action("send_group_msg", params)
                else:
                    params["user_id"] = event.get("user_id")
                    self.dispatcher_action("send_private_msg", params)
            if operation.get("delete"):
                self.dispatcher_action("delete_msg", {"message_id": event.get("message_id")})
            if operation.get("ban") and event.get("message_type") == "group":
                duration = operation.get("ban_duration")
                self.dispatcher_action("set_group_ban", {
                    "group_id": event.get("group_id"),
                    "user_id": event.get("user_id"),
                    "duration": duration if isinstance(duration, int) else 1800,
                })
        except Exception as exc:  # 快速操作失败不影响上报主链路
            self.log(f"[http-report:{self.cfg.name}] quick operation 失败：{exc}")

    def dispatcher_action(self, action: str, params: dict[str, Any]) -> None:
        dispatcher = getattr(self, "dispatcher", None)
        if dispatcher is not None:
            dispatcher.call(action, params)


# ---------------------------------------------------------------- 总装
class Gateway:
    """按配置把所有入口拉起来，并把后端事件转成 OneBot 事件广播出去。"""

    def __init__(self, cfg: OneBotConfig, backend_cfg: BackendConfig, *,
                 uin: str = "", log: Callable[[str], None] | None = None,
                 app_name: str = "SnowLink", app_version: str = "") -> None:
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
        self.dispatcher = ob.Dispatcher(self.backend, self_id=self.uin, message_format=fmt,
                                        app_name=app_name, app_version=app_version,
                                        on_event=self._publish)
        self._threads: list[threading.Thread] = []
        self._http_servers: list[ThreadingHTTPServer] = []
        self._started_at = 0.0
        self.log_lines: list[str] = []

    def _publish(self, event: dict[str, Any]) -> None:
        self.hub.publish(event)

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
            reporter = _HttpReportThread(cfg, self.hub, self.log, self.stop_event,
                                         lambda: self.dispatcher.self_id)
            reporter.dispatcher = self.dispatcher  # type: ignore[attr-defined]
            reporter.start()
            self._threads.append(reporter)
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
            extra = " 与 WebSocket" if cfg.enableWebSocket else ""
            self.log(f"[http:{cfg.name}] 监听 http://{cfg.host}:{cfg.port}{cfg.path}{extra}")
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
        interval = ev.HEARTBEAT_INTERVAL_MS / 1000.0
        while not self.stop_event.wait(interval):
            self.hub.publish(self.dispatcher.heartbeat())

    # ---------------------------------------------------------------- 发送（给 WebUI 用）
    def send_text(self, target: str, text: str, *, group: bool = False) -> dict[str, Any]:
        segments = [{"type": "text", "data": {"text": text}}]
        result = self.backend.send(target, segments, group=group)
        return {"ok": result.ok, "code": result.code, "msg": result.msg, "id": result.message_id}

    def stats(self) -> dict[str, Any]:
        return {
            "uin": self.uin,
            "uptime": round(time.time() - self._started_at, 1) if self._started_at else 0,
            "backend": getattr(self.backend, "name", "unknown"),
            "offline": bool(self.backend_cfg.offline),
            "self_id": self.dispatcher.self_id,
            "nickname": self.dispatcher.nickname,
            "events": self.hub.count,
            "actions": self.dispatcher.stats,
            "networks": {
                "http": [f"{c.host}:{c.port}" for c in self.cfg.httpServers],
                "ws": [f"{c.host}:{c.port}" for c in self.cfg.wsServers],
                "ws_reverse": [c.url for c in self.cfg.wsClients],
                "http_report": [c.url for c in self.cfg.httpClients],
            },
        }
