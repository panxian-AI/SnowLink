"""OneBot v11 消息段：数组 / CQ 字符串 两种格式互相转换。

SnowLuma 的 `messageFormat` 有 `array` 和 `string` 两种，两边都支持才算能顶替。
底层（ArkLink）目前只能发文本，所以这里还要回答一个问题：
**这条消息能不能原样发出去**——不能就明确说出来，不要发个 `[图片]` 占位符假装成功。
"""

from __future__ import annotations

import re
from typing import Any, Iterable

#: 段类型 -> 中文名。用于「不支持」时的提示。
TYPE_NAMES = {
    "text": "文本",
    "face": "表情",
    "image": "图片",
    "record": "语音",
    "video": "视频",
    "file": "文件",
    "at": "提及",
    "reply": "回复",
    "forward": "合并转发",
    "node": "合并转发节点",
    "json": "卡片",
    "xml": "卡片",
    "markdown": "Markdown",
    "poke": "戳一戳",
    "dice": "骰子",
    "rps": "猜拳",
    "share": "链接分享",
    "contact": "名片",
    "location": "位置",
    "music": "音乐",
    "mface": "商城表情",
}

#: 当前底层通道（ArkLink UI 通道）能发的段
SENDABLE = {"text"}


# ---------------------------------------------------------------- CQ 字符串
_CQ_RE = re.compile(r"\[CQ:([a-zA-Z_]+)((?:,[^\]]*)?)\]")


def _unescape(value: str) -> str:
    return (
        value.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def _escape(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(",", "&#44;")
    )


def parse_cq(text: str) -> list[dict[str, Any]]:
    """把 `hi[CQ:face,id=1]` 拆成段数组；不认识的 CQ 码按原文保留。"""
    out: list[dict[str, Any]] = []
    pos = 0
    for m in _CQ_RE.finditer(text):
        if m.start() > pos:
            chunk = text[pos : m.start()]
            if chunk:
                out.append({"type": "text", "data": {"text": _unescape(chunk)}})
        kind = m.group(1)
        data: dict[str, Any] = {}
        raw_args = (m.group(2) or "").lstrip(",")
        if raw_args:
            for pair in raw_args.split(","):
                if "=" in pair:
                    k, v = pair.split("=", 1)
                    data[k] = _unescape(v)
        out.append({"type": kind, "data": data})
        pos = m.end()
    if pos < len(text):
        chunk = text[pos:]
        if chunk:
            out.append({"type": "text", "data": {"text": _unescape(chunk)}})
    return out or [{"type": "text", "data": {"text": ""}}]


def render_cq(segments: Iterable[dict[str, Any]]) -> str:
    parts: list[str] = []
    for seg in segments:
        kind = str(seg.get("type") or "text")
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(_escape(str(data.get("text") or "")))
            continue
        args = ",".join(f"{k}={_escape(str(v))}" for k, v in data.items())
        parts.append(f"[CQ:{kind}{',' + args if args else ''}]")
    return "".join(parts)


# ---------------------------------------------------------------- 归一化
def normalize(raw: Any) -> list[dict[str, Any]]:
    """把外部传进来的 message 一律变成「段数组」，格式对不上就抛 ValueError。"""
    if raw is None:
        return [{"type": "text", "data": {"text": ""}}]
    if isinstance(raw, str):
        return parse_cq(raw)
    if isinstance(raw, dict):
        if "type" in raw:
            return [_clean_segment(raw)]
        raise ValueError("message 是对象时必须带 type（OneBot 段格式）")
    if isinstance(raw, (list, tuple)):
        if not raw:
            return [{"type": "text", "data": {"text": ""}}]
        out = []
        for item in raw:
            if isinstance(item, str):
                out.extend(parse_cq(item))
            elif isinstance(item, dict) and "type" in item:
                out.append(_clean_segment(item))
            else:
                raise ValueError("段数组里的元素必须是 {type,data} 或字符串")
        return out
    raise ValueError(f"不能识别的 message 类型：{type(raw).__name__}")


def _clean_segment(seg: dict[str, Any]) -> dict[str, Any]:
    kind = str(seg.get("type") or "text")
    data = seg.get("data")
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"段 {kind} 的 data 必须是对象")
    # 常见的简写：{"type":"text","data":"hi"}（OneBot 实现里允许）
    if kind == "text" and not isinstance(data, dict):
        data = {"text": str(data)}
    return {"type": kind, "data": dict(data)}


def text_of(segments: Iterable[dict[str, Any]]) -> str:
    """只取文本部分（拼起来）。"""
    chunks: list[str] = []
    for seg in segments:
        if str(seg.get("type")) == "text":
            chunks.append(str((seg.get("data") or {}).get("text") or ""))
    return "".join(chunks)


def describe(segments: Iterable[dict[str, Any]]) -> str:
    """给人看的一句话：`文本+图片+提及`。"""
    kinds: list[str] = []
    for seg in segments:
        kind = str(seg.get("type") or "text")
        name = TYPE_NAMES.get(kind, kind)
        if name not in kinds:
            kinds.append(name)
    return "+".join(kinds) or "空消息"


def unsupported(segments: Iterable[dict[str, Any]]) -> list[str]:
    """列出这条消息里**底层发不出去**的段类型（去重、保序）。"""
    out: list[str] = []
    for seg in segments:
        kind = str(seg.get("type") or "text")
        if kind not in SENDABLE and kind not in out:
            out.append(kind)
    return out


def to_text_body(segments: Iterable[dict[str, Any]]) -> str:
    """把消息压成纯文本：文本原样，其他段用 `[图片]` 这类标记。

    只用于「调用方明确要求降级」（auto_escape / dry_run）以及演示后端，
    正常发送路径不允许悄悄降级——那会让调用方以为图片发出去了。
    """
    parts: list[str] = []
    for seg in segments:
        kind = str(seg.get("type") or "text")
        data = seg.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            qq = str(data.get("qq") or "")
            parts.append("@" + ("全体成员" if qq == "all" else qq))
        elif kind == "face":
            parts.append("[表情]")
        else:
            parts.append(f"[{TYPE_NAMES.get(kind, kind)}]")
    return "".join(parts)


def reply_id(segments: Iterable[dict[str, Any]]) -> str:
    """取 reply 段里的消息 id（没有就空串）。"""
    for seg in segments:
        if str(seg.get("type")) == "reply":
            return str((seg.get("data") or {}).get("id") or "")
    return ""
