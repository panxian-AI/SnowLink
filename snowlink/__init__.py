"""SnowLink —— 顶在 SnowLuma 的位置上，底层换成 ArkLink。

对外：OneBot v11（HTTP / WS / 反向 WS / HTTP 上报 / WebUI），
配置字段与端口跟 SnowLuma 对齐，框架侧不用改一行。

对内：所有动作落到 ArkLink 的发送链路，所有事件来自 ArkLink 的消息总线，
本进程**不注入 QQ、不改协议**——QQ 由 ArkLink 那边的官方客户端自己说自己的话。
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__"]
