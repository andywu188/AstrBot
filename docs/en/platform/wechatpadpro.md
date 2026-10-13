# Connect WeChatPadPro

The WeChatPadPro adapter sends and receives personal WeChat messages through a WeChatPadPro service you host. AstrBot uses the account Access Token to request a login QR code, uses the admin key to create an auth code, then waits for the scan.

## Prepare

1. Deploy and start WeChatPadProMAX. The machine running AstrBot must be able to reach its HTTP port. The API prefix is `/api`.
2. Prepare the account Access Token and the admin key. The service port is usually `8059`.

## Create a bot

1. Open the AstrBot WebUI.
2. Open `Bots` in the sidebar and click `Create Bot`.
3. Choose `WeChatPadPro`.
4. Fill in the host, HTTP port, Access Token, and admin key. Set the WebSocket port when it differs from the HTTP port. Enable TLS when the service uses a certificate, then enable and save the bot.
5. Read the QR-code URL from the AstrBot logs and scan it with WeChat. After login, the auth key is stored in `wechatpadpro_credentials.json` under the data directory.

## Settings

- `access_token`: Account Access Token. `/Login/GetQR`, `/Login/CheckQR`, `/Login/GetCacheInfo`, and `/Runtime/WebSocketTicket` send it as `X-Access-Token`. Do not put the auth code from `/Admin/GenAuthKey` here.
- `admin_key`: Admin KEY. It is sent only as the `key` query of `/Admin/GenAuthKey`.
- `host`: Service IP or domain, without a scheme or port.
- `wechatpadpro_port`: HTTP API port. A legacy `port` value is still read.
- `wechatpadpro_ws_port`: WebSocket port. `0` uses the HTTP port.
- `wechatpadpro_tls`: Use `https` for the API and `wss` for WebSocket. Certificates are checked with the system trust store. Off by default.

After login, message, friend, and group calls use the generated auth code as the `authcode` query.

Messages arrive over WebSocket. Before each connection AstrBot sends an empty POST to `/api/Runtime/WebSocketTicket` with only the `X-Access-Token` header, not the generated auth code. A success response has `Code` 0. The ticket is `Data.ticket`. The socket URL is `ws(s)://{host}:{websocket port}{Data.websocket_path}` (the path is usually `/ws/sync`). The ticket is valid for 5 minutes and can be used once. A dropped connection requests a new ticket and connects again.

## Messages

Text, image, voice, emoji, and quote messages can be received. Sending supports text, image, emoji, and voice. Voice is converted to Tencent Silk. Private and group text both use `/Msg/SendTxt`. A group mention puts one or more member wxids in `At`, separated by commas, so several people can be mentioned in one message.
