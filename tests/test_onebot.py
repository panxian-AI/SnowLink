"""动作分发：参数 schema、retcode 映射、消息 ID、事件形状。"""

from __future__ import annotations

from snowlink import events as ev
from snowlink import onebot as ob
from snowlink.backend import DemoBackend


def make(fmt: str = "array") -> ob.Dispatcher:
    return ob.Dispatcher(DemoBackend(), self_id="10000", message_format=fmt)


# ---------------------------------------------------------------- 发送
def test_send_private_ok() -> None:
    dispatcher = make()
    result = dispatcher.call("send_private_msg", {"user_id": 10001, "message": "hi"})
    assert (result["status"], result["retcode"]) == ("ok", 0)
    assert isinstance(result["data"]["message_id"], int)
    assert dispatcher.backend.sent[0]["text"] == "hi"


def test_send_group_ok() -> None:
    dispatcher = make()
    result = dispatcher.call("send_group_msg", {
        "group_id": 20001,
        "message": [{"type": "text", "data": {"text": "群里好"}}],
    })
    assert result["retcode"] == 0
    assert dispatcher.backend.sent[0]["group"] is True


def test_message_id_is_stable_hash_and_can_be_negative() -> None:
    """message_id 是 int32 哈希：同一会话同一序号必得同一 id，且允许为负。"""
    dispatcher = make()
    first = dispatcher.call("send_group_msg", {"group_id": 20001, "message": "a"})["data"]["message_id"]
    assert isinstance(first, int) and first != 0
    meta = dispatcher.store.find_meta(first)
    assert meta is not None and meta["is_group"] is True


def test_send_msg_routes_by_type() -> None:
    dispatcher = make()
    assert dispatcher.call("send_msg", {"message_type": "private", "user_id": 1,
                                        "message": "a"})["retcode"] == 0
    assert dispatcher.call("send_msg", {"message_type": "group", "group_id": 2,
                                        "message": "b"})["retcode"] == 0
    assert [x["group"] for x in dispatcher.backend.sent] == [False, True]


def test_send_msg_requires_a_target() -> None:
    result = make().call("send_msg", {"message": "hi"})
    assert result["retcode"] == 1400
    assert result["wording"] == "at least one of: group_id, user_id"


def test_image_is_refused_with_reason() -> None:
    dispatcher = make()
    result = dispatcher.call("send_group_msg", {
        "group_id": 20001,
        "message": [{"type": "image", "data": {"file": "a.png"}}],
    })
    assert result["retcode"] == 1404
    assert "图片" in result["wording"]
    assert dispatcher.backend.sent == []  # 没有偷偷降级成 [图片]


def test_reply_segment_refused() -> None:
    dispatcher = make()
    result = dispatcher.call("send_private_msg", {
        "user_id": 1,
        "message": [{"type": "reply", "data": {"id": "9"}},
                    {"type": "text", "data": {"text": "在"}}],
    })
    assert result["retcode"] == 1404 and "回复" in result["wording"]


def test_empty_message_wording_matches_upstream() -> None:
    result = make().call("send_private_msg", {"user_id": 1, "message": ""})
    assert result["retcode"] == 1400
    assert result["wording"] == "message must not be empty"


def test_missing_param_wording() -> None:
    result = make().call("send_private_msg", {"message": "hi"})
    assert result["retcode"] == 1400
    assert result["wording"] == "user_id: is required"


def test_bad_param_type_wording() -> None:
    result = make().call("send_private_msg", {"user_id": "abc", "message": "hi"})
    assert result["retcode"] == 1400
    assert result["wording"] == "user_id: expected a positive integer"


def test_numeric_string_is_accepted() -> None:
    """表单 / 查询串会把数字送来成字符串，SnowLuma 收，我们也收。"""
    result = make().call("send_private_msg", {"user_id": "10001", "message": "hi"})
    assert result["retcode"] == 0


# ---------------------------------------------------------------- 动作面
def test_unknown_action_wording_matches_upstream() -> None:
    result = make().call("no_such_action", {})
    assert result["retcode"] == 1404
    assert result["wording"] == "unknown action"


def test_known_unsupported_action_has_reason() -> None:
    result = make().call("get_group_member_list", {"group_id": 20001})
    assert result["retcode"] == 1404
    assert "群成员" in result["wording"]


def test_unsupported_action_still_validates_params() -> None:
    """参数不合法时先报 1400（与上游「先校验再执行」的顺序一致）。"""
    result = make().call("get_group_member_list", {})
    assert result["retcode"] == 1400
    assert result["wording"] == "group_id: is required"


def test_delete_msg_without_meta() -> None:
    result = make().call("delete_msg", {"message_id": 12345})
    assert result["retcode"] == 100
    assert result["wording"] == "message not found or not retractable"


def test_delete_msg_on_channel_without_recall() -> None:
    dispatcher = make()
    dispatcher.event_from_message({
        "seq": 3, "ts": 1700000000, "chat_type": 1,
        "peer_uid": "10001", "sender_uid": "10001", "text": "撤回我",
    })
    mid = next(iter(dispatcher.store._meta))  # noqa: SLF001 - 测试里取已知 id
    result = dispatcher.call("delete_msg", {"message_id": mid})
    assert result["retcode"] == 1404
    assert "撤回" in result["wording"]


