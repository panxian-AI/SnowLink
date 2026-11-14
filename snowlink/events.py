"""事件构造：把「后端消息」变成 OneBot v11 事件。

字段集合是照着 SnowLuma 的 converter 抄的（不是拍脑袋按 OneBot 文档补）：

* 群消息带 `group_name`（超出 OneBot 规范，但它确实发），`anonymous` 恒为 null，
  `sender` 里 `role` / `sex` / `age` 都补齐；
* 自己发的消息是 `post_type: "message_sent"`，`user_id` 是**自己**，私聊额外带 `target_id`；
* `meta_event.lifecycle`（connect / enable / disable）与 `heartbeat`（30 秒）分开；
* 上下线另有 `notice.bot_status`。

`message_format` 为 `string` 时，`message` 换成 `raw_message`（CQ 串）——这个转换
放在出口层做（每个连接可以有自己的格式），事件本身始终是段数组。
"""

from __future__ import annotations

import time
from typing import Any, Iterable

from . import segments as seg_mod
from .messageids import (GROUP_EVENT, PRIVATE_EVENT, PRIVATE_SENT_EVENT,
                         hash_message_id)

HEARTBEAT_INTERVAL_MS = 30000


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    return int(text) if text.isdigit() and len(text) <= 18 else 0


def _now() -> int:
    return int(time.time())


def message_event(
    msg: dict[str, Any],
    *,
    self_id: int,
    nickname: str = "",
) -> tuple[dict[str, Any], int, dict[str, Any]]:
    """后端消息 -> (`message` 事件, `message_id`, 元信息)。

    元信息给 `reply` 段与 `get_msg` 用：`{is_group, targetId, sequence, timestamp, eventName}`。
    """
    is_group = int(msg.get("chat_type") or 1) == 2
    peer = _int(msg.get("peer_uid"))
    sender_uid = _int(msg.get("sender_uid"))
    if not sender_uid:
        sender_uid = peer if not is_group else 0
    # 私聊的会话对象是对方；群聊是群号
    target = peer if peer else sender_uid
    sequence = _int(msg.get("seq")) or _int(msg.get("message_seq"))
    timestamp = int(float(msg.get("ts") or time.time()))

    text = str(msg.get("text") or "")
    elements = [{"type": "text", "text": text}] if text else []
    segments = seg_mod.to_segments(elements)
    raw = seg_mod.segments_to_raw(segments)

    self_sent = bool(sender_uid) and sender_uid == self_id
    event_name = (
        GROUP_EVENT
        if is_group
        else (PRIVATE_SENT_EVENT if self_sent else PRIVATE_EVENT)
    )
    message_id = hash_message_id(sequence or 0, target, event_name)

    sender_name = str(msg.get("sender_name") or "")
    sender = {
        "user_id": sender_uid,
        "nickname": sender_name,
        "sex": "unknown",
        "age": 0,
    }
    fields: dict[str, Any] = {
        "time": timestamp,
        "self_id": self_id,
        "post_type": "message_sent" if self_sent else "message",
        "message_type": "group" if is_group else "private",
        "sub_type": "normal" if is_group else "friend",
        "message_id": message_id,
        "message_seq": sequence,
        "message": segments,
        "raw_message": raw,
        "font": 0,
    }
    if is_group:
        fields["group_id"] = target
        fields["group_name"] = str(msg.get("peer_name") or "")
        fields["user_id"] = sender_uid
        fields["sender"] = {**sender, "card": "", "role": "member"}
        fields["anonymous"] = None
    else:
        fields["user_id"] = sender_uid
        fields["sender"] = {**sender, "card": ""}
        if self_sent and target and target != self_id:
            fields["target_id"] = target

    meta = {
        "is_group": is_group,
        "targetId": target,
        "sequence": sequence,
        "timestamp": timestamp,
        "eventName": event_name,
    }
    return fields, message_id, meta


def sent_event(
    *,
    is_group: bool,
    target: int,
    message_id: int,
    elements: Iterable[dict[str, Any]],
    self_id: int,
    nickname: str = "",
    timestamp: int | None = None,
    sequence: int = 0,
) -> dict[str, Any]:
    """自己发出去的消息 -> `message_sent` 事件。"""
    segments = seg_mod.to_segments(elements)
    event: dict[str, Any] = {
        "time": timestamp or _now(),
        "self_id": self_id,
        "post_type": "message_sent",
        "message_type": "group" if is_group else "private",
        "sub_type": "normal" if is_group else "friend",
        "message_id": message_id,
        "message_seq": sequence,
        "user_id": self_id,
        "message": segments,
        "raw_message": seg_mod.segments_to_raw(segments),
        "font": 0,
        "sender": {
            "user_id": self_id,
            "nickname": nickname,
            "sex": "unknown",
            "age": 0,
        },
    }
    if is_group:
        event["group_id"] = target
    else:
        event["target_id"] = target
    return event


def lifecycle_event(self_id: int, sub_type: str, *, online: bool = True,
                    good: bool = True, timestamp: int | None = None) -> dict[str, Any]:
    return {
        "time": timestamp or _now(),
        "self_id": self_id,
        "post_type": "meta_event",
        "meta_event_type": "lifecycle",
        "sub_type": sub_type,
        "status": {"online": online, "good": online and good},
    }


def heartbeat_event(self_id: int, *, online: bool = True, good: bool = True,
                    timestamp: int | None = None) -> dict[str, Any]:
    return {
        "time": timestamp or _now(),
        "self_id": self_id,
        "post_type": "meta_event",
        "meta_event_type": "heartbeat",
        "status": {"online": online, "good": online and good},
        "interval": HEARTBEAT_INTERVAL_MS,
    }


def bot_status_event(self_id: int, sub_type: str, *, timestamp: int | None = None) -> dict[str, Any]:
    return {
        "time": timestamp or _now(),
        "self_id": self_id,
        "post_type": "notice",
        "notice_type": "bot_status",
        "sub_type": sub_type,
        "user_id": self_id,
    }


def shape(event: dict[str, Any], message_format: str) -> dict[str, Any]:
    """按连接的 `messageFormat` 整形：`string` 时 message 换成 CQ 串。"""
    if message_format != "string":
        return event
    if event.get("post_type") not in ("message", "message_sent"):
        return event
    if not isinstance(event.get("message"), list):
        return event
    return {**event, "message": str(event.get("raw_message") or "")}
