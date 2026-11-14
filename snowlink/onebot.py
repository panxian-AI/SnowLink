"""OneBot v11：动作分发 + 事件构造。

动作集合按「底层真能做到」来划：能做的返回 `retcode: 0`，
做不到的返回 `retcode: 1404` + 一句人话（哪一项不行、为什么），
**不返回假的成功**——这是 SnowLuma 换过来之后最容易被上层发现差异的地方，
所以宁可明确失败。

事件只发 OneBot v11 标准字段，另外把 ArkLink 的原始字段塞进 `snowlink` 里，方便排查。
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from typing import Any

from . import __version__
from . import contract as C
from . import segments as seg_mod
from .backend import BackendError

#: retcode 取值与 SnowLuma 的枚举一一对应（0 / 100 / 1200 / 1400 / 1404）
RET_OK = C.RET_OK
RET_ACTION_FAILED = C.RET_ACTION_FAILED
RET_INTERNAL = C.RET_INTERNAL
RET_BAD_PARAM = C.RET_BAD_REQUEST
RET_UNSUPPORTED = C.RET_UNKNOWN_ACTION

#: 个别动作的原因比「能力族」那句更具体，这里覆盖
UNSUPPORTED: dict[str, str] = {
    "delete_msg": "ArkLink 的通道还不能撤回消息（能力矩阵里 recall 为 false）",
    "get_group_member_info": "花名册接口还没接真实 QQ，暂时拿不到群成员资料",
    "get_group_member_list": "花名册接口还没接真实 QQ，暂时拿不到群成员列表",
    "get_stranger_info": "没有陌生人资料接口",
    "set_group_ban": "群管理（禁言）通道还没落地",
    "set_group_kick": "群管理（踢人）通道还没落地",
    "set_group_card": "群名片修改通道还没落地",
    "set_group_name": "群名修改通道还没落地",
    "set_friend_add_request": "好友请求处理还没落地",
    "set_group_add_request": "群请求处理还没落地",
    "send_like": "点赞接口没实现",
    "get_cookies": "不持有 QQ 登录 Cookie（QQ 由 ArkLink 侧的客户端持有）",
    "get_csrf_token": "不持有 QQ 登录凭据（QQ 由 ArkLink 侧的客户端持有）",
    "get_credentials": "不持有 QQ 登录凭据（QQ 由 ArkLink 侧的客户端持有）",
    "get_image": "不接管图片文件",
    "get_record": "不接管语音文件",
    "get_group_file_system_info": "群文件接口还没落地",
    "upload_group_file": "群文件接口还没落地",
    "delete_msg_unidirectional": "ArkLink 的通道还不能撤回消息",
    "mark_msg_as_read": "没有已读上报通道",
    "set_restart": "本进程不负责重启（用 systemd / start.bat 守护）",
    "clean_cache": "没有缓存需要清理",
}


def _as_int(value: Any) -> int:
    """OneBot 的 id 是整数；ArkLink 里是字符串，能转就转，转不了给 0。"""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if text.isdigit() and len(text) <= 18:
        return int(text)
    return 0


def _now() -> int:
    return int(time.time())


class Dispatcher:
    """动作路由。同一个实例给所有入口（HTTP / WS / 反向 WS）共用。"""

    def __init__(self, backend: Any, *, self_id: str = "", message_format: str = "array",
                 app_name: str = "SnowLink") -> None:
        self.backend = backend
        self._self_id = self_id
        self.message_format = message_format or "array"
        self.app_name = app_name or "SnowLink"
        self._started = time.time()
        self._self_cache: dict[str, Any] = {}
        self._cache: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
        self._lock = threading.Lock()
        self._sent = 0
        self.stats: dict[str, int] = {"actions": 0, "failed": 0, "unsupported": 0, "events": 0}

    # ---------------------------------------------------------------- 自身信息
    @property
    def self_id(self) -> str:
        if self._self_id:
            return self._self_id
        try:
            info = self.backend.self_info()
        except BackendError:
            return ""
        uin = str(info.get("uin") or info.get("user_id") or info.get("account") or "")
        if uin:
            self._self_id = uin
            self._self_cache = info
        return self._self_id

    def nickname(self) -> str:
        if not self._self_cache:
            try:
                self._self_cache = self.backend.self_info()
            except BackendError:
                self._self_cache = {}
        return str(self._self_cache.get("nickname") or self._self_cache.get("name") or "")

    # ---------------------------------------------------------------- 动作
    def call(self, action: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = dict(params or {})
        action = (action or "").strip()
        self.stats["actions"] += 1
        try:
            handler = self._handlers().get(action)
            if handler is None:
                self.stats["unsupported"] += 1
                why = UNSUPPORTED.get(action) or C.reason_of(action)
                if why:
                    # 动作存在、本实现做不到：给出具体原因，比「unknown action」有用
                    return self._fail(RET_UNSUPPORTED, f"{action}：{why}")
                # 完全不在 OneBot 动作面里，wording 与 SnowLuma 一致
                return self._fail(RET_UNSUPPORTED, "unknown action")
            data = handler(params)
            if isinstance(data, dict) and data.get("__fail__"):
                self.stats["failed"] += 1
                return self._fail(int(data.get("code") or RET_BAD_PARAM), str(data.get("msg") or ""))
            return {"status": "ok", "retcode": RET_OK, "data": data}
        except ValueError as exc:
            self.stats["failed"] += 1
            return self._fail(RET_BAD_PARAM, str(exc))
        except BackendError as exc:
            self.stats["failed"] += 1
            return self._fail(exc.code or RET_INTERNAL, exc.msg)

    def _fail(self, retcode: int, wording: str) -> dict[str, Any]:
        return {"status": "failed", "retcode": retcode, "data": None, "wording": wording}

    def _handlers(self) -> dict[str, Any]:
        return {
            "send_private_msg": self._send_private,
            "send_group_msg": self._send_group,
            "send_msg": self._send_msg,
            "get_login_info": self._get_login_info,
            "get_status": self._get_status,
            "get_version_info": self._get_version_info,
            "get_friend_list": self._get_friend_list,
            "get_group_list": self._get_group_list,
            "get_group_info": self._get_group_info,
            "get_msg": self._get_msg,
            "can_send_image": self._can_send_image,
            "can_send_record": self._can_send_record,
            "get_capabilities": self._get_capabilities,
        }

    # ---------------------------------------------------------------- 发送
    def _message_of(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        raw = params.get("message")
        if raw is None and "message" not in params:
            raise ValueError("缺少 message（OneBot 段数组或 CQ 字符串）")
        if params.get("auto_escape"):
            return [{"type": "text", "data": {"text": str(raw or "")}}]
        try:
            return seg_mod.normalize(raw)
        except ValueError as exc:
            raise ValueError(str(exc)) from exc

    def _do_send(self, target: str, segments: list[dict[str, Any]], *, group: bool) -> dict[str, Any]:
        if not target:
            raise ValueError("缺少 user_id / group_id")
        result = self.backend.send(target, segments, group=group)
        if not result.ok:
            return {"__fail__": True, "code": result.code or RET_INTERNAL, "msg": result.msg}
        with self._lock:
            self._sent += 1
            message_id = result.message_id or f"sl-{self._sent}"
        return {"message_id": _as_int(message_id) or message_id}

    def _send_private(self, params: dict[str, Any]) -> dict[str, Any]:
        target = str(params.get("user_id") or "")
        return self._do_send(target, self._message_of(params), group=False)

    def _send_group(self, params: dict[str, Any]) -> dict[str, Any]:
        target = str(params.get("group_id") or "")
        return self._do_send(target, self._message_of(params), group=True)

    def _send_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        kind = str(params.get("message_type") or "")
        if kind == "private" or (not kind and params.get("user_id")):
            return self._send_private(params)
        if kind == "group" or (not kind and params.get("group_id")):
            return self._send_group(params)
        raise ValueError("send_msg 需要 message_type=private/group，或给 user_id / group_id")

    # ---------------------------------------------------------------- 查询
    def _get_login_info(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"user_id": _as_int(self.self_id), "nickname": self.nickname()}

    def _get_status(self, params: dict[str, Any]) -> dict[str, Any]:
        """返回字段与 SnowLuma 一致：online / good / time（实例运行秒数）。"""
        try:
            info = self.backend.status()
        except BackendError:
            return {"online": False, "good": False, "time": int(time.time() - self._started)}
        return {
            "online": bool(info.get("online", True)),
            "good": bool(info.get("ok", True)),
            "time": int(time.time() - self._started),
        }

    def _get_version_info(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "app_name": self.app_name,
            "app_version": __version__,
            "protocol_version": "v11",
        }

    def _get_friend_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """必填字段与 SnowLuma 一致：user_id / nickname / remark。"""
        out = []
        for item in self.backend.friends():
            uid = item.get("uin") or item.get("user_id") or item.get("uid") or ""
            out.append({
                "user_id": _as_int(uid),
                "nickname": str(item.get("name") or item.get("nickname") or ""),
                "remark": str(item.get("remark") or ""),
            })
        return out

    def _get_group_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        """必填字段与 SnowLuma 一致：group_id / group_name / group_remark /
        member_count / max_member_count / group_all_shut。"""
        out = []
        for item in self.backend.groups():
            gid = item.get("uin") or item.get("group_id") or item.get("uid") or ""
            out.append({
                "group_id": _as_int(gid),
                "group_name": str(item.get("name") or item.get("group_name") or ""),
                "group_remark": str(item.get("remark") or ""),
                "member_count": int(item.get("member_count") or 0),
                "max_member_count": int(item.get("max_member_count") or 0),
                "group_all_shut": int(item.get("group_all_shut") or 0),
                "group_create_time": int(item.get("group_create_time") or 0),
                "group_level": int(item.get("group_level") or 0),
                "group_memo": str(item.get("group_memo") or ""),
                "group_description": str(item.get("group_description") or ""),
            })
        return out

    def _get_group_info(self, params: dict[str, Any]) -> dict[str, Any]:
        gid = str(params.get("group_id") or "")
        for item in self.backend.groups():
            candidate = str(item.get("uin") or item.get("group_id") or "")
            if candidate and candidate == gid:
                return {
                    "group_id": _as_int(gid),
                    "group_name": str(item.get("name") or ""),
                    "member_count": int(item.get("member_count") or 0),
                }
        return {"__fail__": True, "code": RET_ACTION_FAILED, "msg": f"找不到群 {gid} 的资料"}

    def _get_msg(self, params: dict[str, Any]) -> dict[str, Any]:
        """与 SnowLuma 一致：消息事件去掉 post_type / self_id，补 real_id。"""
        mid = str(params.get("message_id") or "")
        with self._lock:
            event = self._cache.get(mid)
        if event is None:
            return {"__fail__": True, "code": RET_ACTION_FAILED, "msg": "message not found"}
        out = dict(event)
        out.pop("post_type", None)
        out.pop("self_id", None)
        out["real_id"] = out.get("message_id") or _as_int(mid) or mid
        return out

    def _can_send_image(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"yes": False}

    def _can_send_record(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"yes": False}

    def _get_capabilities(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return self.backend.capabilities()
        except BackendError as exc:
            return {"__fail__": True, "code": exc.code, "msg": exc.msg}

    # ---------------------------------------------------------------- 事件
    def event_from_message(self, msg: dict[str, Any]) -> dict[str, Any] | None:
        """ArkLink 的一条消息 -> OneBot v11 的 message 事件。"""
        text = str(msg.get("text") or "")
        segments = [{"type": "text", "data": {"text": text}}] if text else []
        if not segments:
            # 空消息（只有非文本内容）也让上层看见，别静默丢
            segments = []
        chat_type = int(msg.get("chat_type") or 1)
        is_group = chat_type == 2
        peer = msg.get("peer_uid") or ""
        sender = msg.get("sender_uid") or peer
        mid = str(msg.get("id") or msg.get("seq") or "")
        message_id = _as_int(mid) or mid
        base: dict[str, Any] = {
            "time": int(float(msg.get("ts") or time.time())),
            "self_id": _as_int(self.self_id),
            "post_type": "message",
            "message_id": message_id,
            "message": self._render_message(segments),
            "raw_message": text,
            "font": 0,
            "sender": {
                "user_id": _as_int(sender),
                "nickname": str(msg.get("sender_name") or ""),
                "card": "",
            },
            "snowlink": {
                "seq": msg.get("seq"),
                "peer_uid": peer,
                "peer_name": msg.get("peer_name"),
                "sender_uid": sender,
                "source": msg.get("source"),
            },
        }
        if is_group:
            base.update({
                "message_type": "group",
                "sub_type": "normal",
                "group_id": _as_int(peer),
                "anonymous": None,
                "sender": {**base["sender"], "role": "member"},
            })
        else:
            base.update({
                "message_type": "private",
                "sub_type": "friend",
                "user_id": _as_int(sender),
            })
        with self._lock:
            if mid:
                self._cache[str(mid)] = base
                while len(self._cache) > 500:
                    self._cache.popitem(last=False)
            self.stats["events"] += 1
        return base

    def event_sent(self, target: str, group: bool, message_id: Any, segments: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "time": _now(),
            "self_id": _as_int(self.self_id),
            "post_type": "message_sent",
            "message_type": "group" if group else "private",
            "sub_type": "normal" if group else "friend",
            "message_id": message_id,
            "message": self._render_message(segments),
            "raw_message": seg_mod.text_of(segments),
            "font": 0,
            "sender": {"user_id": _as_int(self.self_id), "nickname": self.nickname(), "card": ""},
            **({"group_id": _as_int(target)} if group else {"user_id": _as_int(target)}),
        }

    def _render_message(self, segments: list[dict[str, Any]]) -> Any:
        if self.message_format == "string":
            return seg_mod.render_cq(segments)
        return segments

    def lifecycle(self, sub_type: str = "connect") -> dict[str, Any]:
        return {
            "time": _now(),
            "self_id": _as_int(self.self_id),
            "post_type": "meta_event",
            "meta_event_type": "lifecycle",
            "sub_type": sub_type,
        }

    def heartbeat(self, interval: int) -> dict[str, Any]:
        return {
            "time": _now(),
            "self_id": _as_int(self.self_id),
            "post_type": "meta_event",
            "meta_event_type": "heartbeat",
            "status": {"online": True, "good": True},
            "interval": interval * 1000,
        }
