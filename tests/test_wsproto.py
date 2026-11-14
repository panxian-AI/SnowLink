"""RFC6455 子集：握手、掩码、分片、关闭。"""

from __future__ import annotations

import socket
import struct
import threading

from snowlink import wsproto


def test_accept_key_rfc_vector() -> None:
    # RFC 6455 第 1.3 节的示例
    assert wsproto.accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_build_accept_rejects_plain_http() -> None:
    try:
        wsproto.build_accept({"upgrade": "h2c", "sec-websocket-key": "x"})
    except wsproto.WSError as exc:
        assert "Upgrade" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应该抛 WSError")


def test_client_to_server_masked_frame() -> None:
    a, b = socket.socketpair()
    server = wsproto.WSConnection(a, mask=False)
    client = wsproto.WSConnection(b, mask=True)
    client.send_text("你好 OneBot")
    assert server.recv_text() == "你好 OneBot"
    server.send_text('{"retcode":0}')
    assert client.recv_text() == '{"retcode":0}'
    client.close()
    server.close()


def _frame(opcode: int, payload: bytes, *, fin: bool, mask: bool = True) -> bytes:
    head = bytearray()
    head.append((0x80 if fin else 0) | opcode)
    length = len(payload)
    mask_bit = 0x80 if mask else 0
    if length < 126:
        head.append(mask_bit | length)
    else:
        head.append(mask_bit | 126)
        head += struct.pack("!H", length)
    if not mask:
        return bytes(head) + payload
    key = b"\x01\x02\x03\x04"
    head += key
    return bytes(head) + bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def test_fragments_are_joined() -> None:
    a, b = socket.socketpair()
    server = wsproto.WSConnection(a, mask=False)
    b.sendall(_frame(wsproto.OP_TEXT, b"hel", fin=False))
    b.sendall(_frame(wsproto.OP_CONT, b"lo", fin=True))
    assert server.recv_text() == "hello"
    server.close()
    b.close()


def test_close_frame_returns_close() -> None:
    a, b = socket.socketpair()
    server = wsproto.WSConnection(a, mask=False)
    b.sendall(_frame(wsproto.OP_CLOSE, struct.pack("!H", 1000), fin=True))
    opcode, payload = server.recv_message()
    assert opcode == wsproto.OP_CLOSE
    assert struct.unpack("!H", payload[:2])[0] == 1000
    server.close()
    b.close()


def test_handshake_end_to_end() -> None:
    a, b = socket.socketpair()

    def server_side() -> None:
        _line, headers = wsproto.read_http_head(a)
        a.sendall(wsproto.build_accept(headers))

    thread = threading.Thread(target=server_side)
    thread.start()
    wsproto.client_handshake(b, "127.0.0.1", 3001, "/?access_token=x")
    thread.join(timeout=3)

    client = wsproto.WSConnection(b, mask=True)
    server = wsproto.WSConnection(a, mask=False)
    client.send_text("ping")
    assert server.recv_text() == "ping"
    client.close()
    server.close()


def test_client_handshake_rejects_non_101() -> None:
    a, b = socket.socketpair()

    def server_side() -> None:
        wsproto.read_http_head(a)
        a.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")

    thread = threading.Thread(target=server_side)
    thread.start()
    try:
        wsproto.client_handshake(b, "127.0.0.1", 3001, "/")
    except wsproto.WSError as exc:
        assert "101" in str(exc) or "接受" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("应该拒绝非 101 响应")
    finally:
        thread.join(timeout=3)
        a.close()
        b.close()
