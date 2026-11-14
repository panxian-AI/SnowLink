"""WebUI（默认 127.0.0.1:5099）：看一眼状态、发一条、调动作、看事件与日志。

和 SnowLuma 一样的入口位置（5099）与「首启随机口令」体验：
第一次启动时生成随机口令打印在控制台，之后写进 `config/webui.json`（sha512 + salt）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__
from .config import OneBotConfig, RuntimeConfig, load_onebot, save_onebot

_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>SnowLink 控制台</title>
<style>
  :root{--bg:#0d1117;--panel:#161b22;--line:#243040;--text:#e6edf3;--sub:#8b949e;
        --teal:#2BC49A;--purple:#7F77DD;--red:#f85149}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 -apple-system,"Segoe UI",
       "Microsoft YaHei",sans-serif}
  header{display:flex;align-items:center;gap:12px;padding:14px 20px;border-bottom:1px solid var(--line)}
  header h1{font-size:16px;margin:0;font-weight:600}
  .tag{font-size:12px;color:var(--sub);border:1px solid var(--line);border-radius:999px;padding:1px 8px}
  main{max-width:1100px;margin:0 auto;padding:20px;display:grid;gap:16px;
       grid-template-columns:repeat(auto-fit,minmax(320px,1fr))}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px}
  .card h2{font-size:13px;margin:0 0 12px;color:var(--sub);font-weight:600;letter-spacing:.04em}
  .kv{display:flex;justify-content:space-between;gap:10px;padding:4px 0;font-size:13px}
  .kv b{font-weight:600}
  input,textarea,select,button{font:inherit;color:var(--text);background:#0b1220;
       border:1px solid var(--line);border-radius:8px;padding:8px 10px;width:100%}
  textarea{min-height:88px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
  button{background:linear-gradient(90deg,#2BC49A,#7F77DD);color:#04120c;border:0;
         font-weight:600;cursor:pointer;width:auto;padding:8px 16px}
  button.ghost{background:transparent;border:1px solid var(--line);color:var(--text)}
  .row{display:flex;gap:8px;align-items:center;margin-top:8px}
  .row label{color:var(--sub);white-space:nowrap;font-size:13px}
  pre{background:#0b1220;border:1px solid var(--line);border-radius:8px;padding:10px;
      overflow:auto;max-height:240px;font-size:12px;margin:8px 0 0}
  .ok{color:var(--teal)} .bad{color:var(--red)}
  .login{max-width:360px;margin:12vh auto;background:var(--panel);border:1px solid var(--line);
         border-radius:12px;padding:24px}
  .login h1{font-size:18px;margin:0 0 6px} .login p{color:var(--sub);font-size:13px;margin:0 0 16px}
  .hint{color:var(--sub);font-size:12px;margin-top:8px}
</style>
</head>
<body>
<div id="login" class="login" hidden>
  <h1>SnowLink</h1>
  <p>输入启动日志里的口令（存在 config/webui.json）</p>
  <input id="pw" type="password" placeholder="口令" autofocus>
  <div class="row"><button onclick="login()">登录</button><span id="loginMsg" class="bad"></span></div>
</div>

<div id="app" hidden>
  <header>
    <h1>SnowLink</h1><span class="tag">OneBot v11</span>
    <span class="tag" id="ver">v{{VERSION}}</span>
    <span style="flex:1"></span>
    <button class="ghost" onclick="logout()">退出</button>
  </header>
  <main>
    <section class="card">
      <h2>运行状态</h2>
      <div id="status"></div>
    </section>
    <section class="card">
      <h2>网络</h2>
      <div id="networks"></div>
    </section>
    <section class="card">
      <h2>发一条</h2>
      <div class="row"><label>对象</label><input id="sendTo" placeholder="QQ号 / 群号 / 备注名"></div>
      <div class="row"><label>群聊</label><input id="sendGroup" type="checkbox" style="width:auto"></div>
      <div class="row"><textarea id="sendText" placeholder="消息内容（纯文本）"></textarea></div>
      <div class="row"><button onclick="sendMsg()">发送</button><span id="sendMsgOut" class="hint"></span></div>
    </section>
    <section class="card">
      <h2>动作调试</h2>
      <div class="row"><select id="action">
        <option>get_login_info</option><option>get_status</option><option>get_version_info</option>
        <option>get_friend_list</option><option>get_group_list</option><option>get_capabilities</option>
        <option>send_private_msg</option><option>send_group_msg</option>
      </select></div>
      <div class="row"><textarea id="params">{}</textarea></div>
      <div class="row"><button onclick="callAction()">调用</button></div>
      <pre id="callOut">（结果在这里）</pre>
    </section>
    <section class="card">
      <h2>最近事件</h2>
      <pre id="events">（暂无）</pre>
    </section>
    <section class="card">
      <h2>日志</h2>
      <pre id="logs">（暂无）</pre>
    </section>
    <section class="card" style="grid-column:1/-1">
      <h2>OneBot 配置（config/onebot_{{UIN}}.json）</h2>
      <textarea id="cfg" style="min-height:220px"></textarea>
      <div class="row"><button onclick="saveCfg()">保存</button><span id="cfgMsg" class="hint"></span></div>
    </section>
  </main>
</div>

<script>
let authed=false;
async function api(path, opts){
  const r=await fetch(path, Object.assign({headers:{'Content-Type':'application/json'}}, opts||{}));
  if(r.status===401){document.getElementById('login').hidden=false;
    document.getElementById('app').hidden=true;throw new Error('need login');}
  return await r.json();
}
async function login(){
  const pw=document.getElementById('pw').value;
  const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({password:pw})});
  if(r.ok){location.reload();}else{document.getElementById('loginMsg').textContent='口令不对';}
}
async function logout(){await fetch('/api/logout',{method:'POST'});location.reload();}
function kv(k,v){return '<div class="kv"><span>'+k+'</span><b>'+v+'</b></div>';}
async function refresh(){
  const s=await api('/api/status');
  document.getElementById('status').innerHTML=
    kv('账号', s.uin||'（未配置）')+kv('昵称', s.nickname||'-')+
    kv('后端', s.backend+(s.offline?' / 离线演示':''))+kv('运行时长', s.uptime+'s')+
    kv('事件', s.events)+kv('动作', (s.actions||{}).actions||0)+
    kv('失败/不支持', ((s.actions||{}).failed||0)+' / '+((s.actions||{}).unsupported||0));
  const n=s.networks||{};
  document.getElementById('networks').innerHTML=
    kv('HTTP', (n.http||[]).join(', ')||'-')+kv('WS', (n.ws||[]).join(', ')||'-')+
    kv('反向 WS', (n.ws_reverse||[]).join(', ')||'-')+kv('HTTP 上报', (n.http_report||[]).join(', ')||'-');
  const ev=await api('/api/events?limit=20');
  document.getElementById('events').textContent=(ev.items||[]).map(e=>JSON.stringify(e)).join('\n')||'（暂无）';
  const lg=await api('/api/logs?limit=40');
  document.getElementById('logs').textContent=(lg.lines||[]).join('\n')||'（暂无）';
}
async function sendMsg(){
  const out=document.getElementById('sendMsgOut');
  try{
    const r=await api('/api/send',{method:'POST',body:JSON.stringify({
      target:document.getElementById('sendTo').value.trim(),
      text:document.getElementById('sendText').value,
      group:document.getElementById('sendGroup').checked})});
    out.textContent=r.ok?('已发送 '+r.id):('失败：'+(r.msg||r.code));
    out.className=r.ok?'hint ok':'hint bad';
  }catch(e){out.textContent=String(e);}
}
async function callAction(){
  let params={};
  try{params=JSON.parse(document.getElementById('params').value||'{}');}
  catch(e){document.getElementById('callOut').textContent='params 不是合法 JSON';return;}
  const r=await api('/api/call',{method:'POST',body:JSON.stringify({
    action:document.getElementById('action').value, params})});
  document.getElementById('callOut').textContent=JSON.stringify(r,null,2);
}
async function loadCfg(){
  const r=await api('/api/config');
  document.getElementById('cfg').value=r.text||'';
}
async function saveCfg(){
  const r=await api('/api/config',{method:'POST',body:JSON.stringify({text:document.getElementById('cfg').value})});
  document.getElementById('cfgMsg').textContent=r.ok?'已保存（重启后生效）':('保存失败：'+(r.msg||''));
}
(async function(){
  try{await api('/api/status');authed=true;document.getElementById('app').hidden=false;
    await loadCfg();await refresh();setInterval(refresh,3000);}
  catch(e){document.getElementById('login').hidden=false;}
})();
</script>
</body>
</html>
"""


