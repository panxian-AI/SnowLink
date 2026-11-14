"""消息段：CQ 字符串 / 段数组 / 单个段对象 三种入参，统一解析成内部元素。

这里的语义是照着 SnowLuma 逐条对齐的（读的是它发行包里的实现），包括：

* `segmentPayload`：段对象里 `data` 与顶层字段合并，`data` 优先（所以
  `{"type":"text","text":"hi"}` 和 `{"type":"text","data":{"text":"hi"}}` 等价）；
* 段类型名一律小写；
* 段数组里 `text` 恰好是 `""` 的段是**兼容占位符**，直接丢掉、不算可发送段；
* `auto_escape=true` 时字符串整段按字面文本处理，不解析 CQ 码；
* 校验失败的文案与 `code` 一字不差（`MISSING_FIELD` / `INVALID_FIELD` /
  `UNSENDABLE_TYPE` / `UNKNOWN_TYPE`），因为上层会把它打进日志甚至回给用户。

真正发不出去的段（当前通道只能发文本）**不降级成 `[图片]`**，而是让发送动作
返回 1404 并说明是哪几段——这是与 SnowLuma 唯一的实质差异，写在文档里。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Iterable

from .errors import SegmentError

#: 段类型 -> 中文名（只用于日志/报错，不参与协议）
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
    "flash_file": "闪传文件",
    "inline_keyboard": "按钮面板",
}

#: 真能发出去的段类型。ArkLink 的 UI 通道把本地文件放进剪贴板再粘贴，
#: 所以图片和文件都能发——但只能发**本机已有**的文件，URL 得先落盘。
SENDABLE = {"text", "image", "file"}

#: 需要伴随真实文件的段（含只允许「当文件发」的那两类）
ATTACHMENT_KINDS = {"image", "file", "video", "record"}

#: 只能以「文件」形式到达的段：剪贴板粘一个 mp4 过去，对方收到的是附件，
#: 不是可播放的视频消息，更不是语音条。所以单独列出来，好给准确的原因。
FILE_ONLY_KINDS = {"video", "record"}

_REMOTE_PREFIXES = ("http://", "https://", "base64://", "data:", "file://")

_CQ_RE = re.compile(r"\[CQ:([A-Za-z][A-Za-z0-9_]*)(?:,([^\]]*))?\]")


def file_of(segment: dict[str, Any]) -> str:
    """从一个附件段里取文件位置：`file` → `path` → `url`。

    OneBot 实现们对同一个字段各有各的写法，段里通常只出现其中一个，
    所以按优先级挨个看，别只认 `file`。
    """
    data = segment.get("data")
    if not isinstance(data, dict):
        return ""
    for key in ("file", "path", "url"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def is_remote(segment: dict[str, Any]) -> bool:
    """这个附件段给的是不是「地址」而不是本机路径。

    注意空值不算 remote（那是「缺 file 字段」，文案要分开说）。
    """
    target = file_of(segment).lower()
    return bool(target) and target.startswith(_REMOTE_PREFIXES)


def has_attachment(elements: Iterable[dict[str, Any]]) -> bool:
    """这批元素里有没有附件（图片/文件/视频/语音）。"""
    return any(str(elem.get("type") or "") in ATTACHMENT_KINDS for elem in elements)


# ---------------------------------------------------------------- CQ 编解码
def cq_escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("[", "&#91;")
        .replace("]", "&#93;")
        .replace(",", "&#44;")
    )


def cq_unescape(text: str) -> str:
    return (
        text.replace("&#91;", "[")
        .replace("&#93;", "]")
        .replace("&#44;", ",")
        .replace("&amp;", "&")
    )


def parse_cq_params(raw: str) -> dict[str, str]:
    params: dict[str, str] = {}
    if not raw:
        return params
    for pair in raw.split(","):
        eq = pair.find("=")
        if eq > 0:
            params[pair[:eq]] = cq_unescape(pair[eq + 1:])
    return params


def parse_from_cq(text: str, options: "Options | None" = None) -> list[dict[str, Any]]:
    """把 `hi[CQ:face,id=1]` 拆成元素列表（顺序、文本保留）。"""
    elements: list[dict[str, Any]] = []
    last = 0
    for match in _CQ_RE.finditer(text):
        if match.start() > last:
            chunk = cq_unescape(text[last:match.start()])
            if chunk:
                elements.append({"type": "text", "text": chunk})
        last = match.end()
        elem = segment_to_element(match.group(1), parse_cq_params(match.group(2) or ""), options)
        if elem is not None:
            elements.append(elem)
    if last < len(text):
        chunk = cq_unescape(text[last:])
        if chunk:
            elements.append({"type": "text", "text": chunk})
    return elements


def segment_to_cq(seg: dict[str, Any]) -> str:
    """段 -> CQ 码。规则与 SnowLuma 的 `segmentToCQ` 一致（含字段顺序）。"""
    kind = str(seg.get("type") or "")
    data = seg.get("data") or {}
    if kind == "text":
        return cq_escape(str(data.get("text") or ""))
    if kind == "face":
        extra = ""
        if data.get("resultId") is not None:
            extra = f",resultId={cq_escape(str(data['resultId']))}"
        return f"[CQ:face,id={data.get('id', 0)}{extra}]"
    if kind == "image":
        return f"[CQ:image,file={cq_escape(str(data.get('file') or ''))},url={cq_escape(str(data.get('url') or ''))}]"
    if kind == "at":
        return f"[CQ:at,qq={data.get('qq', '')}]"
    if kind == "reply":
        return f"[CQ:reply,id={data.get('id', 0)}]"
    if kind in ("record", "video"):
        return f"[CQ:{kind},file={cq_escape(str(data.get('file') or ''))},url={cq_escape(str(data.get('url') or ''))}]"
    if kind in ("json", "xml"):
        return f"[CQ:{kind},data={cq_escape(str(data.get('data') or ''))}]"
    if kind == "forward":
        return f"[CQ:forward,id={cq_escape(str(data.get('id') or ''))}]"
    if kind == "mface":
        return f"[CQ:mface,name={cq_escape(str(data.get('name') or ''))}]"
    if kind == "poke":
        return f"[CQ:poke,type={data.get('type', 0)}]"
    if kind == "file":
        return (
            f"[CQ:file,name={cq_escape(str(data.get('name') or ''))},"
            f"size={data.get('size', 0)},id={cq_escape(str(data.get('id') or ''))},"
            f"url={cq_escape(str(data.get('url') or ''))}]"
        )
    return f"[CQ:{kind}]"


def segments_to_raw(segments: Iterable[dict[str, Any]]) -> str:
    return "".join(segment_to_cq(seg) for seg in segments)


# ---------------------------------------------------------------- 段对象
def normalize_segment_type(kind: Any) -> str:
    return str(kind).lower()


def segment_payload(seg: dict[str, Any]) -> dict[str, Any]:
    """段对象里的字段：顶层 + `data` 合并，`data` 优先。"""
    top = {k: v for k, v in seg.items() if k not in ("type", "data")}
    if normalize_segment_type(seg.get("type")) == "json" and isinstance(seg.get("data"), str):
        return {**top, "data": seg["data"]}
    nested = seg.get("data") if isinstance(seg.get("data"), dict) else {}
    return {**top, **nested}


def is_segment_array(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, dict) and str(item.get("type") or "").strip() for item in value
    )


def is_empty_text_placeholder(seg: dict[str, Any]) -> bool:
    return normalize_segment_type(seg.get("type")) == "text" and segment_payload(seg).get("text") == ""


# ---------------------------------------------------------------- 字段校验
def _missing(message: str) -> SegmentError:
    return SegmentError("MISSING_FIELD", message, None, "message")


def int_or(value: Any, fallback: int = 0) -> int:
    """整数转换；不合法就抛，文案与 SnowLuma 一致。"""
    if value is None:
        return fallback
    if isinstance(value, bool):
        raise SegmentError("INVALID_FIELD", f"numeric message segment field must be a safe integer, received {value}")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value.is_integer():
            return int(value)
        raise SegmentError("INVALID_FIELD", f"numeric message segment field must be a safe integer, received {value}")
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return fallback
        if not re.fullmatch(r"[+-]?\d+", text):
            raise SegmentError(
                "INVALID_FIELD",
                f"numeric message segment field must contain only an integer, received {json.dumps(value, ensure_ascii=False)}",
            )
        return int(text)
    raise SegmentError("INVALID_FIELD", f"numeric message segment field must be a safe integer, received {value}")


def _require_non_empty(kind: str, data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SegmentError(
            "INVALID_FIELD",
            f'message segment "{kind}" field "{field}" must be a non-empty string',
            kind,
            field,
        )
    return value


def assert_scalar_data(kind: str, data: dict[str, Any]) -> None:
    for field, value in data.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            continue
        if kind == "json" and field in ("data", "config") and isinstance(value, dict):
            continue
        raise SegmentError(
            "INVALID_FIELD",
            f'message segment "{kind}" field "{field}" must be a scalar value',
            kind,
            field,
        )


# ---------------------------------------------------------------- 解析选项
class Options:
    """解析时的外部依赖（回复定位、@ 的 uid、名片卡片、音乐签名）。"""

    def __init__(
        self,
        *,
        resolve_reply_sequence: Callable[[int], int | None] | None = None,
        resolve_mention_uid: Callable[[int], str] | None = None,
        resolve_contact_ark: Callable[[str, int], str | None] | None = None,
    ) -> None:
        self.resolve_reply_sequence = resolve_reply_sequence
        self.resolve_mention_uid = resolve_mention_uid
        self.resolve_contact_ark = resolve_contact_ark


# ---------------------------------------------------------------- 段 -> 元素
def segment_to_element(kind: Any, data: dict[str, Any], options: Options | None = None) -> dict[str, Any] | None:
    name = normalize_segment_type(kind)
    if name == "reply" and "message" in data:
        raise SegmentError(
            "UNSENDABLE_TYPE",
            'reply.message is only supported inside a forward node',
            name,
            "message",
        )
    if name not in ("node", "anonymous"):
        assert_scalar_data(name, data)

    if name == "node":
        raise SegmentError(
            "UNSENDABLE_TYPE",
            'message segment "node" is only valid inside a forward node list',
            name,
        )
    if name == "share":
        url = _require_non_empty(name, data, "url")
        title = _require_non_empty(name, data, "title")
        return {
            "type": "json",
            "text": json.dumps(
                {
                    "app": "com.tencent.structmsg",
                    "view": "news",
                    "prompt": title,
                    "meta": {"news": {
                        "title": title,
                        "desc": str(data.get("content") or ""),
                        "jumpUrl": url,
                        "preview": str(data.get("image") or ""),
                    }},
                },
                ensure_ascii=False,
            ),
        }
    if name == "music":
        music_type = _require_non_empty(name, data, "type")
        has_id = bool(str(data.get("id") or "").strip())
        custom = music_type == "custom" or (
            not has_id and any(k in data for k in ("url", "audio", "title", "image", "content"))
        )
        if custom:
            for field in ("url", "audio", "title"):
                _require_non_empty(name, data, field)
        elif not has_id:
            raise SegmentError(
                "MISSING_FIELD",
                'message segment "music" requires field "id" for a platform card',
                name,
                "id",
            )
        title = str(data.get("title") or "Music")
        return {
            "type": "json",
            "text": json.dumps(
                {
                    "app": "com.tencent.structmsg",
                    "view": "music",
                    "prompt": f"[音乐]{title}",
                    "meta": {"music": {
                        "title": title,
                        "desc": str(data.get("content") or ""),
                        "jumpUrl": str(data.get("url") or ""),
                        "musicUrl": str(data.get("audio") or ""),
                        "preview": str(data.get("image") or ""),
                    }},
                },
                ensure_ascii=False,
            ),
        }
    if name == "location":
        lat = _coordinate(data, "lat")
        lon = _coordinate(data, "lon")
        title = str(data.get("title") or "位置")
        return {
            "type": "json",
            "text": json.dumps(
                {
                    "app": "com.tencent.map",
                    "view": "LocationShare",
                    "prompt": f"[位置]{title}",
                    "meta": {"Location": {
                        "lat": lat,
                        "lng": lon,
                        "title": title,
                        "address": str(data.get("content") or f"{lat},{lon}"),
                    }},
                },
                ensure_ascii=False,
            ),
        }
    if name == "contact":
        contact_type = _require_non_empty(name, data, "type")
        if contact_type not in ("qq", "group"):
            raise SegmentError(
                "INVALID_FIELD",
                'message segment "contact" field "type" must be "qq" or "group"',
                name,
                "type",
            )
        contact_id = str(data.get("id") or "").strip()
        numeric = int_or(contact_id, 0)
        if numeric <= 0:
            raise SegmentError(
                "INVALID_FIELD",
                'message segment "contact" field "id" must be a positive integer',
                name,
                "id",
            )
        ark = None
        if options and options.resolve_contact_ark:
            ark = options.resolve_contact_ark(contact_type, numeric)
        if ark:
            return {"type": "json", "text": ark}
        return {
            "type": "json",
            "text": json.dumps(
                {
                    "app": "com.tencent.contact.lua",
                    "view": "contact",
                    "prompt": f"[推荐{'群' if contact_type == 'group' else '好友'}]",
                    "meta": {"contact": {"type": contact_type, "id": contact_id}},
                },
                ensure_ascii=False,
            ),
        }
    if name == "rps":
        return {"type": "face", "faceId": 359}
    if name == "dice":
        return {"type": "face", "faceId": 358}
    if name == "shake":
        return {"type": "poke", "subType": 1}
    if name == "anonymous":
        raise SegmentError(
            "UNSENDABLE_TYPE",
            'message segment "anonymous" has no executable send semantics',
            name,
        )

    codec = _CODECS.get(name)
    if codec is not None:
        elem = codec(data, options)
        if elem is None:
            if name == "reply":
                return None
            raise SegmentError(
                "MISSING_FIELD",
                f'message segment "{name}" is missing required or usable fields',
                name,
            )
        return elem
    raise SegmentError("UNKNOWN_TYPE", f"unknown message segment type: {kind}", name)


def _coordinate(data: dict[str, Any], field: str) -> float:
    raw = data.get(field)
    if raw is None or raw == "":
        raise SegmentError(
            "MISSING_FIELD",
            f'message segment "location" field "{field}" is required',
            "location",
            field,
        )
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise SegmentError(
            "INVALID_FIELD",
            f'message segment "location" field "{field}" must be a number',
            "location",
            field,
        ) from None
    if not -180 <= value <= 180:
        raise SegmentError(
            "INVALID_FIELD",
            f'message segment "location" field "{field}" is out of range',
            "location",
            field,
        )
    return value


def _pick_media_source(data: dict[str, Any]) -> str:
    for key in ("url", "file", "path"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    return ""


def _codec_text(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
    text = str(data.get("text") or "")
    return {"type": "text", "text": text} if text else None


def _codec_face(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
    face_id = int_or(data.get("id"), -1)
    if face_id < 0:
        return None
    elem: dict[str, Any] = {"type": "face", "faceId": face_id}
    if data.get("resultId") is not None:
        if isinstance(data["resultId"], str):
            elem["resultId"] = data["resultId"]
        elif isinstance(data["resultId"], int) and data["resultId"] >= 0:
            elem["resultId"] = str(data["resultId"])
        else:
            raise SegmentError("INVALID_FIELD", "face.resultId must be a string or non-negative safe integer", "face", "resultId")
    if data.get("large") is not None:
        value = data["large"]
        if value in (True, "true", 1, "1"):
            elem["large"] = True
        elif value in (False, "false", 0, "0"):
            elem["large"] = False
        else:
            raise SegmentError("INVALID_FIELD", "face.large must be a boolean", "face")
    return elem


def _codec_at(data: dict[str, Any], options: Options | None) -> dict[str, Any] | None:
    qq = str(data.get("qq") or "").strip()
    if qq == "all":
        return {"type": "at", "targetUin": 0, "uid": "all", "text": "@全体成员 "}
    uin = int_or(qq, 0)
    if uin <= 0:
        return None
    name = str(data.get("name") or data.get("nickname") or data.get("card") or "").strip()
    uid = str(data.get("uid") or "").strip()
    if not uid and options and options.resolve_mention_uid:
        uid = (options.resolve_mention_uid(uin) or "").strip()
    elem: dict[str, Any] = {"type": "at", "targetUin": uin}
    if uid:
        elem["uid"] = uid
    if name:
        elem["text"] = f"@{name} "
    return elem


def _codec_reply(data: dict[str, Any], options: Options | None) -> dict[str, Any] | None:
    try:
        reply_id = int_or(data.get("id"), 0)
    except SegmentError:
        return None
    if reply_id == 0:
        return None
    if options and options.resolve_reply_sequence:
        seq = options.resolve_reply_sequence(reply_id)
        if isinstance(seq, int) and seq > 0:
            return {"type": "reply", "replySeq": seq, "replyMessageId": reply_id}
    if reply_id > 0:
        return {"type": "reply", "replySeq": reply_id}
    return None


def _codec_image(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
    emoji_id = str(data.get("emoji_id") or "").strip()
    if emoji_id:
        return {"type": "mface", "emojiId": emoji_id, "text": str(data.get("summary") or "")}
    return {
        "type": "image",
        "url": _pick_media_source(data),
        "flash": data.get("type") == "flash",
        "subType": int_or(data.get("sub_type", data.get("subType")), 0),
        "summary": str(data["summary"]) if data.get("summary") else None,
    }


def _codec_media(kind: str):
    def build(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
        source = _pick_media_source(data)
        if not source:
            return None
        elem: dict[str, Any] = {"type": kind, "url": source}
        if kind == "video" and data.get("thumb"):
            elem["thumbUrl"] = str(data["thumb"])
        return elem

    return build


def _codec_json(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
    raw = data.get("data")
    text = json.dumps(raw, ensure_ascii=False) if isinstance(raw, dict) else raw
    if not isinstance(text, str) or not text.strip():
        raise SegmentError(
            "INVALID_FIELD",
            'message segment "json" field "data" must be a JSON object or non-empty JSON string',
            "json",
            "data",
        )
    return {"type": "json", "text": text}


def _codec_xml(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
    return {
        "type": "xml",
        "text": str(data.get("data") or ""),
        "subType": int_or(data.get("resid", data.get("id")), 0),
    }


def _codec_file(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
    file_id = str(data.get("file_id") or data.get("fileId") or data.get("id") or "").strip()
    source = _pick_media_source(data)
    if not file_id and not source:
        return None
    elem: dict[str, Any] = {"type": "file"}
    if file_id:
        elem["fileId"] = file_id
    else:
        elem["url"] = source
    name = str(data.get("name") or data.get("filename") or data.get("fileName") or "").strip()
    if name:
        elem["fileName"] = name
    size = int_or(data.get("file_size", data.get("size", data.get("fileSize"))), 0)
    if size > 0:
        elem["fileSize"] = size
    for key in ("md5", "sha1", "file_hash"):
        value = str(data.get(key) or "").strip()
        if value:
            elem[key] = value
    return elem


def _codec_mface(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
    emoji_id = str(data.get("emoji_id") or "").strip()
    if not emoji_id:
        return None
    return {"type": "mface", "emojiId": emoji_id, "text": str(data.get("name") or data.get("summary") or "")}


def _codec_poke(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
    if data.get("type") is None and data.get("id") is None:
        return None
    return {"type": "poke", "subType": int_or(data.get("type", data.get("id")), 0)}


def _codec_forward(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
    return {"type": "forward", "resId": str(data.get("id") or data.get("res_id") or data.get("forward_id") or "")}


def _codec_markdown(data: dict[str, Any], _o: Options | None) -> dict[str, Any] | None:
    return {"type": "markdown", "text": str(data.get("content") or "")}


_CODECS: dict[str, Callable[[dict[str, Any], "Options | None"], dict[str, Any] | None]] = {
    "text": _codec_text,
    "face": _codec_face,
    "at": _codec_at,
    "reply": _codec_reply,
    "image": _codec_image,
    "record": _codec_media("record"),
    "video": _codec_media("video"),
    "json": _codec_json,
    "xml": _codec_xml,
    "file": _codec_file,
    "mface": _codec_mface,
    "poke": _codec_poke,
    "forward": _codec_forward,
    "markdown": _codec_markdown,
}


# ---------------------------------------------------------------- 元素 -> 段
def element_to_segment(elem: dict[str, Any]) -> dict[str, Any]:
    kind = str(elem.get("type") or "text")
    if kind == "text":
        return {"type": "text", "data": {"text": elem.get("text") or ""}}
    if kind == "face":
        data: dict[str, Any] = {"id": str(elem.get("faceId") or 0)}
        if elem.get("resultId") is not None:
            data["resultId"] = elem["resultId"]
        return {"type": "face", "data": data}
    if kind == "at":
        qq = "all" if elem.get("uid") == "all" or elem.get("targetUin") == 0 else str(elem.get("targetUin") or 0)
        return {"type": "at", "data": {"qq": qq}}
    if kind == "reply":
        return {"type": "reply", "data": {"id": str(elem.get("replyMessageId") or elem.get("replySeq") or 0)}}
    if kind == "image":
        return {
            "type": "image",
            "data": {
                "url": elem.get("url") or "",
                # OneBot 里 `file` 才是主字段：没有 fileId 时把来源放进去，
                # 免得下游只认 file 的实现拿到一个空串
                "file": elem.get("fileId") or elem.get("url") or "",
                "sub_type": elem.get("subType") or 0,
                "summary": elem.get("summary") or "",
            },
        }
    if kind in ("record", "video"):
        return {"type": kind, "data": {"file": elem.get("fileName") or elem.get("fileId") or "", "url": elem.get("url") or ""}}
    if kind == "json":
        return {"type": "json", "data": {"data": elem.get("text") or ""}}
    if kind == "xml":
        return {"type": "xml", "data": {"data": elem.get("text") or "", "resid": elem.get("subType") or 35}}
    if kind == "file":
        return {
            "type": "file",
            "data": {
                "file": elem.get("fileName") or elem.get("fileId") or elem.get("url") or "",
                "file_id": elem.get("fileId") or "",
                "file_size": elem.get("fileSize") or 0,
                "name": elem.get("fileName") or "",
                "size": elem.get("fileSize") or 0,
                "id": elem.get("fileId") or "",
                "url": elem.get("url") or "",
                "file_hash": elem.get("file_hash") or "",
            },
        }
    if kind == "mface":
        emoji_id = str(elem.get("emojiId") or "")
        directory = emoji_id[:2]
        url = f"https://gxh.vip.qq.com/club/item/parcel/item/{directory}/{emoji_id}/raw300.gif" if emoji_id else ""
        return {
            "type": "image",
            "data": {
                "file": f"{directory}-{emoji_id}.gif" if emoji_id else "",
                "url": url,
                "summary": elem.get("text") or "",
                "sub_type": 0,
                "emoji_id": emoji_id,
            },
        }
    if kind == "poke":
        return {"type": "poke", "data": {"type": elem.get("subType") or 0}}
    if kind == "forward":
        return {"type": "forward", "data": {"id": elem.get("resId") or ""}}
    if kind == "markdown":
        return {"type": "markdown", "data": {"content": elem.get("text") or ""}}
    return {"type": kind, "data": {}}


# ---------------------------------------------------------------- 对外入口
def parse_message(raw: Any, auto_escape: bool = False, options: Options | None = None) -> list[dict[str, Any]]:
    """`message` 参数 -> 元素列表。校验语义与 SnowLuma 一致。"""
    if isinstance(raw, str):
        if not raw:
            raise _missing("message must not be empty")
        if auto_escape:
            return [{"type": "text", "text": raw}]
        elements = parse_from_cq(raw, options)
        if not elements:
            raise _missing("message must contain at least one sendable segment")
        return elements

    if isinstance(raw, list):
        if not is_segment_array(raw):
            raise SegmentError(
                "INVALID_FIELD",
                "message segment array entries must be objects with a non-empty string type",
                None,
                "message",
            )
        if not raw:
            raise _missing("message segment array must not be empty")
        elements: list[dict[str, Any]] = []
        for seg in raw:
            if is_empty_text_placeholder(seg):
                continue
            elem = segment_to_element(seg.get("type"), segment_payload(seg), options)
            if elem is not None:
                elements.append(elem)
        if not elements:
            raise _missing("message must contain at least one sendable segment")
        return elements

    if isinstance(raw, dict):
        if isinstance(raw.get("type"), str) and raw["type"].strip():
            elem = segment_to_element(raw["type"], segment_payload(raw), options)
            if elem is not None:
                return [elem]
            raise _missing("message must contain at least one sendable segment")
        raise SegmentError(
            "MISSING_FIELD",
            "single message segment requires a non-empty string type",
            None,
            "type",
        )

    raise SegmentError(
        "INVALID_FIELD",
        "message must be a string, a segment object, or a segment array",
        None,
        "message",
    )


def to_segments(elements: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [element_to_segment(elem) for elem in elements]


def text_of_elements(elements: Iterable[dict[str, Any]]) -> str:
    return "".join(str(elem.get("text") or "") for elem in elements if elem.get("type") == "text")


def unsupported_elements(elements: Iterable[dict[str, Any]]) -> list[str]:
    """列出底层发不出去的段类型（去重、保序）。"""
    out: list[str] = []
    for elem in elements:
        kind = str(elem.get("type") or "")
        if kind not in SENDABLE and kind not in out:
            out.append(kind)
    return out


# ---------------------------------------------------------------- 兼容旧调用
def text_of(segments: Iterable[dict[str, Any]]) -> str:
    chunks: list[str] = []
    for seg in segments:
        if str(seg.get("type")) == "text":
            chunks.append(str((seg.get("data") or {}).get("text") or ""))
    return "".join(chunks)


def render_cq(segments: Iterable[dict[str, Any]]) -> str:
    return segments_to_raw(segments)


def describe(segments: Iterable[dict[str, Any]]) -> str:
    kinds: list[str] = []
    for seg in segments:
        kind = str(seg.get("type") or "text")
        name = TYPE_NAMES.get(kind, kind)
        if name not in kinds:
            kinds.append(name)
    return "+".join(kinds) or "空消息"
