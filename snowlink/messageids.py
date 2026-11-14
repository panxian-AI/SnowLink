"""消息 ID 与回复定位。

SnowLuma 的 message_id 不是序号，而是**稳定哈希**：

    sha1(f"{sequence}:{sessionId}:{eventName}")[0:4] 按大端读成 int32，0 换成 1

所以它**可以是负数**（返回值文档里专门写了「成功返回的 message_id 可为负值」）。
上层框架把它当整数用、拿它回 `delete_msg` / `reply`，所以我们不能自己编一套
自增字符串——那会让 `reply` 段对不上、`get_msg` 找不到。

这里复刻同一套规则；没有权威 sequence 时（当前底层拿不到），用客户端序号兜底，
保证同一条消息在本次进程内 id 稳定、且能反查到事件。
"""

from __future__ import annotations

import hashlib
import struct
import threading
from collections import OrderedDict
from typing import Any

GROUP_EVENT = "group_message"
PRIVATE_EVENT = "private_message"
PRIVATE_SENT_EVENT = "private_message_sent"
PRIVATE_NT_EVENT = "private_message_nt"


def hash_message_id(sequence: int, session_id: int, event_name: str) -> int:
    key = f"{int(sequence)}:{int(session_id)}:{event_name}".encode("utf-8")
    value = struct.unpack(">i", hashlib.sha1(key).digest()[:4])[0]
    return value or 1


class MessageStore:
    """message_id -> 事件 / 元信息。有界，进程内有效（与 SnowLuma 的缓存语义一致）。"""

    def __init__(self, maxlen: int = 2000) -> None:
        self._events: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
        self._meta: "OrderedDict[int, dict[str, Any]]" = OrderedDict()
        self._maxlen = maxlen
        self._lock = threading.Lock()

    # ---------------------------------------------------------------- 事件
    def store_event(self, message_id: int, event: dict[str, Any]) -> None:
        if not message_id:
            return
        with self._lock:
            self._events[message_id] = event
            self._events.move_to_end(message_id)
            while len(self._events) > self._maxlen:
                self._events.popitem(last=False)

    def find_event(self, message_id: int) -> dict[str, Any] | None:
        with self._lock:
            return self._events.get(int(message_id))

    # ---------------------------------------------------------------- 元信息
    def store_meta(self, message_id: int, meta: dict[str, Any]) -> None:
        if not message_id:
            return
        with self._lock:
            self._meta[message_id] = meta
            self._meta.move_to_end(message_id)
            while len(self._meta) > self._maxlen:
                self._meta.popitem(last=False)

    def find_meta(self, message_id: int) -> dict[str, Any] | None:
        with self._lock:
            return self._meta.get(int(message_id))

    # ---------------------------------------------------------------- 回复
    def resolve_reply_sequence(self, is_group: bool, session_id: int, message_id: int) -> int | None:
        """回复段要的是被引用消息的 sequence。

        有权威 sequence 就直接用；否则按「同一会话内能对上」的规则接受
        message_id 本身（本地缓存的 id 就是哈希值，发出去时底层按序号回填）。
        """
        meta = self.find_meta(message_id)
        if meta and meta.get("is_group") == is_group and int(meta.get("targetId") or 0) == int(session_id):
            sequence = meta.get("sequence")
            if isinstance(sequence, int) and sequence > 0:
                return sequence
        if message_id > 0:
            return message_id
        return None
