"""底层后端：把 OneBot 动作翻译成 ArkLink 的调用。

两条实现：

* `ArkLinkBackend`：**正式路径**。发送走 `POST /api/v1/send`，事件走 `GET /api/v1/stream`（SSE）。
  QQ 由 ArkLink 那边的官方客户端通道操作，本进程不注入、不碰协议。
* `DemoBackend`：离线路径。没有 QQ / 没有 ArkLink 时也能把整条链路跑通（把发出去的消息
  回显成一条收到的事件），用来验证框架侧配置对不对。
"""

from __future__ import annotations

import http.client
import json
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urlparse

from . import segments as seg_mod


@dataclass
class SendResult:
    ok: bool
    code: int = 0
    msg: str = ""
    message_id: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


class BackendError(Exception):
    def __init__(self, msg: str, code: int = 1500) -> None:
        super().__init__(msg)
        self.msg = msg
        self.code = code


# ---------------------------------------------------------------- ArkLink
class ArkLinkBackend:
    """ArkLink HTTP API 的薄客户端（只用标准库）。"""

    name = "arklink"

    def __init__(self, base_url: str, token: str = "", *, timeout: float = 20.0) -> None:
        parts = urlparse(base_url or "http://127.0.0.1:6900")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise BackendError(f"arklink.json 里的 baseUrl 不像个地址：{base_url!r}", 1400)
        self.scheme = parts.scheme
        self.host = parts.hostname
        self.port = parts.port or (443 if parts.scheme == "https" else 80)
        self.prefix = parts.path.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- HTTP
    def _conn(self, timeout: float | None = None) -> http.client.HTTPConnection:
        cls = (
            http.client.HTTPSConnection
            if self.scheme == "https"
            else http.client.HTTPConnection
        )
        return cls(self.host, self.port, timeout=timeout or self.timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None,
                *, timeout: float | None = None) -> dict[str, Any]:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        conn = self._conn(timeout)
        try:
            conn.request(method, f"{self.prefix}{path}", body=body, headers=self._headers())
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw) if raw.strip() else {}
            except ValueError:
                raise BackendError(f"ArkLink 返回的不是 JSON（HTTP {resp.status}）：{raw[:200]}")
            if resp.status == 401:
                raise BackendError("ArkLink 拒绝了请求（401）：token 不对。跑 `arklink env token` 或看 ~/.arklink/api.json", 1401)
            return data if isinstance(data, dict) else {"data": data}
        except (OSError, socket.timeout, http.client.HTTPException) as exc:
            raise BackendError(
                f"连不上 ArkLink（{self.scheme}://{self.host}:{self.port}）：{exc}。"
                "先起服务：arklink serve api",
                1500,
            ) from exc
        finally:
            conn.close()

    # ---------------------------------------------------------------- 能力
    def health(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/health", timeout=3)

    def status(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/status")

    def capabilities(self) -> dict[str, Any]:
        return self.request("GET", "/api/v1/capabilities")

    def self_info(self) -> dict[str, Any]:
        data = self.request("GET", "/api/v1/self")
        return data if isinstance(data, dict) else {}

    def friends(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/api/v1/friends")
        for key in ("friends", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return []

    def groups(self) -> list[dict[str, Any]]:
        data = self.request("GET", "/api/v1/groups")
        for key in ("groups", "items", "data"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return []

    # ---------------------------------------------------------------- 发送
    def send(self, target: str, segments: list[dict[str, Any]], *,
             group: bool = False, dry_run: bool = False) -> SendResult:
        """发一条消息。结构化段优先；通道不支持时如实返回失败，不降级。"""
        missing = seg_mod.unsupported(segments)
        if missing:
            names = "、".join(seg_mod.TYPE_NAMES.get(k, k) for k in missing)
            return SendResult(
                False,
                1404,
                f"当前通道只能发文本，这条消息里有：{names}。"
                "（ArkLink 的能力矩阵里这些项是 false；补上通道后接口不用改）",
            )

        text = seg_mod.text_of(segments)
        payload: dict[str, Any] = {"to": target, "text": text}
        if group:
            payload["group_id"] = target
        if dry_run:
            payload["dry_run"] = True
        data = self.request("POST", "/api/v1/send", payload)
        ok = bool(data.get("ok"))
        items = data.get("items") or []
        first = items[0] if items and isinstance(items[0], dict) else {}
        if not ok:
            return SendResult(
                False,
                int(data.get("code") or first.get("code") or 1400),
                str(data.get("msg") or first.get("msg") or "发送失败"),
                raw=data,
            )
        return SendResult(
            True,
            0,
            "",
            message_id=str(first.get("message_id") or first.get("id") or ""),
            raw=data,
        )

    # ---------------------------------------------------------------- 收消息
    def stream(self, on_event: Callable[[dict[str, Any]], None],
               stop: threading.Event, *, since: int = 0) -> None:
        """订阅 SSE：断线自动重连，用 seq 续传，不丢不重（靠 OneBot 侧去重兜底）。"""
        cursor = since
        while not stop.is_set():
            conn = self._conn(timeout=None)
            try:
                headers = self._headers()
                headers["Accept"] = "text/event-stream"
                conn.request("GET", f"{self.prefix}/api/v1/stream?since={cursor}", headers=headers)
                resp = conn.getresponse()
                if resp.status != 200:
                    raise BackendError(f"SSE 返回 HTTP {resp.status}")
                event = "message"
                while not stop.is_set():
                    line = resp.readline()
                    if not line:
                        break
                    text = line.decode("utf-8", "replace").strip()
                    if text.startswith("event:"):
                        event = text[6:].strip()
                    elif text.startswith("data:"):
                        try:
                            payload = json.loads(text[5:].strip())
                        except ValueError:
                            continue
                        if event == "hello":
                            continue
                        if isinstance(payload, dict):
                            seq = int(payload.get("seq") or 0)
                            if seq:
                                cursor = max(cursor, seq)
                            on_event(payload)
            except (OSError, BackendError, http.client.HTTPException):
                pass
            finally:
                conn.close()
            stop.wait(3.0)


# ---------------------------------------------------------------- 演示后端
class DemoBackend:
    """离线后端：记录发送、把每条发送变成一条「收到」的事件，用于打通链路。"""

    name = "demo"
    offline = True

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self._n = 0

    def health(self) -> dict[str, Any]:
        return {"ok": True, "offline": True}

    def status(self) -> dict[str, Any]:
        return {"ok": True, "online": True, "offline": True, "engine": "demo"}

    def capabilities(self) -> dict[str, Any]:
        return {"ok": True, "channel": "demo", "items": {"send.text": True, "image": False}}

    def self_info(self) -> dict[str, Any]:
        return {"ok": True, "uin": "10000", "nickname": "SnowLink Demo"}

    def friends(self) -> list[dict[str, Any]]:
        return [{"uin": "10001", "name": "示例好友"}]

    def groups(self) -> list[dict[str, Any]]:
        return [{"uin": "20001", "name": "示例群"}]

    def send(self, target: str, segments: list[dict[str, Any]], *,
             group: bool = False, dry_run: bool = False) -> SendResult:
        missing = seg_mod.unsupported(segments)
        if missing:
            names = "、".join(seg_mod.TYPE_NAMES.get(k, k) for k in missing)
            return SendResult(False, 1404, f"演示通道只能发文本，这条消息里有：{names}")
        self._n += 1
        item = {
            "to": target,
            "group": group,
            "text": seg_mod.text_of(segments),
            "message_id": f"demo-{self._n}",
            "ts": time.time(),
        }
        self.sent.append(item)
        return SendResult(True, 0, "", message_id=item["message_id"], raw={"ok": True})
