"""命令行入口：`snowlink run / init / check / version`。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from . import __version__
from . import config as cfg_mod
from .backend import ArkLinkBackend, BackendError, DemoBackend
from .gateway import Gateway
from .webui import WebUI


def _say(text: str = "") -> None:
    print(text, flush=True)


def _print_banner(uin: str, cfg: cfg_mod.OneBotConfig, runtime: cfg_mod.RuntimeConfig,
                  password: str, offline: bool) -> None:
    _say("")
    _say(f"  SnowLink v{__version__} · OneBot v11 协议端（底层 ArkLink）")
    _say(f"  账号：{uin or '（未指定 uin，登录信息取 ArkLink 的 /self）'}")
    for item in cfg.httpServers:
        _say(f"  HTTP ：http://{item.host}:{item.port}{item.path}")
    for item in cfg.wsServers:
        token = f"?access_token={item.accessToken}" if item.accessToken else ""
        _say(f"  WS   ：ws://{item.host}:{item.port}{item.path}{token}")
    for item in cfg.wsClients:
        _say(f"  反向 WS：{item.url}")
    for item in cfg.httpClients:
        _say(f"  上报  ：{item.url}")
    _say(f"  WebUI：http://{runtime.webuiHost}:{runtime.webuiPort}")
    if offline:
        _say("  后端  ：离线演示（消息会回显成事件，不会真的发到 QQ）")
    if password:
        _say("")
        _say(f"  首次启动口令（只显示这一次）：{password}")
        _say("  改了忘不掉：删掉 config/webui.json 会重新生成并再次打印。")
    _say("")


# ---------------------------------------------------------------- run
def _run(args: argparse.Namespace) -> int:
    directory = cfg_mod.config_dir(args.config)
    uin = str(args.uin or "")

    if not uin:
        found = cfg_mod.find_uins(directory)
        if found:
            uin = found[0]
            if len(found) > 1:
                _say(f"发现多个账号配置：{', '.join(found)}，本次用 {uin}（--uin 可指定）")
        else:
            backend_probe = cfg_mod.load_backend(directory)
            uin = backend_probe.uin or ""

    cfg = cfg_mod.load_onebot(directory, uin or "default")
    runtime = cfg_mod.load_runtime(directory)
    backend_cfg = cfg_mod.load_backend(directory)
    if args.offline:
        backend_cfg.offline = True
    if args.base_url:
        backend_cfg.baseUrl = args.base_url
    if args.token:
        backend_cfg.token = args.token
    if args.message_format:
        for item in cfg.httpServers + cfg.wsServers:
            item.messageFormat = args.message_format

    gateway = Gateway(cfg, backend_cfg, uin=uin, log=print, app_name=runtime.appName)
    try:
        gateway.start()
    except OSError as exc:
        _say(f"起服务失败：{exc}")
        return 2

    password = ""
    webui_server = None
    if not args.no_webui:
        webui = WebUI(gateway, directory, runtime, uin=uin)
        password = webui.initial_password
        try:
            webui_server = webui.build()
        except OSError as exc:
            _say(f"WebUI 端口 {runtime.webuiPort} 起不来：{exc}")
            webui_server = None

    if webui_server is not None:
        import threading

        threading.Thread(target=webui_server.serve_forever, daemon=True, name="webui").start()

    _print_banner(uin, cfg, runtime, password, bool(backend_cfg.offline))
    _say("按 Ctrl+C 停止。")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        _say("\n已停止")
    finally:
        gateway.stop()
        if webui_server is not None:
            try:
                webui_server.shutdown()
                webui_server.server_close()
            except Exception:
                pass
    return 0


# ---------------------------------------------------------------- init
def _init(args: argparse.Namespace) -> int:
    directory = cfg_mod.config_dir(args.config)
    uin = str(args.uin or "")
    if not uin:
        _say("需要 --uin：配置文件名是 onebot_<QQ号>.json（和 SnowLuma 一致）")
        return 2
    path = cfg_mod.onebot_path(directory, uin)
    if path.exists() and not args.force:
        _say(f"{path} 已存在，没动它（要覆盖加 --force）")
        return 0
    cfg = cfg_mod.OneBotConfig()
    token = cfg_mod.new_token()
    for item in cfg.httpServers:
        item.accessToken = cfg_mod.new_token()
    for item in cfg.wsServers:
        item.accessToken = token
    cfg_mod.save_onebot(directory, uin, cfg)
    cfg_mod.save_runtime(directory, cfg_mod.load_runtime(directory))
    backend = cfg_mod.load_backend(directory)
    if not backend.uin:
        backend.uin = uin
        cfg_mod.save_backend(directory, backend)
    _say(f"已生成 {path}")
    _say(f"HTTP 服务端 3000 / WS 服务端 3001（token：{token}）")
    _say("")
    _say("下一步：")
    _say("  1) 起 ArkLink 接口：arklink serve api   （默认 127.0.0.1:6900）")
    _say("  2) 填 config/arklink.json 的 token：arklink env token")
    _say("  3) 跑：snowlink run")
    return 0


# ---------------------------------------------------------------- check
def _check(args: argparse.Namespace) -> int:
    directory = cfg_mod.config_dir(args.config)
    uin = str(args.uin or "")
    found = cfg_mod.find_uins(directory)
    if not uin and found:
        uin = found[0]
    ok = True

    _say("SnowLink 体检")
    _say(f"  配置目录：{directory.resolve()}")

    if uin:
        _say(f"  ✅ 账号配置：onebot_{uin}.json")
    else:
        ok = False
        _say("  ❌ 没有账号配置（先 `snowlink init --uin <QQ号>`）")

    cfg = cfg_mod.load_onebot(directory, uin or "default")
    runtime = cfg_mod.load_runtime(directory)
    backend_cfg = cfg_mod.load_backend(directory)
    if args.offline:
        backend_cfg.offline = True

    if cfg.httpServers:
        item = cfg.httpServers[0]
        _say(f"  ✅ HTTP 服务端：{item.host}:{item.port}{item.path}"
             + ("（有 accessToken）" if item.accessToken else "（没有 accessToken，建议配上）"))
    else:
        _say("  ⚠️ 没有 httpServers：只靠 WS 也能用，但很多框架默认走 HTTP")

    if cfg.wsServers:
        item = cfg.wsServers[0]
        _say(f"  ✅ WS 服务端：ws://{item.host}:{item.port}{item.path}"
             + ("（有 accessToken）" if item.accessToken else "（没有 accessToken）"))
    else:
        _say("  ⚠️ 没有 wsServers")

    _say(f"  ✅ WebUI：http://{runtime.webuiHost}:{runtime.webuiPort}")

    if backend_cfg.offline:
        _say("  ⚠️ 后端：离线演示模式（不会真的发到 QQ）")
        return 0 if ok else 1

    if not backend_cfg.token:
        ok = False
        _say("  ❌ config/arklink.json 没有 token：跑 `arklink env token` 取，或设环境变量 ARKLINK_API_TOKEN")

    backend = ArkLinkBackend(backend_cfg.baseUrl, backend_cfg.token, timeout=5)
    try:
        health = backend.health()
        _say(f"  ✅ ArkLink：{backend_cfg.baseUrl} 健康（{json.dumps(health, ensure_ascii=False)[:80]}）")
    except BackendError as exc:
        ok = False
        _say(f"  ❌ {exc.msg}")
        return 1

    try:
        info = backend.self_info()
        _say(f"  ✅ 登录账号：{info.get('uin') or info.get('user_id') or '?'}"
             f" / {info.get('nickname') or info.get('name') or ''}")
    except BackendError as exc:
        _say(f"  ⚠️ 拿不到自身信息：{exc.msg}")

    try:
        caps = backend.capabilities()
        items = caps.get("items") or {}
        yes = [k for k, v in items.items() if v is True]
        no = [k for k, v in items.items() if v is False]
        _say(f"  ✅ 能力：能做 {len(yes)} 项，做不到 {len(no)} 项"
             f"（做不到的会返回 1404，不假装成功）")
    except BackendError as exc:
        _say(f"  ⚠️ 拿不到能力矩阵：{exc.msg}")

    _say("")
    _say("结论：" + ("可以直接 `snowlink run`" if ok else "先把上面 ❌ 的修好"))
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="snowlink",
        description="SnowLink：占 SnowLuma 的位置，底层走 ArkLink 的 OneBot v11 协议端",
    )
    parser.add_argument("-v", "--version", action="version", version=f"SnowLink {__version__}")
    sub = parser.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="启动协议端（默认命令）")
    run.add_argument("--config", default="", help="配置目录（默认 ./config）")
    run.add_argument("--uin", default="", help="账号 QQ 号（对应 config/onebot_<uin>.json）")
    run.add_argument("--offline", action="store_true", help="离线演示：不连 ArkLink，不发真消息")
    run.add_argument("--no-webui", action="store_true", help="不起 WebUI（5099）")
    run.add_argument("--base-url", default="", help="覆盖 ArkLink 地址，如 http://127.0.0.1:6900")
    run.add_argument("--token", default="", help="覆盖 ArkLink token")
    run.add_argument("--message-format", choices=["array", "string"], default="",
                     help="覆盖消息格式（默认沿用配置里的 messageFormat）")

    init = sub.add_parser("init", help="生成一套默认配置")
    init.add_argument("--config", default="")
    init.add_argument("--uin", default="")
    init.add_argument("--force", action="store_true", help="覆盖已有配置")

    check = sub.add_parser("check", help="体检：配置 / ArkLink / 能力")
    check.add_argument("--config", default="")
    check.add_argument("--uin", default="")
    check.add_argument("--offline", action="store_true")

    sub.add_parser("version", help="打印版本")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cmd = args.cmd or "run"
    if cmd == "version":
        _say(f"SnowLink {__version__}")
        return 0
    if cmd == "init":
        return _init(args)
    if cmd == "check":
        return _check(args)
    if not hasattr(args, "offline"):
        args.offline = False
    if not hasattr(args, "no_webui"):
        args.no_webui = False
    for attr, default in (("config", ""), ("uin", ""), ("base_url", ""), ("token", ""),
                          ("message_format", "")):
        if not hasattr(args, attr):
            setattr(args, attr, default)
    return _run(args)


if __name__ == "__main__":
    sys.exit(main())
