"""配置：能读懂 SnowLuma 的 onebot_<uin>.json，并且不丢它不认识的字段。"""

from __future__ import annotations

import json

from snowlink import config as cfg

#: 从 SnowLuma 发行包里抄来的结构（值已替换成示例）
SNOWLUMA_STYLE = {
    "mode": "snapshot",
    "networks": {
        "httpServers": [{
            "name": "http-default",
            "accessToken": "http-token",
            "messageFormat": "array",
            "reportSelfMessage": False,
            "host": "127.0.0.1",
            "port": 3000,
            "path": "/",
            "enableWebSocket": False,
        }],
        "httpClients": [],
        "wsServers": [{
            "name": "ws-default",
            "accessToken": "ws-token",
            "messageFormat": "array",
            "reportSelfMessage": False,
            "host": "127.0.0.1",
            "port": 3001,
            "path": "/",
            "role": "Universal",
        }],
        "wsClients": [],
    },
    "statusCommand": {"enabled": False, "swallow": False, "cooldownSeconds": 5, "trigger": "#sl"},
    "historySync": {"enabled": False},
    "notifications": {"channelIds": []},
    "someFutureField": {"keep": "me"},
}


def test_parse_snowluma_config() -> None:
    cfg_obj = cfg.OneBotConfig.from_dict(SNOWLUMA_STYLE)
    assert cfg_obj.mode == "snapshot"
    assert cfg_obj.httpServers[0].port == 3000
    assert cfg_obj.wsServers[0].port == 3001
    assert cfg_obj.wsServers[0].role == "Universal"
    assert cfg_obj.wsServers[0].accessToken == "ws-token"


def test_unknown_fields_survive_roundtrip() -> None:
    cfg_obj = cfg.OneBotConfig.from_dict(SNOWLUMA_STYLE)
    out = cfg_obj.as_dict()
    assert out["someFutureField"] == {"keep": "me"}
    assert out["networks"]["httpServers"][0]["accessToken"] == "http-token"


def test_roundtrip_equals() -> None:
    cfg_obj = cfg.OneBotConfig.from_dict(SNOWLUMA_STYLE)
    again = cfg.OneBotConfig.from_dict(cfg_obj.as_dict())
    assert again.as_dict() == cfg_obj.as_dict()


def test_empty_networks_falls_back_to_defaults(tmp_path) -> None:
    (tmp_path / "onebot_10001.json").write_text(
        json.dumps({"mode": "snapshot", "networks": {}}), encoding="utf-8"
    )
    loaded = cfg.load_onebot(tmp_path, "10001")
    # 一个入口都没有的配置跑起来等于「什么都不听」，给一套默认更有用
    assert loaded.httpServers and loaded.wsServers


def test_find_uins(tmp_path) -> None:
    cfg.save_onebot(tmp_path, "10001", cfg.OneBotConfig())
    cfg.save_onebot(tmp_path, "10002", cfg.OneBotConfig())
    (tmp_path / "onebot_notanumber.json").write_text("{}", encoding="utf-8")
    assert cfg.find_uins(tmp_path) == ["10001", "10002"]


def test_runtime_defaults_and_roundtrip(tmp_path) -> None:
    runtime = cfg.load_runtime(tmp_path)
    assert runtime.webuiPort == 5099
    runtime.webuiPort = 5199
    cfg.save_runtime(tmp_path, runtime)
    assert cfg.load_runtime(tmp_path).webuiPort == 5199


def test_backend_env_override(tmp_path, monkeypatch) -> None:
    cfg.save_backend(tmp_path, cfg.BackendConfig(token="from-file"))
    monkeypatch.setenv("ARKLINK_API_TOKEN", "from-env")
    monkeypatch.setenv("ARKLINK_BASE_URL", "http://10.0.0.9:6900")
    backend = cfg.load_backend(tmp_path)
    assert backend.token == "from-env"
    assert backend.baseUrl == "http://10.0.0.9:6900"


def test_new_token_shape() -> None:
    token = cfg.new_token(16)
    assert len(token) == 16 and token.isalnum()
