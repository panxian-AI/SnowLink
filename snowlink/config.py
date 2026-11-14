"""配置：**字段名和目录结构与 SnowLuma 一致**，这样换过来不用改手上的东西。

    config/onebot_<uin>.json    # OneBot 网络（httpServers / wsServers / wsClients / httpClients）
    config/runtime.json         # WebUI 端口等
    config/webui.json           # WebUI 口令（哈希 + salt）
    config/arklink.json         # 本实现新增：底层 ArkLink 接口地址与 token

为什么单独放 `arklink.json`：SnowLuma 的字段一个都不改、不塞私货，
这样把它的 `config/` 目录整个拷过来仍然能读；我们只多读一个自己的文件。
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_HTTP_PORT = 3000
DEFAULT_WS_PORT = 3001
DEFAULT_WEBUI_PORT = 5099
DEFAULT_HOST = "127.0.0.1"


# ---------------------------------------------------------------- 网络配置
@dataclass
class HttpServer:
    """OneBot HTTP 服务端（POST / 动作调用）。字段与 SnowLuma 同名。"""

    name: str = "http-default"
    accessToken: str = ""  # noqa: N815 - 保持 SnowLuma 的字段名
    messageFormat: str = "array"  # noqa: N815
    reportSelfMessage: bool = False  # noqa: N815
    host: str = DEFAULT_HOST
    port: int = DEFAULT_HTTP_PORT
    path: str = "/"
    enableWebSocket: bool = False  # noqa: N815

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HttpServer":
        return cls(
            name=str(data.get("name") or "http-default"),
            accessToken=str(data.get("accessToken") or ""),
            messageFormat=str(data.get("messageFormat") or "array"),
            reportSelfMessage=bool(data.get("reportSelfMessage", False)),
            host=str(data.get("host") or DEFAULT_HOST),
            port=int(data.get("port") or DEFAULT_HTTP_PORT),
            path=str(data.get("path") or "/"),
            enableWebSocket=bool(data.get("enableWebSocket", False)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "accessToken": self.accessToken,
            "messageFormat": self.messageFormat,
            "reportSelfMessage": self.reportSelfMessage,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "enableWebSocket": self.enableWebSocket,
        }


@dataclass
class WsServer:
    """OneBot 正向 WebSocket 服务端（框架连进来）。"""

    name: str = "ws-default"
    accessToken: str = ""  # noqa: N815
    messageFormat: str = "array"  # noqa: N815
    reportSelfMessage: bool = False  # noqa: N815
    host: str = DEFAULT_HOST
    port: int = DEFAULT_WS_PORT
    path: str = "/"
    role: str = "Universal"  # Universal / Api / Event

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WsServer":
        return cls(
            name=str(data.get("name") or "ws-default"),
            accessToken=str(data.get("accessToken") or ""),
            messageFormat=str(data.get("messageFormat") or "array"),
            reportSelfMessage=bool(data.get("reportSelfMessage", False)),
            host=str(data.get("host") or DEFAULT_HOST),
            port=int(data.get("port") or DEFAULT_WS_PORT),
            path=str(data.get("path") or "/"),
            role=str(data.get("role") or "Universal"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "accessToken": self.accessToken,
            "messageFormat": self.messageFormat,
            "reportSelfMessage": self.reportSelfMessage,
            "host": self.host,
            "port": self.port,
            "path": self.path,
            "role": self.role,
        }


@dataclass
class WsClient:
    """反向 WebSocket：我们主动连出去（NoneBot/koishi 的 Universal 模式）。"""

    name: str = "ws-reverse"
    url: str = ""
    accessToken: str = ""  # noqa: N815
    messageFormat: str = "array"  # noqa: N815
    reportSelfMessage: bool = False  # noqa: N815
    reconnectInterval: int = 5  # noqa: N815

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "WsClient":
        return cls(
            name=str(data.get("name") or "ws-reverse"),
            url=str(data.get("url") or ""),
            accessToken=str(data.get("accessToken") or ""),
            messageFormat=str(data.get("messageFormat") or "array"),
            reportSelfMessage=bool(data.get("reportSelfMessage", False)),
            reconnectInterval=int(data.get("reconnectInterval") or 5),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "accessToken": self.accessToken,
            "messageFormat": self.messageFormat,
            "reportSelfMessage": self.reportSelfMessage,
            "reconnectInterval": self.reconnectInterval,
        }


@dataclass
class HttpClient:
    """HTTP 上报：把事件 POST 到你给的上报地址。"""

    name: str = "http-report"
    url: str = ""
    accessToken: str = ""  # noqa: N815
    messageFormat: str = "array"  # noqa: N815
    reportSelfMessage: bool = False  # noqa: N815
    timeout: int = 5

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "HttpClient":
        return cls(
            name=str(data.get("name") or "http-report"),
            url=str(data.get("url") or ""),
            accessToken=str(data.get("accessToken") or ""),
            messageFormat=str(data.get("messageFormat") or "array"),
            reportSelfMessage=bool(data.get("reportSelfMessage", False)),
            timeout=int(data.get("timeout") or 5),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "accessToken": self.accessToken,
            "messageFormat": self.messageFormat,
            "reportSelfMessage": self.reportSelfMessage,
            "timeout": self.timeout,
        }


@dataclass
class OneBotConfig:
    """`config/onebot_<uin>.json` 的完整结构。"""

    mode: str = "snapshot"
    httpServers: list[HttpServer] = field(default_factory=lambda: [HttpServer()])  # noqa: N815
    httpClients: list[HttpClient] = field(default_factory=list)  # noqa: N815
    wsServers: list[WsServer] = field(default_factory=lambda: [WsServer()])  # noqa: N815
    wsClients: list[WsClient] = field(default_factory=list)  # noqa: N815
    statusCommand: dict[str, Any] = field(  # noqa: N815
        default_factory=lambda: {
            "enabled": False,
            "swallow": False,
            "cooldownSeconds": 5,
            "trigger": "#sl",
        }
    )
    historySync: dict[str, Any] = field(default_factory=lambda: {"enabled": False})  # noqa: N815
    notifications: dict[str, Any] = field(default_factory=lambda: {"channelIds": []})
    #: 未识别的字段原样保留：别人手改过的配置不要因为升级就被吃掉
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OneBotConfig":
        networks = data.get("networks") or {}
        known = {
            "mode",
            "networks",
            "statusCommand",
            "historySync",
            "notifications",
        }
        return cls(
            mode=str(data.get("mode") or "snapshot"),
            httpServers=[HttpServer.from_dict(x) for x in networks.get("httpServers") or []],
            httpClients=[HttpClient.from_dict(x) for x in networks.get("httpClients") or []],
            wsServers=[WsServer.from_dict(x) for x in networks.get("wsServers") or []],
            wsClients=[WsClient.from_dict(x) for x in networks.get("wsClients") or []],
            statusCommand=dict(data.get("statusCommand") or {}),
            historySync=dict(data.get("historySync") or {}),
            notifications=dict(data.get("notifications") or {}),
            extras={k: v for k, v in data.items() if k not in known and k != "networks"},
        )

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "mode": self.mode,
            "networks": {
                "httpServers": [x.as_dict() for x in self.httpServers],
                "httpClients": [x.as_dict() for x in self.httpClients],
                "wsServers": [x.as_dict() for x in self.wsServers],
                "wsClients": [x.as_dict() for x in self.wsClients],
            },
            "statusCommand": self.statusCommand,
            "historySync": self.historySync,
            "notifications": self.notifications,
        }
        out.update(self.extras)
        return out


@dataclass
class RuntimeConfig:
    """`config/runtime.json`：WebUI 相关。字段与 SnowLuma 同名。"""

    webuiPort: int = DEFAULT_WEBUI_PORT  # noqa: N815
    webuiHost: str = DEFAULT_HOST  # noqa: N815
    hookAutoLoad: bool = False  # noqa: N815
    trustProxy: str = ""  # noqa: N815
    logMaxTotalMb: int = 1024  # noqa: N815
    logRetainDays: int = 7  # noqa: N815
    logPerUin: bool = False  # noqa: N815
    #: get_version_info 里上报的实现名。默认如实报 SnowLink；
    #: 需要严格顶替 SnowLuma（有些框架按 app_name 分支）时改成 "SnowLuma"
    appName: str = "SnowLink"  # noqa: N815

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RuntimeConfig":
        return cls(
            webuiPort=int(data.get("webuiPort") or DEFAULT_WEBUI_PORT),
            webuiHost=str(data.get("webuiHost") or DEFAULT_HOST),
            hookAutoLoad=bool(data.get("hookAutoLoad", False)),
            trustProxy=str(data.get("trustProxy") or ""),
            logMaxTotalMb=int(data.get("logMaxTotalMb") or 1024),
            logRetainDays=int(data.get("logRetainDays") or 7),
            logPerUin=bool(data.get("logPerUin", False)),
            appName=str(data.get("appName") or "SnowLink"),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "webuiPort": self.webuiPort,
            "hookAutoLoad": self.hookAutoLoad,
            "webuiHost": self.webuiHost,
            "trustProxy": self.trustProxy,
            "logMaxTotalMb": self.logMaxTotalMb,
            "logRetainDays": self.logRetainDays,
            "logPerUin": self.logPerUin,
        }


@dataclass
class BackendConfig:
    """`config/arklink.json`：底层 ArkLink 接口。

    这一层就是「用的是我的 bot」：动作全部落到 ArkLink 的发送链路，
    事件全部来自 ArkLink 的消息总线；本进程不碰 QQ 进程。
    """

    baseUrl: str = "http://127.0.0.1:6900"  # noqa: N815
    token: str = ""
    uin: str = ""
    timeout: int = 20
    #: 没有 ArkLink 时用假后端（把发出去的消息回显成事件），方便先跑通链路
    offline: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BackendConfig":
        return cls(
            baseUrl=str(data.get("baseUrl") or data.get("base_url") or "http://127.0.0.1:6900"),
            token=str(data.get("token") or ""),
            uin=str(data.get("uin") or ""),
            timeout=int(data.get("timeout") or 20),
            offline=bool(data.get("offline", False)),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "baseUrl": self.baseUrl,
            "token": self.token,
            "uin": self.uin,
            "timeout": self.timeout,
            "offline": self.offline,
        }


# ---------------------------------------------------------------- 读写
def config_dir(override: str | os.PathLike[str] | None = None) -> Path:
    if override:
        return Path(override).expanduser()
    env = os.environ.get("SNOWLINK_CONFIG")
    if env:
        return Path(env).expanduser()
    return Path("config")


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def onebot_path(directory: Path, uin: str) -> Path:
    return directory / f"onebot_{uin}.json"


def find_uins(directory: Path) -> list[str]:
    """扫 `config/onebot_*.json`：多账号各自一份，与 SnowLuma 一致。"""
    out: list[str] = []
    if not directory.is_dir():
        return out
    for path in sorted(directory.glob("onebot_*.json")):
        stem = path.stem[len("onebot_") :]
        if stem and stem.isdigit():
            out.append(stem)
    return out


def load_onebot(directory: Path, uin: str) -> OneBotConfig:
    path = onebot_path(directory, uin)
    data = _read_json(path)
    if not data:
        cfg = OneBotConfig()
        save_onebot(directory, uin, cfg)
        return cfg
    cfg = OneBotConfig.from_dict(data)
    # 空配置里一个网络都没有时给一套默认，避免「跑起来什么都不听」
    if not cfg.httpServers and not cfg.wsServers and not cfg.wsClients and not cfg.httpClients:
        cfg = OneBotConfig()
    return cfg


def save_onebot(directory: Path, uin: str, cfg: OneBotConfig) -> Path:
    path = onebot_path(directory, uin)
    _write_json(path, cfg.as_dict())
    return path


def load_runtime(directory: Path) -> RuntimeConfig:
    data = _read_json(directory / "runtime.json")
    if not data:
        cfg = RuntimeConfig()
        _write_json(directory / "runtime.json", cfg.as_dict())
        return cfg
    return RuntimeConfig.from_dict(data)


def save_runtime(directory: Path, cfg: RuntimeConfig) -> Path:
    path = directory / "runtime.json"
    _write_json(path, cfg.as_dict())
    return path


def load_backend(directory: Path) -> BackendConfig:
    data = _read_json(directory / "arklink.json")
    env_url = os.environ.get("ARKLINK_BASE_URL")
    env_token = os.environ.get("ARKLINK_API_TOKEN")
    cfg = BackendConfig.from_dict(data) if data else BackendConfig()
    if env_url:
        cfg.baseUrl = env_url
    if env_token:
        cfg.token = env_token
    return cfg


def save_backend(directory: Path, cfg: BackendConfig) -> Path:
    path = directory / "arklink.json"
    _write_json(path, cfg.as_dict())
    return path


def new_token(n: int = 32) -> str:
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    return "".join(secrets.choice(alphabet) for _ in range(n))
