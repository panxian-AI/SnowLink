"""错误类型与 retcode。

retcode 取值与 SnowLuma 的枚举完全一致，上层框架（NoneBot / Koishi / 自研）通常
只在日志里看它，但换实现时不能让数值含义变掉：

    0      成功
    100    动作执行失败（业务层失败，比如消息不存在）
    1200   内部错误
    1400   参数不合法（含消息段校验失败）
    1404   不认识的动作

`ActionError` 自带 retcode 与 wording，分发层直接用它拼响应。
"""

from __future__ import annotations

RET_OK = 0
RET_ACTION_FAILED = 100
RET_INTERNAL = 1200
RET_BAD_REQUEST = 1400
RET_UNKNOWN_ACTION = 1404


class ActionError(Exception):
    """动作层面的失败：`str(exc)` 就是回应给上层的 `wording`。"""

    def __init__(self, wording: str, retcode: int = RET_ACTION_FAILED) -> None:
        super().__init__(wording)
        self.wording = wording
        self.retcode = retcode


class BadRequest(ActionError):
    """参数不合法：默认 1400。"""

    def __init__(self, wording: str) -> None:
        super().__init__(wording, RET_BAD_REQUEST)


class SegmentError(Exception):
    """消息段校验失败。

    与 SnowLuma 一样带 `code`（MISSING_FIELD / INVALID_FIELD / UNSENDABLE_TYPE /
    UNKNOWN_TYPE）、出问题的段类型与字段名；上层看到的是 1400 + 原文案。
    """

    def __init__(self, code: str, message: str, element_type: str | None = None,
                 field: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.element_type = element_type
        self.field = field

    def __str__(self) -> str:  # pragma: no cover - 只为日志可读
        return self.message
