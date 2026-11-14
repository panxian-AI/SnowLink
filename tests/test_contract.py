"""契约对齐：动作面、retcode 枚举、返回结构都与 SnowLuma 一致。"""

from __future__ import annotations

import pytest

from snowlink import contract as C
from snowlink import onebot as ob
from snowlink.backend import DemoBackend


def make(**kw) -> ob.Dispatcher:
    return ob.Dispatcher(DemoBackend(), self_id="10000", message_format="array", **kw)


def test_action_face_is_complete() -> None:
    """213 个动作全部登记，一个不少。"""
    assert C.ACTION_COUNT == 213
    assert len(C.ACTIONS) == 213


def test_every_unimplemented_action_gives_a_reason() -> None:
    dispatcher = make()
    implemented = set(dispatcher._handlers())
    missing = []
    for name in C.ACTIONS:
        if name in implemented:
            continue
        result = dispatcher.call(name, {})
        if result["retcode"] != 1404 or not result["wording"]:
            missing.append((name, result))
    assert not missing, f"{len(missing)} 个动作没有明确原因：{missing[:3]}"


def test_reason_text_is_actionable() -> None:
    """原因里必须提到这个动作名，否则上层看不出是哪一个失败。"""
    dispatcher = make()
    for name in ("set_group_ban", "get_group_member_list", "upload_group_file"):
        wording = dispatcher.call(name, {})["wording"]
        assert name in wording
        assert len(wording) > len(name) + 4


def test_retcode_enum_matches_upstream() -> None:
    assert (C.RET_OK, C.RET_ACTION_FAILED, C.RET_INTERNAL, C.RET_BAD_REQUEST,
            C.RET_UNKNOWN_ACTION) == (0, 100, 1200, 1400, 1404)


def test_ok_envelope_has_no_wording() -> None:
    """SnowLuma 的成功响应只有 status/retcode/data 三个键。"""
    result = make().call("get_login_info", {})
    assert set(result) == {"status", "retcode", "data"}


def test_failed_envelope_has_wording() -> None:
    result = make().call("get_group_member_list", {})
    assert set(result) == {"status", "retcode", "data", "wording"}
    assert result["data"] is None


def test_get_status_has_required_time() -> None:
    data = make().call("get_status", {})["data"]
    assert set(data) >= {"online", "good", "time"}
    assert isinstance(data["time"], int) and data["time"] >= 0


def test_get_group_list_required_fields() -> None:
    groups = make().call("get_group_list", {})["data"]
    assert groups
    required = {"group_id", "group_name", "group_remark", "member_count",
                "max_member_count", "group_all_shut"}
    for item in groups:
        assert required <= set(item)


def test_get_friend_list_required_fields() -> None:
    friends = make().call("get_friend_list", {})["data"]
    assert friends
    for item in friends:
        assert {"user_id", "nickname", "remark"} <= set(item)


def test_get_msg_shape_and_miss() -> None:
    dispatcher = make()
    miss = dispatcher.call("get_msg", {"message_id": 123})
    assert miss["retcode"] == ob.RET_ACTION_FAILED
    assert miss["wording"] == "message not found"

    event = dispatcher.event_from_message(
        {"id": "456", "text": "hi", "chat_type": 1, "sender_uid": "10001",
         "seq": 1, "ts": 1}
    )
    assert event is not None
    got = dispatcher.call("get_msg", {"message_id": 456})["data"]
    assert got["real_id"] == 456
    assert "post_type" not in got and "self_id" not in got


def test_version_info_is_three_keys() -> None:
    data = make().call("get_version_info", {})["data"]
    assert set(data) == {"app_name", "app_version", "protocol_version"}
    assert data["app_name"] == "SnowLink"


def test_app_name_override_for_strict_drop_in() -> None:
    data = make(app_name="SnowLuma").call("get_version_info", {})["data"]
    assert data["app_name"] == "SnowLuma"


@pytest.mark.parametrize("name", ["can_send_image", "can_send_record"])
def test_capability_queries_say_no(name: str) -> None:
    data = make().call(name, {})["data"]
    assert data == {"yes": False}
