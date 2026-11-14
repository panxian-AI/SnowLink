"""动作分发：能做的真返回，不能做的给 1404 + 理由，绝不假装成功。"""

from __future__ import annotations

from snowlink import onebot as ob
from snowlink.backend import DemoBackend


def make() -> ob.Dispatcher:
    return ob.Dispatcher(DemoBackend(), self_id="10000", message_format="array")


def test_send_private_ok() -> None:
    dispatcher = make()
    result = dispatcher.call("send_private_msg", {"user_id": 10001, "message": "hi"})
    assert result["status"] == "ok" and result["retcode"] == 0
    assert result["data"]["message_id"]
    assert dispatcher.backend.sent[0]["text"] == "hi"


def test_send_group_ok() -> None:
    dispatcher = make()
    result = dispatcher.call("send_group_msg", {
        "group_id": 20001,
        "message": [{"type": "text", "data": {"text": "群里好"}}],
    })
    assert result["retcode"] == 0
    assert dispatcher.backend.sent[0]["group"] is True


def test_send_msg_routes_by_type() -> None:
    dispatcher = make()
    assert dispatcher.call("send_msg", {"message_type": "private", "user_id": 1,
                                        "message": "a"})["retcode"] == 0
    assert dispatcher.call("send_msg", {"message_type": "group", "group_id": 2,
                                        "message": "b"})["retcode"] == 0
    assert [x["group"] for x in dispatcher.backend.sent] == [False, True]


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


def test_unknown_action_lists_supported() -> None:
    result = make().call("no_such_action", {})
    assert result["retcode"] == 1404
    assert "不认识的动作" in result["wording"]


def test_known_unsupported_action_has_reason() -> None:
    result = make().call("delete_msg", {"message_id": 1})
    assert result["retcode"] == 1404
    assert "撤回" in result["wording"]


def test_missing_target_is_bad_param() -> None:
    result = make().call("send_private_msg", {"message": "hi"})
    assert result["retcode"] == 1400
    assert "user_id" in result["wording"]


def test_login_info_and_version() -> None:
    dispatcher = make()
    info = dispatcher.call("get_login_info", {})
    assert info["data"]["user_id"] == 10000
    version = dispatcher.call("get_version_info", {})
    assert version["data"]["app_name"] == "SnowLink"
    assert version["data"]["protocol_version"] == "v11"


def test_friend_and_group_lists() -> None:
    dispatcher = make()
    friends = dispatcher.call("get_friend_list", {})["data"]
    groups = dispatcher.call("get_group_list", {})["data"]
    assert friends[0]["user_id"] == 10001
    assert groups[0]["group_id"] == 20001


def test_can_send_image_is_false() -> None:
    dispatcher = make()
    assert dispatcher.call("can_send_image", {})["data"] == {"yes": False}
    assert dispatcher.call("can_send_record", {})["data"] == {"yes": False}


def test_event_from_message_private() -> None:
    dispatcher = make()
    event = dispatcher.event_from_message({
        "seq": 7, "id": "m1", "ts": 1700000000, "chat_type": 1,
        "peer_uid": "10001", "peer_name": "示例好友",
        "sender_uid": "10001", "sender_name": "示例好友",
        "text": "你好", "source": "ui",
    })
    assert event is not None
    assert event["post_type"] == "message"
    assert event["message_type"] == "private"
    assert event["user_id"] == 10001
    assert event["self_id"] == 10000
    assert event["raw_message"] == "你好"
    assert event["snowlink"]["seq"] == 7


def test_event_from_message_group() -> None:
    dispatcher = make()
    event = dispatcher.event_from_message({
        "seq": 8, "id": "m2", "ts": 1700000000, "chat_type": 2,
        "peer_uid": "20001", "peer_name": "示例群",
        "sender_uid": "10001", "sender_name": "示例好友", "text": "群消息",
    })
    assert event is not None
    assert event["message_type"] == "group"
    assert event["group_id"] == 20001
    assert event["sender"]["role"] == "member"


def test_get_msg_hits_cache() -> None:
    dispatcher = make()
    dispatcher.event_from_message({
        "seq": 9, "id": "m3", "ts": 1700000000, "chat_type": 1,
        "peer_uid": "10001", "sender_uid": "10001", "text": "缓存我",
    })
    result = dispatcher.call("get_msg", {"message_id": "m3"})
    assert result["retcode"] == 0
    assert result["data"]["raw_message"] == "缓存我"


def test_message_format_string_renders_cq() -> None:
    dispatcher = ob.Dispatcher(DemoBackend(), self_id="10000", message_format="string")
    event = dispatcher.event_from_message({
        "seq": 1, "id": "m4", "ts": 1700000000, "chat_type": 1,
        "sender_uid": "10001", "text": "hi",
    })
    assert event is not None
    assert isinstance(event["message"], str)
    assert event["message"] == "hi"


def test_stats_counts() -> None:
    dispatcher = make()
    dispatcher.call("get_status", {})
    dispatcher.call("delete_msg", {"message_id": 1})
    assert dispatcher.stats["actions"] == 2
    assert dispatcher.stats["unsupported"] == 1
