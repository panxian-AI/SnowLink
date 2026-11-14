"""消息段：三种入参形态、CQ 编解码、校验文案、元素 <-> 段互转。

这里的断言大多直接照 SnowLuma 的行为写（文案一字不差），因为上层会把这些
字符串原样打进日志甚至回给用户。
"""

from __future__ import annotations

import pytest

from snowlink import segments as seg
from snowlink.errors import SegmentError


# ---------------------------------------------------------------- CQ 串
def test_parse_cq_mixed() -> None:
    out = seg.parse_from_cq("你好[CQ:face,id=14]世界")
    assert [x["type"] for x in out] == ["text", "face", "text"]
    assert out[0]["text"] == "你好"
    assert out[1]["faceId"] == 14


def test_parse_cq_unescapes_text() -> None:
    out = seg.parse_from_cq("a&#91;b&#93;&#44;c&amp;d")
    assert out[0]["text"] == "a[b],c&d"


def test_render_cq_roundtrip() -> None:
    segments = [
        {"type": "text", "data": {"text": "hi[1],2"}},
        {"type": "face", "data": {"id": 1}},
    ]
    text = seg.segments_to_raw(segments)
    assert text == "hi&#91;1&#93;&#44;2[CQ:face,id=1]"
    back = seg.parse_from_cq(text)
    assert back[0]["text"] == "hi[1],2"
    assert back[1]["type"] == "face"


def test_cq_unescapes_param_values() -> None:
    out = seg.parse_from_cq("[CQ:at,qq=10001,name=a&#44;b]")
    assert out[0]["targetUin"] == 10001
    assert out[0]["text"] == "@a,b "


# ---------------------------------------------------------------- 入参形态
def test_parse_message_accepts_three_shapes() -> None:
    assert seg.parse_message("hi")[0]["text"] == "hi"
    assert seg.parse_message([{"type": "text", "data": {"text": "hi"}}])[0]["text"] == "hi"
    assert seg.parse_message({"type": "text", "data": {"text": "hi"}})[0]["text"] == "hi"


def test_segment_payload_merges_top_level_and_data() -> None:
    """`{"type":"text","text":"hi"}` 与 `{"type":"text","data":{"text":"hi"}}` 等价。"""
    assert seg.parse_message([{"type": "text", "text": "hi"}])[0]["text"] == "hi"
    elem = seg.parse_message([{"type": "text", "text": "outer", "data": {"text": "inner"}}])[0]
    assert elem["text"] == "inner"


def test_empty_text_placeholder_is_dropped() -> None:
    elements = seg.parse_message([
        {"type": "text", "data": {"text": ""}},
        {"type": "text", "data": {"text": "真正的内容"}},
    ])
    assert [e["text"] for e in elements] == ["真正的内容"]


def test_auto_escape_keeps_cq_literal() -> None:
    elements = seg.parse_message("看[CQ:face,id=1]", True)
    assert elements == [{"type": "text", "text": "看[CQ:face,id=1]"}]


# ---------------------------------------------------------------- 校验文案
def test_empty_message_rejections() -> None:
    with pytest.raises(SegmentError) as err:
        seg.parse_message("")
    assert err.value.message == "message must not be empty"

    with pytest.raises(SegmentError) as err:
        seg.parse_message([])
    assert err.value.message == "message segment array must not be empty"

    with pytest.raises(SegmentError) as err:
        seg.parse_message([{"type": "text", "data": {"text": ""}}])
    assert err.value.message == "message must contain at least one sendable segment"


def test_bad_shapes_rejected_with_exact_wording() -> None:
    with pytest.raises(SegmentError) as err:
        seg.parse_message(123)
    assert err.value.message == "message must be a string, a segment object, or a segment array"

    with pytest.raises(SegmentError) as err:
        seg.parse_message([{"data": {"text": "no type"}}])
    assert err.value.message == (
        "message segment array entries must be objects with a non-empty string type"
    )

    with pytest.raises(SegmentError) as err:
        seg.parse_message({"data": {"text": "no type"}})
    assert err.value.message == "single message segment requires a non-empty string type"


