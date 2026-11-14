"""消息段：数组 / CQ 字符串 双格式 + 「能不能发」的判定。"""

from __future__ import annotations

import pytest

from snowlink import segments as seg


def test_parse_cq_mixed() -> None:
    out = seg.parse_cq("你好[CQ:face,id=14]世界")
    assert [x["type"] for x in out] == ["text", "face", "text"]
    assert out[0]["data"]["text"] == "你好"
    assert out[1]["data"]["id"] == "14"


def test_parse_cq_escapes() -> None:
    out = seg.parse_cq("a&#91;b&#93;&#44;c&amp;d")
    assert out[0]["data"]["text"] == "a[b],c&d"


def test_render_cq_roundtrip() -> None:
    segs = [
        {"type": "text", "data": {"text": "hi[1],2"}},
        {"type": "face", "data": {"id": 1}},
    ]
    text = seg.render_cq(segs)
    assert text == "hi&#91;1&#93;&#44;2[CQ:face,id=1]"
    back = seg.parse_cq(text)
    assert back[0]["data"]["text"] == "hi[1],2"
    assert back[1]["type"] == "face"


def test_normalize_accepts_three_shapes() -> None:
    assert seg.normalize("hi")[0]["data"]["text"] == "hi"
    assert seg.normalize([{"type": "text", "data": {"text": "hi"}}])[0]["type"] == "text"
    assert seg.normalize({"type": "text", "data": {"text": "hi"}})[0]["type"] == "text"


def test_normalize_rejects_garbage() -> None:
    with pytest.raises(ValueError):
        seg.normalize(123)
    with pytest.raises(ValueError):
        seg.normalize([{"data": {"text": "no type"}}])


def test_unsupported_reports_types_in_order() -> None:
    segs = [
        {"type": "text", "data": {"text": "看这个"}},
        {"type": "image", "data": {"file": "a.png"}},
        {"type": "at", "data": {"qq": "123"}},
        {"type": "image", "data": {"file": "b.png"}},
    ]
    assert seg.unsupported(segs) == ["image", "at"]
    assert seg.unsupported([{"type": "text", "data": {"text": "ok"}}]) == []


def test_text_only_is_sendable() -> None:
    assert seg.unsupported([]) == []
    assert seg.text_of([{"type": "text", "data": {"text": "a"}},
                        {"type": "face", "data": {}},
                        {"type": "text", "data": {"text": "b"}}]) == "ab"


def test_to_text_body_marks_non_text() -> None:
    segs = [{"type": "text", "data": {"text": "看图"}},
            {"type": "image", "data": {}},
            {"type": "at", "data": {"qq": "all"}}]
    assert seg.to_text_body(segs) == "看图[图片]@全体成员"


def test_describe() -> None:
    segs = [{"type": "text", "data": {"text": "x"}},
            {"type": "image", "data": {}}]
    assert seg.describe(segs) == "文本+图片"


def test_reply_id() -> None:
    assert seg.reply_id([{"type": "reply", "data": {"id": "42"}}]) == "42"
    assert seg.reply_id([]) == ""
