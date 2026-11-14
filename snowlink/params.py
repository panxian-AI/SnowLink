"""动作参数校验。

SnowLuma 的每个动作都有参数 schema，校验失败时返回：

    {"status": "failed", "retcode": 1400, "data": null, "wording": "group_id: is required"}

wording 的形如 `字段名: 原因`，原因文案是固定的几种（`is required` /
`expected a positive integer` / `must be >= 1` …）。上层框架和人都靠这句定位问题，
所以这里照抄文案，而不是自己另写一套更"友好"的措辞。

数值字段跟它一样宽容：数字或数字字符串都收（`"123"` → 123），布尔字段收
`true/1/yes/on` 与 `false/0/no/off`——表单提交、查询串参数都能直接用。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

from . import segments as seg_mod
from .errors import BadRequest

MISSING = object()


class Field:
    """一个参数的定义：怎么转、缺了怎么办。"""

    def __init__(self, coerce: Callable[[Any], Any], *, required: bool = True,
                 default: Any = MISSING, doc: str = "") -> None:
        self.coerce = coerce
        self.required = required
        self.default = default
        self.doc = doc

    def parse(self, raw: Any, name: str) -> Any:
        if raw is MISSING:
            if self.required:
                raise BadRequest(f"{name}: is required")
            return None if self.default is MISSING else self.default
        try:
            return self.coerce(raw)
        except BadRequest as exc:
            # 内层只给原因，字段名在这里统一拼上（与 `wording` 的顺序一致）
            raise BadRequest(f"{name}: {exc}") from None

    def optional(self) -> "Field":
        return Field(self.coerce, required=False, default=MISSING)


# ---------------------------------------------------------------- 基础类型
def integer(type_name: str = "an integer", *, minimum: int | None = None,
            maximum: int | None = None, non_zero: bool = False) -> Field:
    def coerce(raw: Any) -> Any:
        if isinstance(raw, bool):
            raise BadRequest(f"expected {type_name}")
        if isinstance(raw, (int, float)):
            if isinstance(raw, float) and not raw.is_integer():
                raise BadRequest(f"expected {type_name}")
            value = int(raw)
        elif isinstance(raw, str) and raw.strip() and _is_number(raw.strip()):
            value = int(float(raw.strip()))
        else:
            raise BadRequest(f"expected {type_name}")
        if non_zero and value == 0:
            raise BadRequest("must not be 0")
        if minimum is not None and value < minimum:
            raise BadRequest(f"must be >= {minimum}")
        if maximum is not None and value > maximum:
            raise BadRequest(f"must be <= {maximum}")
        return value

    return Field(coerce)


def _is_number(text: str) -> bool:
    try:
        float(text)
    except ValueError:
        return False
    return True


def uint() -> Field:
    """正整数（QQ 号、群号）。"""
    return integer("a positive integer", minimum=1)


def message_id() -> Field:
    return integer("a message id", non_zero=True)


def boolean(default: bool = False) -> Field:
    def coerce(raw: Any) -> Any:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return raw != 0
        if isinstance(raw, str):
            text = raw.strip().lower()
            if text in ("true", "1", "yes", "on"):
                return True
            if text in ("false", "0", "no", "off"):
                return False
        raise BadRequest("expected a boolean")

    return Field(coerce, required=False, default=default)


def text() -> Field:
    def coerce(raw: Any) -> Any:
        if isinstance(raw, str):
            return raw
        raise BadRequest("expected a string")

    return Field(coerce)


def optional_text() -> Field:
    return text().optional()


def enum(*values: Any) -> Field:
    def coerce(raw: Any) -> Any:
        if isinstance(raw, (str, int)) and raw in values:
            return raw
        raise BadRequest(f"expected one of: {', '.join(str(v) for v in values)}")

    return Field(coerce)


def message() -> Field:
    """OneBot 的 message 参数：字符串 / 段数组 / 单个段对象。

    字符串若长相是 JSON 数组（表单提交时常见）就先按数组解析——这条也与
    SnowLuma 的 `message()` 字段一致。
    """

    def coerce(raw: Any) -> Any:
        if isinstance(raw, str):
            probe = raw.strip()
            if probe.startswith("[") and probe.endswith("]"):
                try:
                    parsed = json.loads(probe)
                except ValueError:
                    return raw
                if isinstance(parsed, list):
                    return parsed
        return raw

    return Field(coerce)


# ---------------------------------------------------------------- 规则
class Rule:
    def __init__(self, doc: str, check: Callable[[dict[str, Any]], bool]) -> None:
        self.doc = doc
        self.check = check


def at_least_one_of(*keys: str) -> Rule:
    return Rule(
        f"at least one of: {', '.join(keys)}",
        lambda params: any(k in params and params[k] is not None for k in keys),
    )


def exactly_one_of(*groups: Any) -> Rule:
    def label(group: Any) -> str:
        return f"({'+'.join(group)})" if isinstance(group, (list, tuple)) else str(group)

    doc = "exactly one of: " + " | ".join(label(g) for g in groups)

    def check(params: dict[str, Any]) -> bool:
        def ok(group: Any) -> bool:
            keys = group if isinstance(group, (list, tuple)) else (group,)
            return all(k in params and params[k] is not None for k in keys)

        return sum(1 for g in groups if ok(g)) == 1

    return Rule(doc, check)


def required_together(*keys: str) -> Rule:
    def check(params: dict[str, Any]) -> bool:
        present = [k for k in keys if k in params and params[k] is not None]
        return not present or len(present) == len(keys)

    return Rule(f"all or none of: {', '.join(keys)}", check)


def mutually_exclusive(*keys: str) -> Rule:
    def check(params: dict[str, Any]) -> bool:
        return sum(1 for k in keys if k in params and params[k] is not None) <= 1

    return Rule(f"at most one of: {', '.join(keys)}", check)


# ---------------------------------------------------------------- 执行
def parse_params(spec: dict[str, Field], raw: dict[str, Any],
                 rules: Iterable[Rule] = ()) -> dict[str, Any]:
    """按 spec 转换；任何一项不合法就抛 `BadRequest`（1400）。"""
    out: dict[str, Any] = {}
    for name, field in spec.items():
        out[name] = field.parse(raw.get(name, MISSING), name)
    for rule in rules:
        if not rule.check(out):
            raise BadRequest(rule.doc)
    return out


def only_declared(spec: dict[str, Field], raw: dict[str, Any]) -> dict[str, Any]:
    """丢掉未声明的字段，避免把 action/echo 之类的残留当参数传下去。"""
    return {k: v for k, v in raw.items() if k in spec}


# ---------------------------------------------------------------- 消息便捷
def parse_message_arg(value: Any, auto_escape: bool = False,
                      options: seg_mod.Options | None = None) -> list[dict[str, Any]]:
    """把 message 参数解析成元素；段校验失败转成 1400。"""
    try:
        return seg_mod.parse_message(value, auto_escape, options)
    except seg_mod.SegmentError as exc:
        raise BadRequest(exc.message) from None
