# 接入 WeChatPadPro

WeChatPadPro 适配器通过自建的 WeChatPadPro 服务收发个人微信消息。AstrBot 连接你已经部署好的服务，使用管理密钥生成授权码，再扫码登录。

## 准备

1. 部署并启动 WeChatPadProMAX，确认 HTTP 端口可从运行 AstrBot 的机器访问。接口前缀是 `/api`。
2. 准备管理端 `admin_key`。默认服务端口通常是 `8059`。

## 创建机器人

1. 进入 AstrBot WebUI。
2. 打开左侧 `机器人`，点击 `创建机器人`。
3. 选择 `WeChatPadPro`。
4. 填写服务地址、端口和管理密钥，启用后保存。
5. 查看 AstrBot 日志中的登录二维码地址，用微信扫码。登录成功后授权码会写入数据目录的 `wechatpadpro_credentials.json`。

## 配置项

- `admin_key`：WeChatPadPro 管理端密钥。
- `host`：服务 IP 或域名，不要带协议和端口。
- `wechatpadpro_port`：服务端口。旧配置中的 `port` 仍会被读取。

消息通过 `ws://{host}:{port}/ws/sync` 接收。每次连接前向 `/api/Runtime/WebSocketTicket` 换取票据。票据 5 分钟内有效，且只能使用一次。连接断开后会重新换票再连。

## 支持的消息

文本、图片、语音、表情和引用消息可以接收。发送支持文本、图片、表情和语音（语音会转成 Tencent Silk）。私聊和群聊都走 `/Msg/SendTxt`。群消息里的 @ 会把一个或多个成员 wxid 用逗号放进 `At`，可以同时 @ 多个人。