# ---------------------------------------------------------------- 查询
def test_login_info_and_version() -> None:
    dispatcher = make()
    info = dispatcher.call("get_login_info", {})
    assert info["data"]["user_id"] == 10000
    assert set(info["data"]) == {"user_id", "nickname"}
    version = dispatcher.call("get_version_info", {})
    assert version["data"]["app_name"] == "SnowLink"
    assert version["data"]["protocol_version"] == "v11"


def test_status_has_time_field() -> None:
    data = make().call("get_status", {})["data"]
    assert set(data) == {"online", "good", "time"}
    assert isinstance(data["time"], int)


def test_friend_and_group_lists() -> None:
    dispatcher = make()
    friend = dispatcher.call("get_friend_list", {})["data"][0]
    group = dispatcher.call("get_group_list", {})["data"][0]
    assert set(friend) == {"user_id", "nickname", "remark"}
    assert {"group_id", "group_name", "group_remark", "member_count",
            "max_member_count", "group_all_shut"} <= set(group)


def test_group_info_falls_back_to_empty_shape() -> None:
    data = make().call("get_group_info", {"group_id": 999})["data"]
    assert data["group_id"] == 999 and data["group_name"] == ""


def test_can_send_image_is_false() -> None:
    dispatcher = make()
    assert dispatcher.call("can_send_image", {})["data"] == {"yes": False}
    assert dispatcher.call("can_send_record", {})["data"] == {"yes": False}


# ---------------------------------------------------------------- 事件
def test_event_from_message_private() -> None:
    dispatcher = make()
    event = dispatcher.event_from_message({
        "seq": 7, "ts": 1700000000, "chat_type": 1,
        "peer_uid": "10001", "peer_name": "示例好友",
        "sender_uid": "10001", "sender_name": "示例好友",
        "text": "你好", "source": "ui",
    })
    assert event is not None
    assert event["post_type"] == "message"
    assert event["message_type"] == "private"
    assert event["sub_type"] == "friend"
    assert event["user_id"] == 10001
    assert event["self_id"] == 10000
    assert event["raw_message"] == "你好"
    assert event["message_seq"] == 7
    assert event["message"] == [{"type": "text", "data": {"text": "你好"}}]


def test_event_from_message_group() -> None:
    dispatcher = make()
    event = dispatcher.event_from_message({
        "seq": 8, "ts": 1700000000, "chat_type": 2,
        "peer_uid": "20001", "peer_name": "示例群",
        "sender_uid": "10001", "sender_name": "示例好友", "text": "群消息",
    })
    assert event is not None
    assert event["message_type"] == "group"
    assert event["group_id"] == 20001
    assert event["group_name"] == "示例群"
    assert event["anonymous"] is None
    assert event["sender"]["role"] == "member"


def test_self_sent_message_becomes_message_sent() -> None:
    dispatcher = make()
    event = dispatcher.event_from_message({
        "seq": 9, "ts": 1700000000, "chat_type": 1,
        "peer_uid": "10001", "sender_uid": "10000", "sender_name": "我", "text": "自己发的",
    })
    assert event is not None
    assert event["post_type"] == "message_sent"
    assert event["user_id"] == 10000
    assert event["target_id"] == 10001


def test_get_msg_and_reply_roundtrip() -> None:
    dispatcher = make()
    event = dispatcher.event_from_message({
        "seq": 10, "ts": 1700000000, "chat_type": 1,
        "peer_uid": "10001", "sender_uid": "10001", "text": "缓存我",
    })
    assert event is not None
    mid = event["message_id"]
    result = dispatcher.call("get_msg", {"message_id": mid})
    assert result["retcode"] == 0
    assert result["data"]["raw_message"] == "缓存我"
    assert "post_type" not in result["data"] and "self_id" not in result["data"]
    assert result["data"]["real_id"] == mid

    # 用这个 id 回复时，reply 段能解析出被引用消息的序号（虽然本通道还发不出去）
    assert dispatcher._options(10001).resolve_reply_sequence(mid) == 10  # noqa: SLF001


def test_get_msg_miss_wording() -> None:
    result = make().call("get_msg", {"message_id": 42})
    assert result["retcode"] == 100
    assert result["wording"] == "message not found"


def test_message_format_shaping_happens_at_exit() -> None:
    dispatcher = make("string")
    event = dispatcher.event_from_message({
        "seq": 1, "ts": 1700000000, "chat_type": 1,
        "sender_uid": "10001", "text": "hi",
    })
    assert event is not None
    # 事件本身始终是段数组，出口按连接格式整形
    assert isinstance(event["message"], list)
    shaped = ev.shape(event, "string")
    assert shaped["message"] == "hi"


def test_meta_events() -> None:
    dispatcher = make()
    lifecycle = dispatcher.lifecycle("connect")
    assert lifecycle["meta_event_type"] == "lifecycle"
    assert lifecycle["sub_type"] == "connect"
    assert lifecycle["status"] == {"online": True, "good": True}

    heartbeat = dispatcher.heartbeat()
    assert heartbeat["interval"] == 30000


def test_stats_counts() -> None:
    dispatcher = make()
    dispatcher.call("get_status", {})
    dispatcher.call("get_group_member_list", {"group_id": 20001})
    assert dispatcher.stats["actions"] == 2
    assert dispatcher.stats["unsupported"] == 1
