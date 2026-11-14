# 示例配置

复制到 `config/` 再改：

```bash
cp -r config.example config
```

- `onebot_<uin>.json` 的文件名要换成你的 QQ 号；
- 两个 `accessToken` 换成随机串（`python -c "import secrets;print(secrets.token_urlsafe(24))"`）；
- `arklink.json` 的 token 用 `arklink env token` 取；
- `config/webui.json` 不用手写：首次启动自动生成并打印口令。
