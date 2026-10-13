# 接入 WeChatPadPro

WeChatPadPro 适配器通过自建的 WeChatPadPro 服务收发个人微信消息。AstrBot 用账号 Access Token 取登录二维码，用管理员 KEY 生成授权码，再扫码登录。

## 准备

1. 部署并启动 WeChatPadProMAX，确认 HTTP 端口可从运行 AstrBot 的机器访问。接口前缀是 `/api`。
2. 准备账号 Access Token，以及管理员 KEY。默认服务端口通常是 `8059`。

## 创建机器人

1. 进入 AstrBot WebUI。
2. 打开左侧 `机器人`，点击 `创建机器人`。
3. 选择 `WeChatPadPro`。
4. 填写服务地址、HTTP 端口、Access Token 和管理员 KEY。WebSocket 端口与 HTTP 不同时再填写 WebSocket 端口。服务使用证书时打开 TLS。启用后保存。
5. 查看 AstrBot 日志中的登录二维码地址，用微信扫码。登录成功后授权码会写入数据目录的 `wechatpadpro_credentials.json`。

## 配置项

- `access_token`：账号 Access Token。`/Login/GetQR`、`/Login/CheckQR`、`/Login/GetCacheInfo` 和 `/Runtime/WebSocketTicket` 放在请求头 `X-Access-Token`。不要填 `/Admin/GenAuthKey` 生成出来的授权码。
- `admin_key`：管理员 KEY，只作为 `/Admin/GenAuthKey` 的 `key` 参数。
- `host`：服务 IP 或域名，不要带协议和端口。
- `wechatpadpro_port`：HTTP API 端口。旧配置中的 `port` 仍会被读取。
- `wechatpadpro_ws_port`：WebSocket 端口。填 `0` 时与 HTTP 端口相同。
- `wechatpadpro_tls`：开启后 HTTP 接口使用 `https`，WebSocket 使用 `wss`。证书由系统信任库校验。默认关闭。

登录后的消息、好友和群接口使用查询参数 `authcode`，值是生成出来的授权码。

消息通过 WebSocket 接收。每次连接前向 `/api/Runtime/WebSocketTicket` 发空请求体的 POST，只带请求头 `X-Access-Token`，不传生成出来的授权码。成功时 `Code` 为 0，票据在 `Data.ticket`，连接地址是 `ws(s)://{host}:{WebSocket 端口}{Data.websocket_path}`（路径通常是 `/ws/sync`）。票据 5 分钟内有效，且只能使用一次。连接断开后会重新换票再连。

## 支持的消息

文本、图片、语音、表情和引用消息可以接收。发送支持文本、图片、表情和语音（语音会转成 Tencent Silk）。私聊和群聊都走 `/Msg/SendTxt`。群消息里的 @ 会把一个或多个成员 wxid 用逗号放进 `At`，可以同时 @ 多个人。
