"""极简 RFC6455：只做 OneBot 用得上的部分（文本帧 / ping-pong / close）。

为什么不用 `websockets` 库：这个项目的定位是「拷过来就能跑」，
不因为装不上依赖而卡住（ArkLink 也是这条原则）。代价是只支持 OneBot 需要的子集：
不做扩展、不做压缩、不做二进制分片重传；分片会累积到 fin 再交付。
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import struct
from typing import Any

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT = 0x0
OP_TEXT = 0x1
OP_BINARY = 0x2
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA

MAX_PAYLOAD = 16 * 1024 * 1024


class WSError(Exception):
    """握手或帧层出错（连接该断了）。"""


def accept_key(key: str) -> str:
    sha = hashlib.sha1((key + GUID).encode("ascii")).digest()
    return base64.b64encode(sha).decode("ascii")


def header_value(headers: dict[str, str], name: str, default: str = "") -> str:
    return headers.get(name.lower(), default)


def build_accept(headers: dict[str, str]) -> bytes:
    """根据 Upgrade 请求头生成 101 响应。校验不过就抛 WSError。"""
    if "websocket" not in header_value(headers, "upgrade").lower():
        raise WSError("不是 WebSocket 升级请求（缺 Upgrade: websocket）")
    key = header_value(headers, "sec-websocket-key")
    if not key:
        raise WSError("缺少 Sec-WebSocket-Key")
    version = header_value(headers, "sec-websocket-version", "13")
    if version != "13":
        raise WSError(f"只支持 WebSocket 13，收到 {version}")
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept_key(key)}\r\n"
        "\r\n"
    ).encode("ascii")


def read_http_head(sock: socket.socket, limit: int = 32768) -> tuple[str, dict[str, str]]:
    """读到 `\\r\\n\\r\\n` 为止，返回 (请求行, 小写键的头部字典)。"""
    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise WSError("连接在握手前关闭")
        buf += chunk
        if len(buf) > limit:
            raise WSError("握手请求头过大")
    head, _, _ = buf.partition(b"\r\n\r\n")
    lines = head.decode("iso-8859-1", "replace").split("\r\n")
    request_line = lines[0] if lines else ""
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    return request_line, headers


def client_handshake(sock: socket.socket, host: str, port: int, path: str = "/",
                     extra_headers: dict[str, str] | None = None) -> None:
    """主动连出去时的握手（反向 WS 用）。"""
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    lines = [
        f"GET {path or '/'} HTTP/1.1",
        f"Host: {host}:{port}",
        "Upgrade: websocket",
        "Connection: Upgrade",
        f"Sec-WebSocket-Key: {key}",
        "Sec-WebSocket-Version: 13",
    ]
    for k, v in (extra_headers or {}).items():
        lines.append(f"{k}: {v}")
    sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))

    buf = b""
    while b"\r\n\r\n" not in buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise WSError("对端在握手阶段断开了连接")
        buf += chunk
        if len(buf) > 32768:
            raise WSError("握手响应过大")

    head, _, rest = buf.partition(b"\r\n\r\n")
    text = head.decode("iso-8859-1", "replace")
    first, _, tail = text.partition("\r\n")
    parts = first.split()
    if len(parts) < 2 or parts[1] != "101":
        raise WSError(f"对端没有接受 WebSocket 升级：{first}")
    headers: dict[str, str] = {}
    for line in tail.split("\r\n"):
        if ":" in line:
            k, v = line.split(":", 1)
            headers[k.strip().lower()] = v.strip()
    if header_value(headers, "sec-websocket-accept") != accept_key(key):
        raise WSError("Sec-WebSocket-Accept 校验失败")
    if rest:
        # 对端在握手响应后立刻发了帧：退回给连接层
        sock._snowlink_pending = rest  # type: ignore[attr-defined]


class WSConnection:
    """一条 WebSocket 连接。`mask=True` 表示我们是客户端（客户端必须掩码）。"""

    def __init__(self, sock: socket.socket, *, mask: bool) -> None:
        self.sock = sock
        self.mask = mask
        self.closed = False
        self._pending = getattr(sock, "_snowlink_pending", b"")

    # ---------------------------------------------------------------- 发送
    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self.closed:
            raise WSError("连接已关闭")
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        mask_bit = 0x80 if self.mask else 0
        if length < 126:
            header.append(mask_bit | length)
        elif length < 65536:
            header.append(mask_bit | 126)
            header += struct.pack("!H", length)
        else:
            header.append(mask_bit | 127)
            header += struct.pack("!Q", length)
        if self.mask:
            key = os.urandom(4)
            header += key
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + payload)

    def send_text(self, text: str) -> None:
        self._send_frame(OP_TEXT, text.encode("utf-8"))

    def ping(self, data: bytes = b"") -> None:
        self._send_frame(OP_PING, data)

    def close(self, code: int = 1000, reason: str = "") -> None:
        if self.closed:
            return
        payload = struct.pack("!H", code) + reason.encode("utf-8")[:120]
        try:
            self._send_frame(OP_CLOSE, payload)
        except OSError:
            pass
        self.closed = True
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass

    # ---------------------------------------------------------------- 接收
    def _recv_exact(self, n: int) -> bytes:
        out = b""
        if self._pending:
            take = min(n, len(self._pending))
            out, self._pending = self._pending[:take], self._pending[take:]
            n -= take
        while n > 0:
            chunk = self.sock.recv(n)
            if not chunk:
                raise WSError("连接已关闭")
            out += chunk
            n -= len(chunk)
        return out

    def recv_message(self) -> tuple[int, bytes]:
        """返回 (opcode, 负载)。控制帧会在这里就地处理（ping 自动回 pong）。"""
        fragments: list[bytes] = []
        frag_opcode: int | None = None
        while True:
            b1, b2 = self._recv_exact(2)
            fin = bool(b1 & 0x80)
            opcode = b1 & 0x0F
            masked = bool(b2 & 0x80)
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._recv_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._recv_exact(8))
            if length > MAX_PAYLOAD:
                raise WSError("帧过大")
            key = self._recv_exact(4) if masked else b""
            payload = self._recv_exact(length) if length else b""
            if masked:
                payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))

            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_PONG:
                continue
            if opcode == OP_CLOSE:
                self.closed = True
                return OP_CLOSE, payload
            if opcode == OP_CONT:
                if frag_opcode is None:
                    continue
                fragments.append(payload)
                if fin:
                    return frag_opcode, b"".join(fragments)
                continue
            if not fin:
                frag_opcode = opcode
                fragments = [payload]
                continue
            return opcode, payload

    def recv_text(self) -> str:
        while True:
            opcode, payload = self.recv_message()
            if opcode == OP_CLOSE:
                raise WSError("对端关闭了连接")
            if opcode == OP_TEXT:
                return payload.decode("utf-8", "replace")
            # 二进制帧对 OneBot 没意义，跳过