def test_unknown_segment_type() -> None:
    with pytest.raises(SegmentError) as err:
        seg.parse_message([{"type": "no_such", "data": {}}])
    assert err.value.code == "UNKNOWN_TYPE"
    assert err.value.message == "unknown message segment type: no_such"


def test_node_segment_is_unsendable() -> None:
    with pytest.raises(SegmentError) as err:
        seg.parse_message([{"type": "node", "data": {}}])
    assert err.value.code == "UNSENDABLE_TYPE"


def test_field_level_validation_messages() -> None:
    with pytest.raises(SegmentError) as err:
        seg.parse_message([{"type": "share", "data": {"title": "t"}}])
    assert err.value.message == 'message segment "share" field "url" must be a non-empty string'

    with pytest.raises(SegmentError) as err:
        seg.parse_message([{"type": "face", "data": {"id": "abc"}}])
    assert "must contain only an integer" in err.value.message


# ---------------------------------------------------------------- 段类型覆盖
def test_segment_types_map_to_expected_elements() -> None:
    cases = {
        "face": {"id": 1},
        "at": {"qq": "10001"},
        "image": {"file": "a.png"},
        "record": {"file": "a.amr"},
        "video": {"file": "a.mp4"},
        "json": {"data": "{}"},
        "xml": {"data": "<x/>"},
        "file": {"file_id": "f1", "name": "a.txt"},
        "mface": {"emoji_id": "1234"},
        "poke": {"type": 1},
        "forward": {"id": "res1"},
        "markdown": {"content": "# hi"},
    }
    for kind, data in cases.items():
        elements = seg.parse_message([{"type": kind, "data": data}])
        assert elements, kind


def test_share_music_location_contact_become_json_cards() -> None:
    share = seg.parse_message([{"type": "share", "data": {"url": "https://a", "title": "t"}}])[0]
    assert share["type"] == "json" and "com.tencent.structmsg" in share["text"]

    location = seg.parse_message([{"type": "location", "data": {"lat": 1.5, "lon": 2.5}}])[0]
    assert location["type"] == "json" and "LocationShare" in location["text"]

    contact = seg.parse_message([{"type": "contact", "data": {"type": "qq", "id": "10001"}}])[0]
    assert contact["type"] == "json" and "com.tencent.contact.lua" in contact["text"]


def test_rps_dice_shake_are_faces_and_pokes() -> None:
    assert seg.parse_message([{"type": "rps"}])[0] == {"type": "face", "faceId": 359}
    assert seg.parse_message([{"type": "dice"}])[0] == {"type": "face", "faceId": 358}
    assert seg.parse_message([{"type": "shake"}])[0] == {"type": "poke", "subType": 1}


# ---------------------------------------------------------------- 元素 -> 段
def test_element_to_segment_shapes() -> None:
    assert seg.element_to_segment({"type": "text", "text": "hi"}) == {
        "type": "text", "data": {"text": "hi"},
    }
    assert seg.element_to_segment({"type": "at", "targetUin": 0, "uid": "all"}) == {
        "type": "at", "data": {"qq": "all"},
    }
    assert seg.element_to_segment({"type": "mface", "emojiId": "12"})["type"] == "image"


def test_unsupported_reports_types_in_order() -> None:
    elements = seg.parse_message([
        {"type": "text", "data": {"text": "看这个"}},
        {"type": "face", "data": {"id": "1"}},
        {"type": "at", "data": {"qq": "10001"}},
        {"type": "face", "data": {"id": "2"}},
    ])
    assert seg.unsupported_elements(elements) == ["face", "at"]
    assert seg.unsupported_elements([{"type": "text", "text": "ok"}]) == []
    # 图片和文件现在可发，不该出现在「不支持」里
    assert seg.unsupported_elements([{"type": "image", "url": "a.png"},
                                     {"type": "file", "url": "/tmp/a.pdf"}]) == []


def test_text_of_elements() -> None:
    assert seg.text_of_elements([{"type": "text", "text": "a"},
                                 {"type": "face", "faceId": 1},
                                 {"type": "text", "text": "b"}]) == "ab"


def test_describe() -> None:
    segments = [{"type": "text", "data": {"text": "x"}},
                {"type": "image", "data": {}}]
    assert seg.describe(segments) == "文本+图片"
