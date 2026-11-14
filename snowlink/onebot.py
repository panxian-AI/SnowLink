"""动作分发（OneBot v11）。

分三类，行为刻意不同：

1. **能做的**：参数按 SnowLuma 的 schema 校验（失败 1400 + 同一句 wording），
   做完返回 `{status:"ok", retcode:0, data:...}`。
2. **动作存在、但当前通道做不到**（撤回、群管理、群文件、相册、Qzone…）：
   返回 `1404` + 一句具体原因（缺哪个能力），**不假装成功**。
3. **压根不认识的动作**：`1404` + `unknown action`，与 SnowLuma 一字不差。

第 2 类是唯一的实质差异：SnowLuma 直接操作 QQ 协议，这些它能做；我们走的是
官方客户端的 UI 通道，通道没有的能力就不该骗上层。补上通道后这里只改实现、
不改接口。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

from . import contract as C
from . import events as ev
from . import params as P
from . import segments as seg_mod
from .backend import BackendError
from .errors import (RET_ACTION_FAILED, RET_BAD_REQUEST, RET_OK, ActionError,
                     BadRequest, SegmentError)
from .messageids import (GROUP_EVENT, PRIVATE_SENT_EVENT, MessageStore,
                         hash_message_id)

#: 与 SnowLuma 的 `UNSUPPORTED` 不同的地方：这里给的是「能力缺什么」，不是动作清单
EXTRA_REASONS: dict[str, str] = {
    "get_credentials": "不持有 QQ 登录凭据（客户端在 ArkLink 那侧，本进程只有 token）",
    "get_cookies": "不持有 QQ 登录 Cookie（客户端在 ArkLink 那侧）",
    "get_csrf_token": "不持有 QQ 登录 CSRF token（客户端在 ArkLink 那侧）",
    "get_group_member_info": "拿不到群成员资料：通道只给了好友/群名册，没有成员花名册",
    "get_group_member_list": "拿不到群成员列表：通道只给了好友/群名册，没有成员花名册",
    "get_group_shut_list": "拿不到群禁言列表：通道只给了好友/群名册，没有成员花名册",
    "get_stranger_info": "拿不到陌生人资料：通道只给了好友/群名册",
    "set_restart": "本进程不负责重启（用 systemd / start.bat 守护）",
    "clean_cache": "没有需要清理的缓存",
    "get_capabilities": "",  # 这是本实现的扩展动作，见下方 handler
}


def _int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    return int(text) if text.isdigit() and len(text) <= 18 else 0


class Dispatcher:
    """动作路由。HTTP / 正向 WS / 反向 WS / WebUI 共用同一个实例。"""

    def __init__(self, backend: Any, *, self_id: str = "", message_format: str = "array",
                 app_name: str = "SnowLink", app_version: str = "", nickname: str = "",
                 on_event: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.backend = backend
        self._self_id = _int(self_id)
        self._nickname = nickname
        self.message_format = message_format or "array"
        self.app_name = app_name or "SnowLink"
        self.app_version = app_version
        self.store = MessageStore()
        self.on_event = on_event
        self._started = time.time()
        self._lock = threading.Lock()
        self._client_seq = 0
        self._sent = 0
        self.stats: dict[str, int] = {"actions": 0, "failed": 0, "unsupported": 0, "events": 0}

    # ---------------------------------------------------------------- 自身
    @property
    def self_id(self) -> int:
        if self._self_id:
            return self._self_id
        try:
            info = self.backend.self_info()
        except BackendError:
            return 0
        uin = _int(info.get("uin") or info.get("user_id") or info.get("account"))
        if uin:
            self._self_id = uin
            self._nickname = str(info.get("nickname") or info.get("name") or self._nickname)
        return self._self_id

    @property
    def nickname(self) -> str:
        if not self._nickname:
            try:
                info = self.backend.self_info()
            except BackendError:
                info = {}
            self._nickname = str(info.get("nickname") or info.get("name") or "")
        return self._nickname

    def online(self) -> bool:
        try:
            info = self.backend.status()
        except BackendError:
            return False
        return bool(info.get("online", info.get("ok", True)))

    def uptime(self) -> int:
        return int(time.time() - self._started)

    # ---------------------------------------------------------------- 入口
    def call(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        action = (action or "").strip()
        raw = dict(params or {})
        self.stats["actions"] += 1
        handler = self._handlers.get(action)
        spec = self._specs.get(action)
        try:
            if handler is None:
                if spec is not None:
                    # 动作面里有、但当前通道做不到：先按它的 schema 校验，再明确拒绝
                    P.parse_params(spec, raw, self._rules.get(action, ()))
                    raise ActionError(self._reason(action), C.RET_UNKNOWN_ACTION)
                why = C.reason_of(action)
                if why:
                    self.stats["unsupported"] += 1
                    raise ActionError(f"{action}：{why}", C.RET_UNKNOWN_ACTION)
                raise ActionError("unknown action", C.RET_UNKNOWN_ACTION)
            values = P.parse_params(spec or {}, raw, self._rules.get(action, ()))
            data = handler(values)
            return {"status": "ok", "retcode": RET_OK, "data": data}
        except BadRequest as exc:
            self.stats["failed"] += 1
            return self._fail(RET_BAD_REQUEST, exc.wording)
        except SegmentError as exc:
            self.stats["failed"] += 1
            return self._fail(RET_BAD_REQUEST, exc.message)
        except ActionError as exc:
            self.stats["failed"] += 1
            if exc.retcode == C.RET_UNKNOWN_ACTION:
                self.stats["unsupported"] += 1
            return self._fail(exc.retcode, exc.wording)
        except BackendError as exc:
            self.stats["failed"] += 1
            # 通道「做不到」透传 1404，其余按动作失败（100）——与上游的映射一致
            retcode = exc.code if exc.code in (RET_BAD_REQUEST, C.RET_UNKNOWN_ACTION) else RET_ACTION_FAILED
            return self._fail(retcode, exc.msg)

    def _fail(self, retcode: int, wording: str) -> dict[str, Any]:
        return {"status": "failed", "retcode": retcode, "data": None, "wording": wording}

    def _reason(self, action: str) -> str:
        own = EXTRA_REASONS.get(action)
        if own:
            return f"{action}：{own}"
        family = C.reason_of(action)
        return f"{action}：{family}" if family else f"{action}：当前通道做不到"

    # ---------------------------------------------------------------- 参数面
    @property
    def _specs(self) -> dict[str, dict[str, P.Field]]:
        gid = {"group_id": P.uint()}
        return {
            "send_private_msg": {
                "user_id": P.uint(),
                "message": P.message(),
                "auto_escape": P.boolean(False),
            },
            "send_group_msg": {
                "group_id": P.uint(),
                "message": P.message(),
                "auto_escape": P.boolean(False),
            },
            "send_msg": {
                "message": P.message(),
                "message_type": P.optional_text(),
                "group_id": P.uint().optional(),
                "user_id": P.uint().optional(),
                "auto_escape": P.boolean(False),
            },
            "get_msg": {"message_id": P.message_id()},
            "delete_msg": {"message_id": P.message_id()},
            "get_login_info": {},
            "get_status": {},
            "get_version_info": {},
            "get_friend_list": {},
            "get_group_list": {"no_cache": P.boolean(False)},
            "get_group_info": {**gid, "no_cache": P.boolean(False)},
            "get_group_member_info": {**gid, "user_id": P.uint(), "no_cache": P.boolean(False)},
            "get_group_member_list": {**gid, "no_cache": P.boolean(False)},
            "get_stranger_info": {"user_id": P.uint()},
            "can_send_image": {},
            "can_send_record": {},
            "set_group_ban": {**gid, "user_id": P.uint(), "duration": P.integer().optional()},
            "set_group_kick": {**gid, "user_id": P.uint(), "reject_add_request": P.boolean(False)},
            "get_group_msg_history": {
                **gid,
                "message_id": P.integer().optional(),
                "count": P.integer(minimum=0).optional(),
                "reverse_order": P.boolean(True),
            },
            "get_friend_msg_history": {
                "user_id": P.uint(),
                "message_id": P.integer().optional(),
                "count": P.integer(minimum=0).optional(),
                "reverse_order": P.boolean(True),
            },
            "get_credentials": {},
            "get_cookies": {"domain": P.optional_text()},
            "get_csrf_token": {},
            "get_capabilities": {},
        }

    @property
    def _rules(self) -> dict[str, tuple[P.Rule, ...]]:
        return {"send_msg": (P.at_least_one_of("group_id", "user_id"),)}

    # ---------------------------------------------------------------- 发送
    def _options(self, session_id: int = 0, *, group: bool = False) -> seg_mod.Options:
        def reply_seq(reply_id: int) -> int | None:
            return self.store.resolve_reply_sequence(group, session_id, reply_id)

        def mention_uid(uin: int) -> str:
            return ""

        return seg_mod.Options(resolve_reply_sequence=reply_seq, resolve_mention_uid=mention_uid)

    def _send(self, target: int, values: dict[str, Any], *, group: bool) -> dict[str, Any]:
        elements = P.parse_message_arg(
            values.get("message"),
            bool(values.get("auto_escape")),
            self._options(target, group=group),
        )
        missing = seg_mod.unsupported_elements(elements)
        if missing:
            names = "、".join(seg_mod.TYPE_NAMES.get(k, k) for k in missing)
            raise ActionError(
                f"当前通道只能发文本，这条消息里有：{names}。"
                "（ArkLink 能力矩阵里这些项是 false，补上通道后接口不用改）",
                C.RET_UNKNOWN_ACTION,
            )
        if not target:
            raise BadRequest("user_id: is required" if not group else "group_id: is required")
        if not seg_mod.text_of_elements(elements).strip():
            raise BadRequest("message must contain at least one sendable segment")

        segments = seg_mod.to_segments(elements)
        result = self.backend.send(str(target), segments, group=group)
        if not result.ok:
            raise ActionError(result.msg or "发送失败", result.code or RET_ACTION_FAILED)

        with self._lock:
            self._sent += 1
            sequence = int(getattr(result, "sequence", 0) or 0) or self._client_seq
            if not sequence:
                self._client_seq += 1
                sequence = self._client_seq
            event_name = GROUP_EVENT if group else PRIVATE_SENT_EVENT
            message_id = hash_message_id(sequence, target, event_name)
        self.store.store_meta(message_id, {
            "is_group": group,
            "targetId": target,
            "sequence": sequence,
            "timestamp": int(time.time()),
            "eventName": event_name,
        })
        self._emit_sent(group, target, message_id, elements, sequence)
        return {"message_id": message_id}

    def _emit_sent(self, group: bool, target: int, message_id: int,
                   elements: list[dict[str, Any]], sequence: int) -> None:
        if self.on_event is None:
            return
        event = ev.sent_event(
            is_group=group,
            target=target,
            message_id=message_id,
            elements=elements,
            self_id=self.self_id,
            nickname=self.nickname,
            sequence=sequence,
        )
        self.store.store_event(message_id, {**event, "real_id": message_id})
        self.stats["events"] += 1
        self.on_event(event)

    def send_private_msg(self, v: dict[str, Any]) -> dict[str, Any]:
        return self._send(int(v["user_id"]), v, group=False)

    def send_group_msg(self, v: dict[str, Any]) -> dict[str, Any]:
        return self._send(int(v["group_id"]), v, group=True)

    def send_msg(self, v: dict[str, Any]) -> dict[str, Any]:
        kind = str(v.get("message_type") or "")
        group_id = v.get("group_id")
        user_id = v.get("user_id")
        is_temp_reply = kind == "private" and user_id is not None and group_id is not None
        if not is_temp_reply and (kind == "group" or group_id is not None):
            if group_id is None:
                raise BadRequest("group_id is required")
            return self._send(int(group_id), v, group=True)
        if user_id is None:
            raise BadRequest("user_id is required")
        return self._send(int(user_id), v, group=False)

    # ---------------------------------------------------------------- 查询
    def get_login_info(self, _v: dict[str, Any]) -> dict[str, Any]:
        return {"user_id": self.self_id, "nickname": self.nickname}

    def get_status(self, _v: dict[str, Any]) -> dict[str, Any]:
        online = self.online()
        return {
            "online": online,
            "good": online,
            "time": self.uptime(),
        }

    def get_version_info(self, _v: dict[str, Any]) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "app_version": self.app_version,
            "protocol_version": "v11",
        }

    def get_friend_list(self, _v: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in self.backend.friends():
            uid = _int(item.get("uin") or item.get("user_id") or item.get("uid"))
            if not uid:
                continue
            out.append({
                "user_id": uid,
                "nickname": str(item.get("nick") or item.get("nickname") or item.get("name") or ""),
                "remark": str(item.get("remark") or item.get("name") or ""),
            })
        return out

    def get_group_list(self, _v: dict[str, Any]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for item in self.backend.groups():
            gid = _int(item.get("uin") or item.get("group_id") or item.get("uid"))
            if not gid:
                continue
            out.append(self._group_shape(gid, item))
        return out

    def get_group_info(self, v: dict[str, Any]) -> dict[str, Any]:
        gid = int(v["group_id"])
        for item in self.backend.groups():
            if _int(item.get("uin") or item.get("group_id")) == gid:
                return self._group_shape(gid, item)
        # 与 SnowLuma 一致：拿不到资料时返回结构完整的空对象，而不是把调用打挂
        return self._group_shape(gid, {})

    @staticmethod
    def _group_shape(gid: int, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "group_id": gid,
            "group_name": str(item.get("name") or item.get("group_name") or ""),
            "group_remark": str(item.get("remark") or ""),
            "member_count": _int(item.get("member_count")),
            "max_member_count": _int(item.get("max_member_count")),
            "group_create_time": _int(item.get("group_create_time")),
            "group_level": _int(item.get("group_level")),
            "group_memo": str(item.get("group_memo") or ""),
            "group_description": str(item.get("group_description") or ""),
            "group_all_shut": _int(item.get("group_all_shut")),
        }

    def get_msg(self, v: dict[str, Any]) -> dict[str, Any]:
        mid = int(v["message_id"])
        stored = self.store.find_event(mid) or self.store.find_event(abs(mid))
        if stored is None:
            raise ActionError("message not found", RET_ACTION_FAILED)
        out = dict(stored)
        out.pop("post_type", None)
        out.pop("self_id", None)
        out["real_id"] = out.get("message_id") or mid
        return out

    def delete_msg(self, v: dict[str, Any]) -> None:
        mid = int(v["message_id"])
        meta = self.store.find_meta(mid) or self.store.find_meta(abs(mid))
        if meta is None:
            raise ActionError("message not found or not retractable", RET_ACTION_FAILED)
        # 通道不支持撤回：ArkLink 会返回 1404 + 原因，这里如实透传
        self.backend.recall(mid, target=meta.get("targetId"), group=bool(meta.get("is_group")))
        return None

    def can_send_image(self, _v: dict[str, Any]) -> dict[str, Any]:
        return {"yes": False}

    def can_send_record(self, _v: dict[str, Any]) -> dict[str, Any]:
        return {"yes": False}

    def get_capabilities(self, _v: dict[str, Any]) -> dict[str, Any]:
        info = self.backend.capabilities()
        return {
            "app": "SnowLink",
            "backend": getattr(self.backend, "name", "unknown"),
            "arklink": info,
            "sendable_types": sorted(seg_mod.SENDABLE),
        }

    # 这些动作存在，但通道做不到（参数已在上层校验过，这里统一给原因）
    def _unsupported(self, action: str) -> Callable[[dict[str, Any]], Any]:
        def run(_v: dict[str, Any]) -> Any:
            raise ActionError(self._reason(action), C.RET_UNKNOWN_ACTION)

        return run

    def set_group_ban(self, v: dict[str, Any]) -> None:
        self.backend.mute(int(v["group_id"]), int(v["user_id"]),
                          int(v.get("duration") or 1800))
        return None

    def set_group_kick(self, v: dict[str, Any]) -> None:
        self.backend.kick(int(v["group_id"]), int(v["user_id"]),
                          bool(v.get("reject_add_request")))
        return None

    def get_group_msg_history(self, v: dict[str, Any]) -> dict[str, Any]:
        return self.backend.history(int(v["group_id"]), group=True,
                                    count=int(v.get("count") or 20))

    def get_friend_msg_history(self, v: dict[str, Any]) -> dict[str, Any]:
        return self.backend.history(int(v["user_id"]), group=False,
                                    count=int(v.get("count") or 20))

    def get_credentials(self, v: dict[str, Any]) -> Any:
        raise ActionError(self._reason("get_credentials"), C.RET_UNKNOWN_ACTION)

    def get_cookies(self, v: dict[str, Any]) -> Any:
        raise ActionError(self._reason("get_cookies"), C.RET_UNKNOWN_ACTION)

    def get_csrf_token(self, v: dict[str, Any]) -> Any:
        raise ActionError(self._reason("get_csrf_token"), C.RET_UNKNOWN_ACTION)

    @property
    def _handlers(self) -> dict[str, Callable[[dict[str, Any]], Any]]:
        return {
            "send_private_msg": self.send_private_msg,
            "send_group_msg": self.send_group_msg,
            "send_msg": self.send_msg,
            "get_login_info": self.get_login_info,
            "get_status": self.get_status,
            "get_version_info": self.get_version_info,
            "get_friend_list": self.get_friend_list,
            "get_group_list": self.get_group_list,
            "get_group_info": self.get_group_info,
            "get_msg": self.get_msg,
            "delete_msg": self.delete_msg,
            "can_send_image": self.can_send_image,
            "can_send_record": self.can_send_record,
            "get_capabilities": self.get_capabilities,
            "set_group_ban": self.set_group_ban,
            "set_group_kick": self.set_group_kick,
            "get_group_msg_history": self.get_group_msg_history,
            "get_friend_msg_history": self.get_friend_msg_history,
            "get_credentials": self.get_credentials,
            "get_cookies": self.get_cookies,
            "get_csrf_token": self.get_csrf_token,
            "get_group_member_info": self._unsupported("get_group_member_info"),
            "get_group_member_list": self._unsupported("get_group_member_list"),
            "get_stranger_info": self._unsupported("get_stranger_info"),
        }

    # ---------------------------------------------------------------- 事件
    def event_from_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """后端的一条消息 -> OneBot 事件（并登记 message_id，供 get_msg / reply 用）。"""
        fields, message_id, meta = ev.message_event(
            msg, self_id=self.self_id, nickname=self.nickname
        )
        self.store.store_event(message_id, {**fields, "real_id": message_id})
        self.store.store_meta(message_id, meta)
        self.stats["events"] += 1
        return fields

    def lifecycle(self, sub_type: str) -> dict[str, Any]:
        online = self.online()
        return ev.lifecycle_event(self.self_id, sub_type, online=online, good=online)

    def heartbeat(self) -> dict[str, Any]:
        online = self.online()
        return ev.heartbeat_event(self.self_id, online=online, good=online)

    def bot_status(self, sub_type: str) -> dict[str, Any]:
        return ev.bot_status_event(self.self_id, sub_type)