def _hash(password: str, salt: str) -> str:
    return hashlib.sha512((password + salt).encode("utf-8")).hexdigest()


def _hash_alt(password: str, salt: str) -> str:
    return hashlib.sha512((salt + password).encode("utf-8")).hexdigest()


class WebUI:
    def __init__(self, gateway: Any, config_path: Path, runtime: RuntimeConfig,
                 *, uin: str = "") -> None:
        self.gateway = gateway
        self.config_path = config_path
        self.runtime = runtime
        self.uin = uin
        self.sessions: set[str] = set()
        self._lock = threading.Lock()
        self.initial_password = ""
        self._webui_file = config_path / "webui.json"
        self._state = self._load_or_init()

    # ---------------------------------------------------------------- 口令
    def _load_or_init(self) -> dict[str, Any]:
        try:
            data = json.loads(self._webui_file.read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("passwordHash"):
                return data
        except (OSError, ValueError):
            pass
        password = secrets.token_urlsafe(12)
        salt = secrets.token_hex(16)
        self.initial_password = password
        state = {
            "passwordHash": _hash(password, salt),
            "passwordSalt": salt,
            "mustChangePassword": False,
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._webui_file.parent.mkdir(parents=True, exist_ok=True)
        self._webui_file.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")
        return state

    def _check(self, password: str) -> bool:
        salt = str(self._state.get("passwordSalt") or "")
        expected = str(self._state.get("passwordHash") or "")
        if not expected:
            return False
        return hmac.compare_digest(_hash(password, salt), expected) or hmac.compare_digest(
            _hash_alt(password, salt), expected
        )

    def _save(self, password: str) -> None:
        salt = secrets.token_hex(16)
        self._state.update({
            "passwordHash": _hash(password, salt),
            "passwordSalt": salt,
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        })
        self._webui_file.write_text(json.dumps(self._state, ensure_ascii=False, indent=2) + "\n",
                                    encoding="utf-8")

    # ---------------------------------------------------------------- HTTP
    def build(self) -> ThreadingHTTPServer:
        ui = self
        page = _PAGE.replace("{{VERSION}}", __version__).replace("{{UIN}}", ui.uin or "<uin>")

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: Any) -> None:
                return

            # ---------------- 工具
            def _cookie_ok(self) -> bool:
                raw = str(self.headers.get("Cookie") or "")
                for part in raw.split(";"):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        if k.strip() == "snl" and v.strip() in ui.sessions:
                            return True
                return False

            def _json(self, status: int, data: Any) -> None:
                raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _html(self, text: str) -> None:
                raw = text.encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if not length:
                    return {}
                try:
                    data = json.loads(self.rfile.read(length).decode("utf-8"))
                except ValueError:
                    return {}
                return data if isinstance(data, dict) else {}

            # ---------------- 路由
            def do_GET(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                query = parse_qs(urlparse(self.path).query)
                if path == "/":
                    self._html(page)
                    return
                if not self._cookie_ok():
                    self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "msg": "需要登录"})
                    return
                if path == "/api/status":
                    self._json(HTTPStatus.OK, ui.gateway.stats())
                    return
                if path == "/api/events":
                    limit = int((query.get("limit") or ["20"])[0] or 20)
                    self._json(HTTPStatus.OK, {"items": ui.gateway.hub.recent(limit)})
                    return
                if path == "/api/logs":
                    limit = int((query.get("limit") or ["40"])[0] or 40)
                    self._json(HTTPStatus.OK, {"lines": ui.gateway.log_lines[-limit:]})
                    return
                if path == "/api/config":
                    text = ""
                    if ui.uin:
                        try:
                            text = (ui.config_path / f"onebot_{ui.uin}.json").read_text(encoding="utf-8")
                        except OSError:
                            text = ""
                    self._json(HTTPStatus.OK, {"ok": True, "text": text, "uin": ui.uin})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "msg": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = urlparse(self.path).path
                if path == "/api/login":
                    data = self._body()
                    if ui._check(str(data.get("password") or "")):
                        sid = secrets.token_urlsafe(24)
                        with ui._lock:
                            ui.sessions.add(sid)
                        raw = b'{"ok": true}'
                        self.send_response(HTTPStatus.OK)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Set-Cookie", f"snl={sid}; Path=/; HttpOnly; SameSite=Lax")
                        self.send_header("Content-Length", str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                    else:
                        self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "msg": "口令不对"})
                    return
                if path == "/api/logout":
                    with ui._lock:
                        ui.sessions.clear()
                    self._json(HTTPStatus.OK, {"ok": True})
                    return
                if not self._cookie_ok():
                    self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "msg": "需要登录"})
                    return
                data = self._body()
                if path == "/api/send":
                    result = ui.gateway.send_text(str(data.get("target") or ""),
                                                  str(data.get("text") or ""),
                                                  group=bool(data.get("group")))
                    self._json(HTTPStatus.OK, result)
                    return
                if path == "/api/call":
                    params = data.get("params")
                    result = ui.gateway.dispatcher.call(str(data.get("action") or ""),
                                                        params if isinstance(params, dict) else {})
                    self._json(HTTPStatus.OK, result)
                    return
                if path == "/api/config":
                    text = str(data.get("text") or "")
                    try:
                        parsed = json.loads(text)
                    except ValueError as exc:
                        self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "msg": f"不是合法 JSON：{exc}"})
                        return
                    if not isinstance(parsed, dict):
                        self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "msg": "顶层必须是对象"})
                        return
                    if ui.uin:
                        save_onebot(ui.config_path, ui.uin, OneBotConfig.from_dict(parsed))
                    self._json(HTTPStatus.OK, {"ok": True})
                    return
                if path == "/api/password":
                    old = str(data.get("old") or "")
                    new = str(data.get("new") or "")
                    if not ui._check(old) or len(new) < 8:
                        self._json(HTTPStatus.BAD_REQUEST,
                                   {"ok": False, "msg": "原口令不对，或新口令少于 8 位"})
                        return
                    ui._save(new)
                    ui.sessions.clear()
                    self._json(HTTPStatus.OK, {"ok": True})
                    return
                self._json(HTTPStatus.NOT_FOUND, {"ok": False, "msg": "not found"})

        return ThreadingHTTPServer((self.runtime.webuiHost, self.runtime.webuiPort), Handler)
