# SnowLink

> **顶在 SnowLuma 的位置上，底层换成 ArkLink。**
> 对外还是 OneBot v11（HTTP / WS / 反向 WS / 上报 / WebUI 5099），
> 配置字段和端口跟 SnowLuma 一样；对内不注入 QQ、不改协议，
> 消息由 ArkLink 那边的官方客户端自己产生。

```
QQ 客户端（官方，登录着）  ← UI 通道 →  ArkLink（:6900）
                                          │  HTTP + SSE
                                          ▼
                                    SnowLink（:3000 / :3001 / :5099）
                                          │  OneBot v11
                                          ▼
                          NoneBot2 / Koishi / AstrBot / 你自己的框架
```

## 为什么有这个

SnowLuma 这类「第三方协议端」的仓库正在一个个消失，但机器人框架那一侧什么都没变：
它们要的还是 OneBot v11。所以缺的不是又一个协议实现，而是**一个能站在同一个位置上、
用另一种方式拿到消息的替身**。

SnowLink 的选择是：不碰 QQ 进程里的协议，借用 ArkLink 的官方客户端通道
（Xvfb 里挂着的那个 QQ，界面怎么发它就怎么发），把结果翻译成 OneBot v11。

| | SnowLuma | SnowLink |
|---|---|---|
| 消息从哪来 | 注入 QQ 进程，解析私有协议 | ArkLink 操作已登录的官方客户端 |
| QQ 更新协议 | 可能要重做注入层 | 不受影响（走界面） |
| 对外接口 | OneBot v11（213 个动作） | OneBot v11（213 个动作**全部注册**，同端口、同配置字段、同 retcode） |
| 协议细节 | 它定的 | **逐条对齐**：段解析、参数文案、message_id 哈希、WS 角色与心跳、上报签名与 quick operation |
| 部署 | Windows / Docker | Windows / Linux / 小服务器（`arklink` 的部署方式） |
| 富媒体 | 图片、语音、视频、卡片 | **图片和文件可以**（把本地文件粘进剪贴板），语音条/视频消息/卡片不行 |

> 能力差异不藏着：213 个动作全部登记，会做的 17 个真做，其余 196 个返回
> `retcode: 1404` + 一句「为什么不行」+ 缺什么，**不会假装成功**，
> 也不会把图片降级成 `[图片]` 发出去。
> 具体见 [docs/兼容性.md](docs/兼容性.md)。

## 三步跑起来

```bash
# 1) 起 ArkLink 的接口（QQ 已登录的前提下）
arklink serve api                       # 默认 127.0.0.1:6900

# 2) 生成配置（文件名与 SnowLuma 一致：config/onebot_<QQ号>.json）
python -m snowlink init --uin 你的QQ号
#    把 token 填进 config/arklink.json：arklink env token

# 3) 体检 + 启动
python -m snowlink check
python -m snowlink run
```

启动后：

```
HTTP ：http://127.0.0.1:3000       WS：ws://127.0.0.1:3001
WebUI：http://127.0.0.1:5099       首次口令打印在控制台
```

没有 QQ 也想先跑通？`python -m snowlink run --offline`：发送会回显成一条收到的事件，
框架侧的配置对不对一眼就能看出来。

## 配置（和 SnowLuma 同构）

`config/onebot_<uin>.json` 的字段一个没改，把 SnowLuma 的配置拷过来能直接读：

```json
{
  "mode": "snapshot",
  "networks": {
    "httpServers": [{"name": "http-default", "accessToken": "...", "messageFormat": "array",
                     "host": "127.0.0.1", "port": 3000, "path": "/", "enableWebSocket": false}],
    "wsServers":   [{"name": "ws-default", "accessToken": "...", "messageFormat": "array",
                     "host": "127.0.0.1", "port": 3001, "path": "/", "role": "Universal"}],
    "wsClients":   [],
    "httpClients": []
  }
}
```

多出来的只有一个 `config/arklink.json`（本实现的底层连接）：

```json
{"baseUrl": "http://127.0.0.1:6900", "token": "从 arklink env token 取", "uin": "你的QQ号"}
```

字段细节、多账号、反向 WS 示例见 [docs/配置.md](docs/配置.md)。

## 接框架

- **反向 WS（推荐）**：`networks.wsClients` 填 `ws://框架:端口/onebot/v11/ws`，连上就推事件。
- **正向 WS**：框架连 `ws://127.0.0.1:3001/`，`Authorization: Bearer <wsServers[0].accessToken>`。
- **HTTP**：`POST http://127.0.0.1:3000/`，body `{"action":"send_group_msg","params":{...}}`。
- **HTTP 上报**：`networks.httpClients` 填你的地址，事件会 POST 过去。

鉴权支持三种写法：`Authorization: Bearer <token>`、`?access_token=<token>`、
`Sec-WebSocket-Protocol: <token>`（OneBot v11 的三种常见约定）。

几个和上游一致的细节，接入时能少踩坑：

- **WS 角色由路径决定**：`/api` → 只收动作，`/event` → 只推事件，其余 → 两样都行。
  连 `/onebot/v11/ws` 拿到的是「两样都行」。
- **连上先收三帧**：`lifecycle(connect)`、`lifecycle(enable)`、`heartbeat`；
  之后每 30 秒一个 `heartbeat`，断开时补一个 `lifecycle(disable)`。
- **`message_id` 是 int32 哈希，可以是负数**，别拿它当自增序号用。
- **HTTP 上报带签名**：`X-Self-ID` + `X-Signature: sha1=<hmac>`（配了 token 时），
  响应体回 JSON 会被当作 [quick operation](docs/兼容性.md#http-上报) 执行。

## WebUI

`http://127.0.0.1:5099` —— 状态、网络、发一条、动作调试、最近事件、日志、配置编辑。
口令存在 `config/webui.json`（sha512 + salt）；首启随机生成并打印一次，
忘了就删掉 `config/webui.json` 重新生成。

## 部署到服务器

SnowLink 本身是纯 Python（无第三方依赖），跟 ArkLink 一起装到小服务器上即可：

```bash
sudo bash ArkLink/tools/setup_server.sh --source /path/to/ArkLink --qq-deb /tmp/QQ_xxx.deb
# 然后给 SnowLink 配一个 systemd 服务：
#   ExecStart=/usr/bin/python3 -m snowlink run --config /opt/snowlink/config
```

QQ 跑在服务器的无头虚拟屏里，SnowLink 只连 ArkLink 的 127.0.0.1:6900，不需要额外开端口。

## 边界与风险

- SnowLink 是**独立实现**，不使用 SnowLuma 的代码与二进制，也不代表它；两者只是接口位置相同。
- OneBot v11 是公开协议；QQ 一侧没有官方授权，账号风控风险始终存在，请自行判断。
- 仅建议用于个人学习与自建场景；商业使用前先确认你自己承担的合规责任。

## 开发

```bash
python -m pytest -q        # 99 项：段编解码 / 参数文案 / 动作面 / 事件形状 / 四个出口 / 契约
```

`tests/test_contract.py` 会拿真实的动作面做交叉校验（213 条是否齐全、每条是否都能给出可读原因、
成功信封的键集合），所以改动作面时测试会先红。

MIT License。
